# V22：跨域配置与中间件顺序

状态：必要性复核与本地红测完成，独立副本已实现初步修复，仍缺浏览器与完整门禁，未合入。副本 `/tmp/rssripple-v22-cors-36t2eojn`，运行基线为已验收 S1 `6bf8c1c`；不修改 V21 正在运行的冻结副本。

## 必要性与优先级

保留 P2。生产同源 SPA 不依赖跨域开放；独立部署前端的合法跨域预检会被外层认证提前返回 401，且 401 无 CORS 头，浏览器无法按正常认证错误处理。当前通配来源加 credentials 还会在含 Cookie 的请求上反射任意 Origin。SameSite=Lax 限制了跨站 Cookie，不能仅凭 ASGI 手工 Cookie 就宣布任意站点可读取登录数据；CORS 也不是 CSRF 防护。

a 三项红测中第三项先因错误假设未知 API 路径应返回 404 失败；当前 SPA fallback 返回 200。修正后 b 三项均在目标断言失败（0.96 秒）：预检 401、未认证 401 缺 allow-origin、任意合成 Origin 被反射且 allow-credentials=true。测试执行实际 app.main ASGI 栈，未 mock Auth/CORS；用合成环境 bootstrap key 完成认证，附带无意义 Cookie 只验证 CORS 的头处理。该响应是 SPA fallback 页面，不是已证明的私密数据泄露。

本领域不需要录制 torrent/feed 作为数据；来源、凭证与请求均明确合成，避免把真实用户 Cookie 带入证据。日志、JUnit 和测试源码原件见 probes/cors-policy-a/b-*。

## 方案边界

1. 新增启动配置 CORS_ALLOWED_ORIGINS，JSON 精确 HTTP(S) origin 列表，默认空（同源 SPA 不受影响）；拒绝通配、null、userinfo、路径、query/fragment 与控制字符，规范化默认端口。环境示例和权威约定同步更新。
2. CORS 位于 Auth 与数据库重试层外侧，让预检不要求认证，允许来源的 401 可被浏览器读取；正常 API 仍由 Auth 验证。检查 FastAPI 未捕获 500 的外层 ServerErrorMiddleware：不能只重排 add_middleware 就声称所有错误响应均有 CORS，需有实际 500 测试后决定外层封装方式。
3. 未授权来源不返回 allow-origin。不要把 CORS 拒绝等同服务器未执行请求；CSRF 另行验证 Origin、同站不同 origin、表单简单请求与 Cookie 行为，发现需要时明确扩展范围，不能默默放弃该部分。
4. 保留程序端无 Origin 的 API key、同源 TOTP Cookie、SSE、poster、压缩及既有错误结构。API 文档与部署说明明确跨站 Cookie 仍受 SameSite=Lax 限制。

## 严格验收

覆盖允许/拒绝/无 Origin、预检与实际请求、Cookie/API key、401/422/404/500、流式和 GZip；验证精确 origin 默认端口、同站兄弟来源、配置失败关闭。配置从独立进程启动加载，避免测试仅修改中间件参数却漏测配置接入。服务器 ASGI 测试不能代替浏览器 CORS/SameSite/CSRF 验证，需补受控本地浏览器场景并标注边界。最后执行完整单元/API ≥95%、隔离集成 ≥85%，零失败、跳过/哈希/退出/清理审计。当前红测绝不代表修复完成。

## 原型 d/e 验证

新 `CorsFastAPI` 仅重写 build_middleware_stack，在 super 构建的完整错误栈外应用标准 CORSMiddleware；保留 FastAPI 路由、lifespan、dependency_overrides 和现有 Auth/DB 重试。精确 origin 列表与已有出站例外共享配置校验，默认空且两项配置互不影响。权威约定、API、环境示例与测试清单已在候选同步。

c 首次因遗漏 lifespan 的 FastAPI 类型导入而收集失败，修正后 d 14 passed；e 扩大错误/配置/流式/无 Origin/拒绝来源矩阵，32 passed、1 项既有 pytest 弃用警告，0.94 秒，Ruff 通过。422 专项目前由临时端点主动抛出，不代表已覆盖请求模型自动验证；500 为真实未捕获异常经生产 handler 生成。源码/基线与九文件候选归档见 `probes/cors-policy-e-*`。

续接仍须独立进程配置接入、真实 Cookie/浏览器 SameSite/CSRF、GZip、poster 和自动 422 等边界；随后对齐 V21 验收主干、五维审查和完整双门禁。32 项局部通过不关闭 S4，也不替代 CSRF 论证。

## Chromium 真实 Cookie 验证（f–h）

在三个仅回环临时来源上运行 Chromium 1194，启动独立 Turso 库，生产 TOTP 端点使用固定合成秘钥登录并实际签发 HttpOnly/SameSite=Lax Cookie。环境变量允许 127.0.0.1:32901；拒绝同站另一端口 32902 及跨站 localhost:32902。不加载真实用户配置，应用 lifespan 关闭以避免调度任务；建表与测试秘钥初始化使用生产 DB helper。

f 启动器模块名错误、g 初始化与服务使用不同事件循环导致登录 500，均为夹具失败，不是产品缺陷。h 修复后浏览器脚本退出 0：允许来源读取 authenticated=true；两个拒绝来源 fetch 均报 TypeError。Playwright 对被 CORS 阻断请求获取的请求头不完整，结果 JSON 的 cookieSent=false **不能当作 Cookie 未上网证据**；该字段只来自浏览器 API 可见头。

