# 下载完成通知（Download Notifications）

RSSRipple 的语义到"订阅 + 下载 + 通知"为止。**文件整理规划（重命名/移动/Plex 刷新）不属于 RSSRipple**，由外部消费者（如 vault-organizer，独立部署在存储所在主机）完成。本文档是通知机制的权威设计：模型、快照契约、webhook 注册与投递、回调 API、状态机、补生成与 mock webhook。

## 分工边界

```
RSSRipple（任意主机）                                外部消费者（如 vault-organizer，挂存储）
┌────────────────────────────────────┐            ┌──────────────────────────────┐
│ task → completed                    │            │ 接收 POST /webhook            │
│   ↓ 停种 + DownloadNotification     │  webhook   │   ↓ start 回调（→processing） │
│     （Agent 队列, pending）         │──────────→│   ↓ 自主规划 + 幂等执行         │
│   ↓ 每分钟投递循环（退避重试）       │            │   ↓ Plex 刷新                 │
│ ack → remove_torrent → done        │←──────────│   ↓ ack / fail 回调            │
│ failed/卡死 → 界面手动重试          │←──────────│                                │
└────────────────────────────────────┘            └──────────────────────────────┘
```

## DownloadNotification 模型

通知队列**从属于下载 Agent**：每个 Agent 一个单例 FIFO 队列（`created_at` 升序为队列序），通知经 `agent_id` 归属（Agent 删除时 SET NULL 保留历史）。

```python
class DownloadNotification(Base):
    __tablename__ = "download_notifications"

    id: str                              # UUID
    agent_id: str | None → Agent         # 队列归属（ON DELETE SET NULL）
    download_task_id: str → DownloadTask # Unique：一个任务至多一条通知（幂等基础；并发创建
                                         # 走 SAVEPOINT，输掉竞争回读已存在行）（幂等基础）
    payload: dict                        # 完整快照 JSON（见下）
    status: str                          # "pending" | "processing" | "done" | "failed"
    error_message: str | None            # 投递失败 / 消费者 fail 原因（ack 后也可能带 warning）
    attempt_count: int                   # 已投递次数（含失败）
    next_attempt_at: datetime | None     # 下次投递时间（指数退避）；pending 且 null = 立即到期
    notified_at: datetime | None         # 最近一次投递成功时间
    processed_at: datetime | None        # ack/fail 时间
    created_at / updated_at
```

**Agent 模型附带三列**（webhook 按 Agent 注册，与单例队列对齐；存量库走 light migration）：

- `notify_webhook_url: str | None` — 注册的 webhook；NULL = 未注册（不投递，队列继续积累，注册后自动恢复）。
- `notify_webhook_mock: bool` — mock webhook：投递直接记成功、不发 HTTP，仅用于在界面查看通知内容。mock 消费者永不 ack → 不会停删 torrent（测试安全）。
- `notify_webhook_token: str | None` — 回调 token：**每次注册 webhook 时动态生成**（`secrets.token_urlsafe(32)`，重新注册即换发，注销清空）。消费者回调 start/ack/fail 时以 `Authorization: Bearer <token>` 鉴权，按通知所属 Agent 比对。

## 状态机

```
pending --(消费者 start)--> processing --(ack)--> done
   |                          |
   |--(投递退避超限 / 消费者 fail)--> failed --(界面 retry)--> pending
```

