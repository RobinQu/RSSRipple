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
