# V18 S2 TOTP 登录限流

## 最新状态：Turso 突发修订与 p/q 完整门禁

前序 M3 已完成 x/aa 验收并合入 main `2274768`；S2 p 的全部基线哈希已与新 main 核对一致。p/q 自身门禁仍运行，不能因前序已验收而提前合入 S2。

S3 组合 h 暴露继承的 S2 问题：12 并发预留可能耗尽 Turso 写冲突重试。为排除单元夹具快进 sleep 的干扰，新增独立进程探针：m 缺少 MVCC 配置的建库错误不算业务复现；n 正确开启 MVCC 并使用生产退避，在十轮 12/40 并发中，五轮 40 并发有四轮出现数据库异常（分别 5/9/1/5 个请求），没有额度超发，但不能正常返回拒绝结果。证据为 probes/auth-throttling-contention-n-result.json、日志及探针原文；数据是专用临时库与合成来源。

必要修订：仅 Turso 在同一 AsyncEngine 内用异步锁串行额度短事务，弱引用保存引擎对应锁；计数和窗口仍由数据库决定，锁不是进程内额度表。PG 仍依赖原子 upsert/行锁协调跨进程；其他数据库写冲突仍使用既有独立事务重试。该改变解决真实突发退化，不降低断言或提高重试次数来掩盖问题。

o 正式组合 **16 passed，36.55 秒**，包括新增 Turso 子进程十轮突发（真实退避，无 pytest sleep 替换）、PG 服务/真实 HTTP 参数以及单元/API 边界。每轮恰好 5 次放行，其余拒绝，零数据库异常，来源/全局落库额度正确。候选增为 18 文件，完整 3063 输入冻结于 auth-throttling-p-frozen.json，tar/source manifest 同前缀；权威模型、约定和测试清单同步，全仓 Ruff 通过。

旧 k/l 已因独立复现的运行缺陷主动 SIGINT 中断：单元退出 1（中断后 pytest tmp_path teardown KeyError，无完整验收结果），集成退出 2（仅部分 110 passed/1 skipped）。两应用退出 0，部分报告/原始覆盖率导出和项目清理完成，不计算或宣称完整覆盖率通过；3062 原冻结输入未改。摘要 auth-throttling-k-l-terminal.json。专用 PG 容器 rssripple-v18-unit-pg-j 继续供新单元门禁使用。

p 完整单元/API session 60783（显式 PG，95%），q 完整集成 session 90642（唯一项目 rssripple-v18-final-q，85%）已启动，记录 auth-throttling-p-q-running.json。当前目录 `/tmp/rssripple-v18-turso-review`，运行中禁止修改冻结源；必须两道完整门禁、跳过审计、哈希核验与清理全部完成，且前序 M3 验收后才能合入。S2 仍未关闭。

## 组合验证与原门禁（i/j → k/l，已被替代）

候选已接到 M3 的 aa 冻结基线，保留已合入 M2 的全部修订。仅测试清单追加段落产生三方合并冲突，已同时保留 M2/M3 和 S2 的明确范围；17 个 S2 有效文件中的运行实现与 h 完全一致。新目录 `/tmp/rssripple-v18-rebased-7vcwttma`，候选依赖 M3 验收后才能合入 main。

i 认证/限流/API key 与 M2/M3 组合回归 **85 passed，119.25 秒**。j 专用 PostgreSQL 迁移回归 **8 passed、0 skipped，2.09 秒**；JUnit 确认此前 f 跳过的 `test_create_tables_postgres_path`、`test_light_migrations_postgres_legacy_shapes`、`test_light_migrations_postgres_legacy_delivery_columns_nullable` 均实际通过。专用 PG 容器 `rssripple-v18-unit-pg-j`（回环 32848、tmpfs、自动删除）继续供本批完整单元/API 使用，不能提前清理。

17 文件候选与完整 3062 输入已冻结：probes/auth-throttling-k-source.json、auth-throttling-k-frozen.json、auth-throttling-k-candidate.tar.gz。全仓 Ruff 通过。k 完整单元/API session 71817，显式设置 RSSRIPPLE_TEST_POSTGRES_URL，95% 门禁；l 完整集成 session 57265，唯一项目 rssripple-v18-final-l，85% 门禁。记录见 probes/auth-throttling-k-l-running.json；两者尚未终态，不关闭 S2。测试期间不改冻结源，终态后需审计覆盖率、跳过、应用退出和项目/专用 PG 清理。