关键新证据：从未授权同站来源发简单 POST 到真实 `/api/v1/auth/logout`，浏览器读取失败，但返回 API 同源页面再读状态为 authenticated=false。这证明当前 CORS 原型不能阻止退出登录副作用，SameSite=Lax 也不能阻止本次同站来源行为。仅证明 logout CSRF，不推广为所有业务写操作可被跨站利用。

下一步扩展状态变更来源防护：明确 Cookie/无凭证认证端点与显式 API key 的规则，允许同源与配置白名单，拒绝不可信 Origin/明确跨站浏览器请求；保留无 Origin 程序端兼容，并评估 Referer/Sec-Fetch-Site 的边界。需用真实浏览器重新证明未授权 logout 不清 Cookie，并覆盖合法登录/退出及业务写入，不能只让 CORS read 失败。浏览器原始脚本、启动器、结果与夹具错误日志归档为 probes/cors-browser-*；临时服务发送 SIGINT 后会话 82114 已终止，进程退出 130；浏览器脚本自身退出 0，源页面临时 HTTP 服务在 finally 关闭。

## 来源防护原型与浏览器回归（i/j）

增加独立 BrowserOriginMiddleware，在 CORS 内、完整应用栈外拒绝不可信的 API 状态变更；同源比较按 scheme/host/实际端口，配置白名单共享来源规范。Origin 优先；缺失时使用 Referer，完全缺失时拒绝明确 cross-site/same-site Fetch Metadata；无浏览器来源头的程序端保留兼容。明确 API-key 头不会绕过不可信来源检查。403 使用 FORBIDDEN 统一错误结构，已同步候选权威约定/API/错误文档。

i 46 passed、1 项既有弃用警告，1.00 秒；涵盖真实 logout 是否发送清 Cookie、信任/不信任来源、无来源兼容以及实际 channels 请求模型自动 422。Ruff 通过。j 真实 Chromium 脚本加入强断言并退出 0：拒绝同站来源 logout 后同源状态仍 authenticated=true；允许来源 logout 返回 200，随后 authenticated=false；允许读取和拒绝读取保持正常。临时服务 SIGINT 退出 130，浏览器与页面服务 finally 清理。

十一文件候选/基线/归档见 `probes/cors-policy-j-*`，浏览器结果与脚本见 `probes/cors-browser-j-*`。尚未合入；仍需把浏览器入口整理为可移植自动化、覆盖代理/重复头/压缩/poster 等剩余边界、完整代码审查以及 ≥95%/≥85% 门禁。V21 冻结候选未修改。

## 兼容性矩阵与可复用浏览器入口（k–m）

k 新增重复头、代理、GZip 与 poster 专项，30 passed / 1 failed，证明重复 Referer 第一项可信时仍执行 logout。修正为单值 Referer 后，l 完整专项 53 passed、1 项既有弃用警告，0.99 秒。没有通过信任任意 Forwarded 头来解决代理场景；伪造转发头仍被拒绝，已还原的外部 HTTPS ASGI scope 同源成功。压缩响应同时保留 Vary Origin/Accept-Encoding；真实 poster 静态挂载仍需认证。

新增 `tests/browser/` 可复用夹具、脚本和 README，移除机器专属路径；Playwright/Python/浏览器路径从标准依赖或可选环境传入。夹具强制临时数据库/海报目录、合成 TOTP、回环监听与关闭应用 lifespan，并在 finally dispose/删除数据。m 按此入口实际运行退出 0，所有浏览器断言通过；服务 SIGINT 收尾。十四文件候选及哈希见 `probes/cors-policy-m-*`。

下一步：核验源码五维质量与新增中间件覆盖率，等待 V21 门禁结果后对齐主干并冻结完整双门禁。尚未完成完整 ≥95%/≥85% 验收，CORS/CSRF 项不关闭。

## 门禁前审查与认证兼容性（n/o）

五维记录见 [V22-REVIEW](V22-REVIEW.md)。n 将现有认证 API 与静态页面回归加入，76 passed；o 补畸形 Origin 两项真实请求后 78 passed、1 项既有弃用警告，26.49 秒。新增 CORS 8/8、CSRF 35/35 行均覆盖；此为局部覆盖率，不能替代完整应用门禁。原始日志、JUnit、覆盖率 XML 与原始数据文件保留，十四文件当前候选为 `probes/cors-policy-o-*`。

截至本轮 V21 单元/API 会话 75854 与完整集成跟踪会话 81087 均已轮询确认仍在运行，不重启。等待其终态并验收后，再将 V22 三方对齐最终 main；当前本地 main 运行代码仍只有已验收 S1，不应用 V21/V22 未验收实现。

续接更新：V21 已验收合入 main `938a7fb`，不再等待其会话。下一步从该主干创建新候选，三方合并 V22 o 的十四文件（尤其保留同一文档的 V21 修改），重核 browser/专项后冻结完整双门禁。

## 完整门禁 q/r 已启动

最新冻结目录 `/tmp/rssripple-v22-rebased-iamb_8_1`，基于已验收 V21 `938a7fb`。三方合并保留全部清理改动；仅测试清单两段末尾追加冲突，已保留双方并更正旧阶段文字。p 78 passed、1 warning、8.97 秒，真实 Chromium p 通过，临时服务 SIGINT 退出 130。十四有效文件、3247 输入冻结，原始证据和清单见 cors-policy-p/q-*。

完整单元/API q 会话 92328，专用 PG rssripple-v22-unit-pg-q（32858）；完整集成 r 会话 63998，项目 rssripple-v22-final-r，依赖 startup 退出 0。复用镜像 sha256:82b848…（依赖未变）。实时句柄/路径和收尾要求见 `probes/cors-policy-q-r-running.json`。不得修改冻结目录或启动重复测试，未验收前保持 TODO。
