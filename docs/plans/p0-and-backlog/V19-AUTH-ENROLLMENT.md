# S3：认证器密钥不进入启动日志

状态：尚未全量验收或合入。最新目录 `/tmp/rssripple-v19-turso-review-2m88z4f9` 已叠加 M2/M3 及 S2 p 的 Turso 突发修订，j 组合回归、k 正式组合入口、l 最终指引回归均通过；已冻结并启动 m 完整单元/API。原 `/tmp/rssripple-v19-auth-enrollment` 是基于 M1 a8a09bd 的历史候选。

## 最新验收准备（j/k/l → m）

n 完整隔离集成现已启动：唯一项目 rssripple-v19-final-n，session 58903，日志 /tmp/rssripple-v19-integration-n.log；沿用 m 同一冻结源，尚未终态。m/n 句柄与清理所需项目名记录在 probes/auth-enrollment-m-running.json。

j **42 passed，58.93 秒**，覆盖先前失败的认证组合及 12/40 并发。k **5 passed，28.33 秒**，涵盖绑定 Turso/PG、Turso 十轮真实突发、PG 服务和双 HTTP 进程；未复现旧 h 的写冲突。最终核对 Dockerfile 发现依赖安装在 .venv，容器绑定文档须沿用服务的 `uv run --no-project` 解释器选择，已修正中英文 README/约定；启动提示只指向部署文档。实际核验 uv 选择候选 .venv 解释器，最后 l **6 passed，5.25 秒**。前端源码相对 g 未变，继承已通过的生产构建。

[五维审查](V19-REVIEW.md) 允许完整门禁，14 有效文件与 3072 输入已冻结，source/tar/frozen 见 probes/auth-enrollment-m-*。m session 34744，日志 /tmp/rssripple-v19-unit-m.log，要求 ≥95%；显式使用独立 PG 容器 rssripple-v19-unit-pg-m（回环 32849、tmpfs）。避免与 S2 并发迁移争用固定 scratch 库名。完整集成尚未启动，待现有集成门禁释放资源后基于同一冻结源启动唯一项目；不能用 k 五项替代全量 ≥85% 门禁。测试期间不得修改该目录的冻结输入，前序 M3/S2 验收与基线匹配仍是合入条件。

h/i 终态：原组合 h 为 **40 passed、1 failed，62.83 秒**，失败是继承的 S2 Turso 12 并发写冲突耗尽重试；没有放松测试。i 正式绑定双库与 PG 服务/HTTP 四入口 **4 passed，22.41 秒**。随后 V18 独立探针确认生产退避下也会突发失败，并增加同引擎短事务串行。新 j 基于该修订重新组合；S3 不能继承旧 h 的失败状态宣称通过，也不能只引用 i 覆盖全部组合行为。历史日志原件 gzip、可读副本、JUnit 与 i 候选 tar/source 均已归档。

后续 g 前端验证：`corepack pnpm run build` 退出 0，构建资产确认包含新的中英文登录绑定提示，14 个候选源文件哈希不变。产物生成于候选 app/static，不作为新的手写源文件提交；生产 Dockerfile 会重新构建前端。原候选已三方合并到 M2/M3/S2 组合基线，新目录 `/tmp/rssripple-v19-rebased-69kjq3or`，无冲突；h 认证组合回归与 i 双库/多进程正式组合入口正在运行。两次合并不能代替完整门禁，也不表示 S3 已合入。

## 必要性复核

主干实际 `app.main.lifespan` 每次成功启动均 WARNING 输出完整 provisioning URI，包含管理员长期 TOTP 密钥。API 与 conventions 文档及中英文登录提示明确依赖此日志。故仅删除日志会破坏首次绑定流程，必须提供运维主动读取入口。

a 最初探针因错误引用不存在的 fixture 报错，不算缺陷复现。b 改用已有 Turso fixture 后，实际 lifespan 连续两次启动均输出同一密钥，负向测试 **1 failed，2.75 秒**；数据库由测试生成并清理，日志中的密钥仅属于临时测试库。

## 方案与边界

保留首次启动初始化和持久化凭证逻辑；启动仅记录不含秘密的绑定指引。新增 `python -m app.scripts.auth_enrollment --show`，要求显式开关且 stdout 是终端，读取同一 DATABASE_URL 的已有 TOTP 密钥并显示 URI；没有公开初始化 API，不执行 DDL，不创建/轮换密钥，不影响旧 Cookie。未初始化、无终端、参数缺失、连接错误均非零退出；数据库异常不打印可能含连接凭证的详情。

默认 PostgreSQL 可在运行容器 exec；Turso 必须停止应用后通过同卷的一次性容器执行，再启动应用。README 中英文、权威 API/认证约定/前端设计和登录提示均同步。此修复停止未来自动日志泄露，不声称清理历史日志或自动处理已泄露的生产密钥。

## 验证与修订

- c：首次/重复 lifespan、只读保持、未初始化不建密钥、显式开关、重定向拒绝、错误详情脱敏及原认证 API，**22 passed，32.98 秒**。
- d：首次真实终端测试 **1 failed，3.81 秒**，发现同一驱动 dispose 连接池后仍持有 Turso 底层文件句柄；CLI 没有绕过锁。该流程不符合“先停止应用”的实际运维契约。
- e：改为真正退出启动子进程后运行 CLI，Turso **1 passed，7.63 秒**；两次启动分别使用独立进程。
- f：正式 Turso/PG 两入口 **2 passed、0 skipped，14.35 秒**。每个入口五个场景：首次/重启不记密钥、真实伪终端只读绑定、重定向拒绝、缺少开关拒绝、真实 pyotp 与生产 ASGI 路由登录且旧 Cookie/密钥不变。数据为专用库生成的测试凭证；数据库、进程、终端和生产路由是真实执行，不称为生产数据录制或真实网络 HTTP。

f 专用 PostgreSQL `rssripple-v19-enrollment-f` 使用回环随机端口、`--rm` 和 tmpfs；测试子库 finally 删除，容器停止退出 0。父测试超时清理进程组，终端文件描述符异常也关闭。Ruff 通过；前端仅两处翻译字符串修改，仍待构建验证。

## 后续验收要求

先合入已满足门禁的前序批次，再基于实际 main 三方合并 S3（特别是 S2 修改的 app/main.py、API 与认证约定），保留 S2 限流行为。补齐完整组合回归和前端构建，按五维审查冻结最终文件，执行全量单元/API ≥95% 与隔离集成 ≥85%，必须零失败，审计跳过、哈希与清理。当前专项不替代门禁，S3 留在 TODO。
