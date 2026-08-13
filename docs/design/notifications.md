# 下载完成通知（Download Notifications）

RSSRipple 的语义到"订阅 + 下载 + 通知"为止。**文件整理规划（重命名/移动/Plex 刷新）不属于 RSSRipple**，由外部消费者（如 vault-organizer，独立部署在存储所在主机）完成。本文档是通知机制的权威设计：模型、快照契约、多 webhook 注册、fan-out 投递与退避、聚合状态机、重试与保留策略、下游清理 API。

## 分工边界

```
RSSRipple（任意主机）                                外部消费者（如 vault-organizer，挂存储）
┌────────────────────────────────────┐            ┌──────────────────────────────┐
│ task → completed                    │            │ 接收 POST webhook（2xx）      │
│   ↓ 停种 + DownloadNotification     │  webhook   │   ↓ 自主规划 + 幂等执行         │
│     （Agent 队列，payload 快照）     │  fan-out   │   ↓ Plex 刷新                 │
│   ↓ ensure_deliveries（每通知 ×      │──────────→│   ↓ 用任务 API 清理            │
│     每启用 webhook 各一条 delivery） │  （每 webhook│   （GET /tasks、pause、DELETE） │
│   ↓ 每分钟投递循环（退避重试）       │  独立投递） │                                │
│ 2xx → done；退避超限 → failed       │            │                                │
│ 失败/卡死 → 界面手动重试             │            │                                │
└────────────────────────────────────┘            └──────────────────────────────┘
```

投递是**纯出站、发后即忘（fire-and-forget）**：RSSRipple 向每个注册的 webhook POST 一次，2xx 即视为成功。没有任何 token 机制、也没有任何入站回调（旧版的 start/ack/fail 回调端点已删除）。消费者完成处理后如需清理（停种、删 torrent），直接调用 RSSRipple 的任务 API（见下文"下游清理"）。

## 模型

### DownloadNotification（快照锚点）

通知队列**从属于下载 Agent**：每个 Agent 一个单例 FIFO 队列（`created_at` 升序为队列序），通知经 `agent_id` 归属（Agent 删除时 SET NULL 保留历史）。fan-out 重构后本表**只保留快照锚点**，投递状态全部下放到 `webhook_deliveries`：

```python
class DownloadNotification(Base):
    __tablename__ = "download_notifications"

    id: str                              # UUID
    agent_id: str | None → Agent         # 队列归属（ON DELETE SET NULL）
    download_task_id: str → DownloadTask # Unique：一个任务至多一条通知（幂等基础；并发创建
                                         # 走 SAVEPOINT，输掉唯一约束竞争回读已存在行）
    payload: dict                        # 完整快照 JSON（见下）
    created_at / updated_at
```

### AgentWebhook（webhook 注册表）

一个 Agent 可注册**任意多个** webhook；每条通知 fan-out 到其 Agent 全部**启用**的 webhook，每个 webhook 一条 delivery：

```python
class AgentWebhook(Base):
    __tablename__ = "agent_webhooks"

    id: str                              # UUID
    agent_id: str → Agent                # FK CASCADE
    url: str                             # 投递目标（非 mock 必须 http(s)；mock 允许为空，
                                         # 服务端落 mock://local 占位，仅展示用）
    mock: bool                           # mock webhook：投递直接记成功、不发 HTTP，
                                         # 仅用于在界面查看通知内容（测试通道）
    enabled: bool                        # 停用的 webhook 保留行与投递历史但不再接收
                                         # 新 delivery；重新启用后从积压 backlog 恢复
    created_at / updated_at
```

### WebhookDelivery（投递执行记录）

每对 `(notification, webhook)` 一行，是通知管道的 fan-out 单元；每条 delivery 携带自己的状态与重试簿记，单个 webhook 失败绝不阻塞其他 webhook：