## 必要性复核

main a8a09bd 的认证入口没有失败计数或请求额度。a 本地 ASGI 使用真实 AuthMiddleware、API 路由与独立测试数据库，验证码验证替换为明确的拒绝函数：连续 20 次请求均 401，验证调用 20 次，无 429，红测失败（1.89 秒）。不使用生产账户、真实密钥或网络攻击；该测试只证明缺少限流，不声称实际破解。原型 /tmp/rssripple-v18-auth-limit，证据 probes/auth-throttling-a.*。

## 候选方案论证

- 单管理员应用仍需限制同一来源与全局尝试，防止更换地址绕过。建议每来源 5 次/60 秒、全局 30 次/60 秒；在 OTP 验证前以数据库原子操作预留额度。成功认证也消耗本窗口额度，避免并发绕过；超限 429，统一错误结构与 Retry-After，窗口到期自动恢复。该阈值为候选工程选择，不是既有用户配置。
- 计数跨 web worker/进程共享并可重启保留，使用现有双库 SQLAlchemy upsert 模式；不使用进程内字典作为权威，不依赖 Redis 才能保护独立部署。
- 来源使用服务器解析的 peer 地址，不直接相信请求自带 X-Forwarded-For；部署代理信任配置需写清。全局额度是必要后盾，避免无限地址键及分布式尝试。过期桶有界清理；不记录验证码或密钥。
- 失败、成功、格式错误是否消耗额度必须明确，验证失败事务不能回滚掉预留计数；并发争抢必须在真实双库独立会话中证明预算上限。数据库不可用不得绕过保护。

## 验收标准

真实 API 测试覆盖阈值/Retry-After/恢复/成功 Cookie/格式错误/伪造转发头；真实 PG 跨进程并发验证总验证次数受预算限制，Turso 独立会话验证同样语义；重启与旧库升级测试、来源桶清理与来源轮换全局保护。时间用可控时钟，禁止长时间 sleep。数据均为专用测试账户配置，不能复用真实 TOTP 密钥。权威 API、错误码、数据模型、迁移、认证约定同步更新。每轮仍要求完整单元/API ≥95%、完整隔离集成 ≥85%，零失败与清理审计。S2 保留 TODO；S1 SSRF、S3 日志密钥等独立问题不由本修复代替。

## 持久化预算原型（b/c）

独立原型新增 AuthRateLimitBucket，每来源 5 次、全局 30 次、60 秒窗口，条件 upsert 在单独短事务预留且提交后才验证 OTP。全局预算先于来源预算，即使来源超限也消耗全局请求预算；全局拒绝不创建新来源行。来源键仅保存 peer 地址 SHA-256，不读原始转发头。每次全局预留成功后最多清理 100 个已过期来源桶，数据库错误不绕过验证。格式不符合 OTPRequest 的请求由 422 拒绝、不进入验证码验证或消耗预算；任意通过 schema 的字符串均占额度。

HTTP 429 使用 RATE_LIMITED 与 Retry-After；统一 HTTP 异常处理器补转发 exc.headers，401 原语义保持。b 原认证接口及必要性用例 17 passed（17.77 秒）；c 扩展为 **24 passed（35.52 秒）**：真实 Turso 独立会话 12 并发仅 5 次预留成功、轮换 40 来源仅 30 次成功、401 后仍限流、边界到期恢复/清理、伪造 X-Forwarded-For 不换桶、真实 pyotp 成功 Cookie 与成功次数限流、schema 错误以及 DB 不可用。来源/时钟合成，密钥只在专用测试库创建，未使用生产数据。

待验证：四个真实 PostgreSQL 进程并发、进程退出后持久性、旧库新增表幂等、权威契约同步、完整两套门禁。d 专用容器 rssripple-v18-auth-d（随机回环端口 32844）与自建 auth_limit_* 数据库正在验证；不能将专项通过当作验收。

## PostgreSQL 跨进程与持久性（d/e）

d **1 passed，2.31 秒**，内部五个必要场景全部通过：旧库幂等增加表保留 AppSetting；四进程同来源 12 次预留仅 5 次成功；四进程轮换来源 40 次仅 30 次成功；进程退出及连接重建后拒绝状态保留；到期恢复并清理过期桶。每个子进程经过就绪屏障一起开始，专用数据库 finally 删除，容器 rssripple-v18-auth-d 已清理。该测试验证真实跨进程共享服务预算，HTTP 边界另由 API 测试覆盖。