- `pending`：等待投递或等待消费者处理。投递成功只写 `notified_at`，状态不变（等 start/ack）。
- retry（界面）：`failed`/`processing`（卡死）/`pending` → `pending`，`attempt_count=0`、`next_attempt_at=now`。`done` 不可重试（409 INVALID_STATE）。

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
           "collection": "...", "seasons": [...], "episodes": [{"season","episode","title"}]},
  "files": [{"name": "相对 torrent 根的路径", "size": 734003200}]
}
```

- `torrent_name` + `files` 来自下载器 RPC（`get_torrent_files`，统一客户端接口的一部分）；RPC 失败降级为不带 `files` 入队，消费者退回扫描 `download_dir`。
- 电影无 `seasons`/`episodes`（null）；`collection` 为 WorkCollection 显示名或 null。

## 投递与退避

单一投递路径：scheduler 每分钟 `_process_download_notifications` —— 先为 completed 且无通知的任务**停种（best-effort `pause_torrent`）+ 补建通知**（幂等），再投递所有到期的 `pending` 行。除该循环外没有任何代码发 webhook。

- 投递 body：`{"event": "download.completed", "notification": <payload>}`，POST 到 Agent 注册的 URL，5s 超时，2xx 为成功。
- 失败：`attempt_count += 1`；达到 `NOTIFY_MAX_ATTEMPTS`（默认 5）→ `failed`；否则 `next_attempt_at = now + min(base * 2^attempt, 30min)`（base 默认 30s）。
- 未注册 webhook / mock：跳过 HTTP；mock 直接记 `notified_at`。
- 每条通知独立 commit，写锁绝不跨 HTTP 调用持有。

## API（前缀 /api/v1）

| 端点 | 调用方 | 语义 |
|---|---|---|
| `GET /agents/{id}/notifications?status=&page=` | 前端 | 队列列表（倒序展示；列表项不含 payload） |
| `GET /notifications/{id}` | 前端 | 详情含完整 payload |
| `GET /agents/{id}/webhook` | 前端 | 注册状态 `{registered, url, mock, token}`（token 供复制到消费者配置） |
| `PUT /agents/{id}/webhook` | 前端 | 注册/更新 `{url, mock}`；非 mock 必须 http(s) url；mock 时 url 清空；**每次注册换发新回调 token**（响应含 token） |
| `DELETE /agents/{id}/webhook` | 前端 | 注销（清空 url/mock/token；队列继续积累，重新注册后自动恢复投递） |
| `POST /agents/{id}/notifications/backfill` | 前端 | `{since: datetime \| null}`；为该 Agent 从未生成过通知的 completed 任务补建（`since=null` 从最早开始）；返回 `{created}`；投递走正常循环天然限速 |
| `POST /notifications/{id}/retry` | 前端 | 重置回 pending 立即到期 |
| `POST /notifications/{id}/start` | 消费者 | pending→processing（幂等） |
| `POST /notifications/{id}/ack` | 消费者 | →done + `remove_torrent(delete_data=False)`（删 torrent 失败不阻断 done，记 error_message warning） |
| `POST /notifications/{id}/fail` | 消费者 | →failed + error_message |

- 回调端点（start/ack/fail）要求 `Authorization: Bearer <Agent 回调 token>`（注册 webhook 时动态生成，按通知所属 Agent 比对）；Agent 无 token（未注册）时 503 `CALLBACK_TOKEN_NOT_CONFIGURED`，不匹配 401 `UNAUTHORIZED`。
- 状态冲突 409 `INVALID_STATE`。

## 与其他机制的交互

- **torrent 生命周期**：completed → 创建通知时停种 → ack 时 `remove_torrent(delete_data=False)`。
- **`_cleanup_expired`**：删除过期 completed 任务时跳过存在未 `done` 通知的任务；`done` 通知按 `NOTIFY_RETENTION_DAYS`（默认 30 天）清理。
- **下载器客户端接口**：`get_torrent_files(torrent_id) -> {name, files: [{name, size}]}` 是统一接口的一部分，TransmissionWrapper 与 MockDownloaderWrapper 均实现。

## 前端

Agent 详情页"通知记录" Tab：webhook 注册卡片（含 mock 选项）、"重新生成"（可选起始时间，默认从最早 completed 任务检查）、通知表格（创建时间、最近一次触发时间、状态、错误、详情 Drawer 查看 payload、重试）。

## 环境变量

`NOTIFY_ENABLED`（默认 true，总开关/熔断）、`NOTIFY_MAX_ATTEMPTS`（5）、`NOTIFY_RETRY_BASE_SECONDS`（30）、`NOTIFY_RETENTION_DAYS`（30）。回调 token 不再是环境变量——注册 webhook 时按 Agent 动态生成。