```python
class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "webhook_id"),  # fan-out 幂等：
        # 一条通知对同一 webhook 至多一条 delivery，fan-out 跑多少次都一样
    )

    id: str                              # UUID
    notification_id: str → DownloadNotification  # FK CASCADE
    webhook_id: str → AgentWebhook       # FK CASCADE
    status: str                          # "pending" | "done" | "failed"
    attempt_count: int                   # 已失败次数
    next_attempt_at: datetime | None     # 下次投递时间（指数退避）；pending 且 null = 立即到期
    error_message: str | None            # 最近失败原因
    delivered_at: datetime | None        # 投递成功时间
    created_at / updated_at
```

### 存量库 light migration

- 旧版 `agents.notify_webhook_url` / `notify_webhook_mock` 注册在启动时由 `_apply_light_migrations` 一次性复制为 `agent_webhooks` 行（已有 webhook 的 Agent 跳过；幂等）；旧三列（含 `notify_webhook_token`）留在物理表中成为**惰性孤儿列**（ORM 已移除，无 DROP migration）。
- 旧版 `download_notifications` 的投递列（`status`/`error_message`/`attempt_count`/`next_attempt_at`/`notified_at`/`processed_at`）同样从 ORM 移除。SQLite/Turso 无法原地 DROP NOT NULL，迁移**重建整张表**为当前模型列（复制 id/agent_id/download_task_id/payload/时间戳后 DROP + RENAME）；PostgreSQL 仅对 `status`/`attempt_count` 执行 `DROP NOT NULL`，孤儿列保留。

## 状态机

### delivery 状态（DB 持久化）

```
pending --(2xx / mock)--> done
   |
   |--(退避超限，attempt_count ≥ NOTIFY_MAX_ATTEMPTS)--> failed --(界面重试)--> pending
```

- `pending`：等待投递或退避中；`next_attempt_at` 为空或 ≤ now 即到期。
- webhook 在 fan-out 后被删除或停用：delivery 保持 `pending` 不投递，webhook 恢复可用后自动续投。
- 重试（界面）：`failed`（或 `all` 模式下 `done` + `failed`）→ `pending`，`attempt_count=0`、`next_attempt_at=now`、`error_message=null`，下一轮 tick 立即投递。

### notification 聚合状态（API 计算，不落库）

通知的展示状态由其全部 delivery 聚合（列表端点用相关子查询按同一规则过滤）：

- 无任何 delivery **或**存在任一 `pending` delivery → `pending`；
- 全部 delivery 均为 `done` → `done`；
- 其余（有 `failed` 且无 `pending`）→ `failed`。

## 快照 payload（契约）

创建时冻结，此后 metadata 变更不影响本条：

```json
{
  "notification_id": "...",
  "agent": {"id": "...", "name": "..."},
  "task": {"download_task_id": "...", "download_dir": "<daemon 视角绝对路径>",
           "torrent_name": "..."},
  "resource": {"title_raw": "...", "season": 4, "episode": 9,
               "is_batch": false, "episode_start": null, "episode_end": null,
               "subtitle_langs": ["zh-CN"], "resolution": "1080p",
               "container": "MKV", "title_year": 2023},
  "work": {"type": "series | movie",
           "title_en": "...", "title_cn": "...", "original_title": "...",
           "year": 2023, "content_type": "anime | tv | movie | ...",
           "is_anime": true, "collection": "...", "genre": ["Animation", "Fantasy"],
           "seasons": [...], "episodes": [{"season","episode","title"}]},
  "files": [{"name": "相对 torrent 根的路径", "size": 734003200}]
}
```

- `work.genre`：作品分类标签快照，取值为**封闭 TMDB 27 类英文 canonical 名**（快照时经 `normalize_genres` 归一化；完整枚举见 data-models.md「genre 取值约定」，API 侧 /docs 中 `NotificationWorkPayload.genre` 渲染同一枚举）。旧通知无此键，走"重新生成"后补齐。
- `work.is_anime`：三态动漫标记快照（`true`/`false`/`null`，与 `content_type` 并列，创建时冻结；判定规则见 data-models.md「is_anime 判定约定」，schema 见 `app/schemas/notification.py` 的 `NotificationWorkPayload`）。旧通知无此键，走"重新生成"后补齐。
- `torrent_name` + `files` 来自下载器 RPC（`get_torrent_files`，统一客户端接口的一部分）；RPC 失败降级为不带 `files` 入队，消费者退回扫描 `download_dir`。
- 电影无 `seasons`/`episodes`（null）；`collection` 为 WorkCollection 显示名或 null。
- `task.download_task_id` 是消费者后续操作任务的句柄（见"下游清理"）。