e **28 passed，41.77 秒**，补齐 Turso 旧库新增表幂等、连接重建持久性、105 条过期桶一次仅清理 100 条且保留有效桶、数据库失败不调用验证码验证。f 正在扩大到 API key、异常处理、迁移和应用静态资源，并加强 DB 故障实际 HTTP 500/no-cookie 断言。候选 16 文件、哈希与补丁见 probes/auth-throttling-f-source.json / auth-throttling-f-prototype.patch。已同步候选权威模型/API/错误码/迁移/代理约定/测试清单；运行代码仍不在 main，完整门禁尚未执行。

f 已退出 0：98 passed、3 skipped、2 warnings，103.53 秒，16 文件哈希一致。三项跳过为既有 PostgreSQL 迁移测试未配置其专用地址（默认 127.0.0.1:5432 不可用）；d 的独立 PostgreSQL 限流专项不能替代这三条旧迁移路径。完整门禁须提供所需环境并继续审计。日志与 JUnit 已归档 probes/auth-throttling-f.*；候选仍未验收或合入。

## 多 HTTP 进程验证的必要性（g）

既有四进程预算测试直接调用服务，不能单独证明真实 HTTP 路由在不同 web 进程间遵守预算、共享 Cookie、进程重启后继续限流。本轮保持运行实现不变，将正式 PG 入口参数化为 service/http 两套独立子库测试，新增两个 Uvicorn 进程的真实网络验证：12 个合法 TOTP 请求只能成功 5 次；Cookie 在另一进程和替换后的进程仍有效且 OTP 仍受限；八个实际回环源地址各五次请求受全局 30 次限制；401 累计到限额后正确验证码也被拒，直接推进专用测试表的过期时间后恢复。

边界明确：生产 app 路由/认证/异常处理器，lifespan 关闭，专用测试表和非生产密钥由驱动准备；服务不解析代理头，测试无外部网络。所有子进程与专用库 finally 清理，父驱动超时杀进程组，避免遗留 HTTP 服务。g 在 rssripple-v18-auth-g 临时 PostgreSQL 上执行，结果未出前不计入验收。

## 多 HTTP 进程验证结果（g/h）

g 为 **1 passed、1 failed，6.12 秒**。HTTP 的第一组 12 并发请求已满足 5 次成功/7 次拒绝，但替换进程时驱动错误地要求主动 SIGTERM 后退出码为 0。检查本地 Uvicorn `Server.capture_signals`，它在正常关闭后恢复并重新发送收到的信号，因此预期退出码为 -15。修订仅允许本 helper 主动终止的进程返回 0 或 -SIGTERM；意外提前退出和超时仍失败，业务断言未改。

h 两个正式入口 **2 passed、0 skipped，8.18 秒，退出 0**，包含五个跨进程服务场景和四个真实 HTTP 场景。HTTP 确认来源并发上限、跨进程 Cookie 与进程替换后额度持久、八个真实回环源地址共享全局上限，以及错误验证码耗额和到期恢复。全部数据为专用测试库的合成配置；真实部分是 PostgreSQL、多进程、HTTP、生产认证处理与 pyotp 校验，不使用生产密钥。

专用数据库由父测试 finally 删除，`rssripple-v18-auth-g` 以 `--rm` 和 tmpfs 运行并已停止清理，未发现认证测试子进程。日志原件 gzip、可读副本、JUnit 和九场景结果见 probes/auth-throttling-g.* / auth-throttling-h.*。候选现为 17 文件，完整 tar、文本补丁、候选/基线哈希见 probes/auth-throttling-h-source.json 等。Ruff 通过；[审查](V18-REVIEW.md) 仅允许进入全量门禁，S2 尚未验收或合入。

## q 完整集成终态

18 文件修订候选的 q 完整集成 3189 passed、17 skipped、8 warnings，1974.16 秒，退出 0；覆盖率 20794/23573 = 88.21%，汇总门禁退出 0。与 M3 aa 相比无新增跳过、无理由变更。3063 冻结输入不变，两个应用经 SIGINT 均正常退出 0，产物导出和 `rssripple-v18-final-q` 的 down -v 清理均退出 0。证据见 `probes/auth-throttling-q-terminal.json` 及同前缀报告。p 完整单元/API 仍运行，专用 PG 容器继续供其使用；S2 尚不能验收或合入。
