# V22 完整门禁前五维审查

候选 `/tmp/rssripple-v22-cors-36t2eojn`，基于已验收 S1 `6bf8c1c`。遵循 code-review-and-quality。此结论允许准备完整门禁，不代表合入；V21 正在验证的副本未改动。

- **正确性**：真实 ASGI 红测与 Chromium 已证明预检/401/来源反射及同站 logout CSRF。CorsFastAPI 仅覆盖 middleware stack 构建，外层标准 CORS 处理完整错误响应，内层来源 guard 在实际路由及认证副作用前拒绝不可信状态变更。同源或白名单只授予来源信任，不绕过认证。真实 OTP Cookie、拒绝退出保留登录、允许退出清 Cookie 均经浏览器验证；本批不声称替代所有 CSRF/浏览器安全机制。
- **可读性**：CORS、来源防护与认证各自有明确职责，未把复杂来源分支塞进 AuthMiddleware；短 origin helper 用标准 URL 解析并比较 scheme/host/实际端口。重复 Origin/Referer 拒绝歧义，错误统一返回 403 FORBIDDEN。
- **架构**：继承 FastAPI 的栈构建保留 router、lifespan、依赖覆盖和 OpenAPI API；避免把导出的 app 换成缺少这些接口的通用 wrapper。共享 Settings 精确来源 validator，不把浏览器白名单混同出站私网授权。应在依赖升级时继续执行 500/流式/代理回归。
- **安全**：默认白名单空，不信任任意 Forwarded/X-Forwarded-Host，API-key 头不作为跳过校验依据。无 Origin/Referer/Fetch Metadata 的程序端兼容是明确边界，非通用 CSRF token 机制；Origin 存在时必须有效可信。允许来源有浏览器读取与写请求权限，运维只能配置可信前端。Cookie 仍 HttpOnly/SameSite=Lax；反向代理需可信地还原外部 scheme/Host。
- **性能**：新增检查无 DB、网络或完整请求体读取；每个状态变更请求解析本身及有限配置来源，不缓冲 SSE/响应。原 GZip、Vary、静态 poster 认证与 API 校验保留。白名单是管理员启动配置，尚无证据需要缓存或额外复杂性。

验证：l 53 项专项通过；m 仓库形式 Chromium 入口实际通过，独立临时 DB 与页面服务正常收尾；n 扩大认证/静态回归 76 passed，middleware 合计 95%，CORS 100%、CSRF 94%（缺失为畸形 URL 捕获路径）；o 补测后 78 passed，CORS 8/8、CSRF 35/35 行均覆盖。这些局部门槛不是完整 ≥95%/≥85% 门禁。仍需对齐最终验收主干、冻结所有测试输入、运行完整双门禁，并审计跳过、正常退出、导出与清理。

重基复核：候选 `/tmp/rssripple-v22-rebased-iamb_8_1` 对齐已验收 V21 `938a7fb`。仅 integration-inventory 追加段冲突，保留清理/CORS 双方内容并更新阶段性过时文字；生产改动逐字节沿用 o 候选。p 78 项通过，真实 Chromium p 通过，临时服务 SIGINT 退出 130。冻结十四文件源码与完整输入清单见 cors-policy-q-*，允许进入完整双门禁，未批准合入。