## 投递与退避

单一投递路径：scheduler 每分钟 `_process_download_notifications` tick ——

1. **入队（enqueue）**：为 completed 且无通知的任务**停种（best-effort `pause_torrent`）+ 补建通知**（`download_task_id` 唯一约束 + SAVEPOINT 竞争回读，幂等）。**仅当其 Agent 至少有一个启用 webhook 时才补建**——未注册 webhook 的 Agent 不生成通知，避免堆积无用记录（手动"重新生成"不受此限）。
2. **fan-out（`ensure_deliveries`）**：为"通知 × 其 Agent 每个启用 webhook"补建缺失的 `pending` delivery（`next_attempt_at=now`）。幂等（`(notification_id, webhook_id)` 唯一约束 + SAVEPOINT 吸收竞争）；新注册/重新启用的 webhook 在下一次运行（或注册时立即，见下）收到全部积压。
3. **投递（`deliver_due_deliveries`）**：捞取全部到期 `pending` delivery（每 tick 上限 50 条，`created_at` 升序），并发投递（`Semaphore(10)` 上限）。

除该循环外没有常驻代码发 webhook；唯一的额外 fan-out 触发点是 **webhook 注册/更新 API**（`POST /agents/{id}/webhooks` 与 `PUT .../webhooks/{wid}` 在提交前调用 `ensure_deliveries(agent_id)`），使新注册/重新启用的 webhook 立即收到积压而不等下一个 tick。

- 投递 body：`{"event": "download.completed", "notification": <payload>}`，POST 到 webhook 的 URL，**180s 超时**（`WEBHOOK_TIMEOUT_SECONDS`），2xx 为成功。
- 失败：`attempt_count += 1`；达到 `NOTIFY_MAX_ATTEMPTS`（默认 5）→ `failed`；否则 `next_attempt_at = now + min(base * 2^attempt, 1800s)`（base 默认 30s，封顶 30 分钟）。
- mock webhook：不发 HTTP，直接记 `done`（`delivered_at` 落时间），仅用于在界面查看通知内容。
- **webhook URL 可达性（Docker 部署）**：消费者在宿主机或其他容器时，机器主机名（常解析为 127.0.1.1 回环）在容器内不可达；`docker-compose.yml` 已注入 `extra_hosts: host.docker.internal:host-gateway`，指向宿主机的 webhook 应注册为 `http://host.docker.internal:<port>/...`。
- 每条 delivery 独立 commit（行变更经 `commit_lock` 串行化，AsyncSession 不可重入），写锁绝不跨 HTTP 调用持有；单个 webhook 失败不回滚其他 delivery。

## API（前缀 /api/v1）

| 端点 | 调用方 | 语义 |
|---|---|---|
| `GET /agents/{id}/notifications?status=&page=` | 前端 | 队列列表（倒序；列表项不含 payload，带聚合 `status` 与 `delivery_summary {total,done,failed,pending}`；`status` 按聚合状态过滤：pending/done/failed，其他值 422） |
| `GET /notifications/{id}` | 前端 | 详情：完整 payload + `deliveries` 数组（每条含 webhook_url、status、attempt_count、error_message、delivered_at、next_attempt_at） |
| `GET /agents/{id}/webhooks` | 前端 | webhook 列表（`{id, url, mock, enabled, created_at}`，创建时间升序） |
| `POST /agents/{id}/webhooks` | 前端 | 注册 webhook `{url, mock?, enabled?}` → **201**；非 mock 必须 http(s) url（422）；注册后立即 fan-out 积压（`ensure_deliveries`） |
| `PUT /agents/{id}/webhooks/{wid}` | 前端 | 更新 `{url?, mock?, enabled?}`（部分更新）；更新后同样立即 fan-out（重新启用即恢复投递积压） |
| `DELETE /agents/{id}/webhooks/{wid}` | 前端 | 删除 webhook（其 delivery 历史随 FK CASCADE 删除；通知行保留） |
| `POST /agents/{id}/notifications/regenerate` | 前端 | `{since: datetime \| null}`；对该 Agent 全部 completed 任务（`since` 按 `completed_at` 过滤，null=从最早开始）**重跑完整生成链路**（停种 + 拉取文件清单 + 构建快照）：无通知的补建，已有的原地重建 payload（保留行与 `notification_id`）并将其非 pending delivery 复位为立即到期 pending 随新快照重投；当次链路拿不到 torrent 文件清单（RPC 失败/种子已删/无种子）时保留旧快照不降级；返回 `{created, regenerated}`；fan-out 与投递走正常循环天然限速 |
| `POST /notifications/{id}/retry` | 前端 | 单条重试：body `{mode: "failed" \| "all"}` → `{reset: n}`。`failed` = 仅重置 failed delivery；`all` = 重置全部非 pending delivery（done + failed）。重置为 pending 且立即到期 |
| `POST /notifications/retry` | 前端 | 批量重试：body `{mode, since?, agent_id?}` → `{reset: n}`。`since` 按 `notification.created_at >= since` 过滤；`agent_id` 限定 Agent；缺省 = 全库范围 |

## 下游清理（消费者侧任务 API）

回调机制删除后，webhook 消费者用 RSSRipple 的任务 API 完成清理——这些 API 隐藏 Transmission 内部细节（一律以 RSSRipple 的 `DownloadTask` UUID 寻址），payload 中的 `task.download_task_id` 即句柄：

- `GET /api/v1/tasks` — 全局任务列表（分页 `page`/`page_size`≤100；过滤 `downloader_id`/`agent_id`/`status`；`created_at` 倒序）。
- `GET /api/v1/tasks/{id}` — 任务详情（含 file_resource、agent、channel 信息）。
- `POST /api/v1/tasks/{id}/pause` — 停止（暂停）种子。
- `DELETE /api/v1/tasks/{id}` — 删除任务：调用 `remove_torrent`（query 参数 `delete_data` 控制是否删已下载数据），任务标记 `cancelled`。
- 另有 `GET /api/v1/downloaders/{id}/tasks`（本地任务分页）与 `GET /api/v1/downloaders/{id}/torrents`（实时种子列表）。

## 与其他机制的交互

- **torrent 生命周期**：completed → 创建通知时停种（best-effort）。此后 RSSRipple 不再动种子；停删由消费者按需通过任务 API 驱动。
- **`_cleanup_expired`（每日）**：删除过期 completed 任务时跳过其通知存在**任一非 `done` delivery** 的任务（投递重试循环仍需要通知 payload 引用的任务行）；创建时间早于 `NOTIFY_RETENTION_DAYS`（默认 30 天）的通知整行删除（delivery 随 ORM 级联一并清理）。
- **下载器客户端接口**：`get_torrent_files(torrent_id) -> {name, files: [{name, size}]}` 是统一接口的一部分，TransmissionWrapper 与 MockDownloaderWrapper 均实现。

## 前端

Agent 详情页"通知记录" Tab：webhook 多注册列表（添加/编辑/删除/启用开关/mock 标记；"重新生成"按钮（弹窗可选起始时间，留空=从最早 completed 任务开始）对该 Agent 的 completed 任务重跑完整生成链路——补建缺失通知、重建已有通知的 payload 并复位其投递重投，提示补建/重新生成数量；通知表格（创建时间、聚合状态、投递摘要 `{done}/{total}`、操作列——详情 Drawer 展示 payload JSON + 逐条 delivery 表格，行级重试下拉选 failed-only / all）；批量重试弹窗（mode 单选 + 可选 since）；手动"刷新"按钮 + 存在 pending 时每 10s 静默轮询列表。

## 环境变量

`NOTIFY_ENABLED`（默认 true，总开关/熔断）、`NOTIFY_MAX_ATTEMPTS`（5）、`NOTIFY_RETRY_BASE_SECONDS`（30）、`NOTIFY_RETENTION_DAYS`（30）。webhook 超时为代码常量 180s，非环境变量。
