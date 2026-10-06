# S1 完整门禁前审查

审查基线：已验收 main 运行代码 `b0e2ce1`；候选 `/tmp/rssripple-v20-rebased-rc0rjzc5`。本记录允许进入完整测试，不代表验收或合入。应用 code-review-and-quality 五维标准。

- **正确性**：连接层检查全部解析结果后仅向底层传递数值 IP，原 Host/SNI 和 TLS 校验保留；同步/异步使用相同目的地规则。首次请求及重定向均受约束。RSS 先 HTTP 抓取后解析字节，管理员 Basic auth 同源保留、跨源剥离。镜像仍走 infohash 检验。实际 SDK 构造器请求、普通/流式调用、RPC 409 协商有真实网络测试，旧 mock 的生命周期适配不删除原业务断言。
- **可读性**：策略集中在 outbound_http，服务只声明管理员初始 origin；同步/异步传输分开适配，共享 origin/IP 判定。Transmission 使用实例 hook，不修改全局 Requests。未将配置私网来源和外部派生 URL 混为一类。
- **架构**：HTTPX 的 _pool 是受约束的私有接点，httpcore 使用公开 network_backend；精确固定已有版本 0.28.1/1.0.9。Transmission 7.0.6 的私有 _http_query 接点由真实 SDK 协议测试保护。依赖锁没有升级包，httpcore 从已有传递依赖改为显式依赖。独立测试镜像按锁文件重新构建，Compose 可用 RSSRIPPLE_TEST_IMAGE 选择独立标签。
- **安全**：拒绝非公网/混合 DNS 结果、数字地址变体及 IPv6 转换/隧道范围；每个私网例外为管理员配置的精确 origin，不接受通配/路径/用户信息。关闭受控客户端环境代理，保留企业 CA。RPC 重定向不得离开配置 origin；未把此规则扩展为禁止合法管理员内网服务。ag 十项真实 HTTP/Turso 验证 Plex/Emby/Jellyfin/Wigolo/webhook 现有直连及不跟随重定向行为；没有修改这些正常路径。Wigolo 的空 307 响应目前抛 JSONDecodeError，本批不宣称统一了其错误分类。
- **性能与生命周期**：解析线程上限 4、未完成查询上限 8，超时或取消不释放仍在运行的 OS 查询槽；避免无界后台 DNS。短生命周期 SDK 用上下文释放连接；长生命周期 LangChain 模型不保留空闲连接，旧在途请求可完成。Transmission hook 不捕获 client，避免引用环。OS DNS 无法被强行取消，已明确其有界存续行为。

证据：t–ae 红/绿日志与候选归档；ad 136 passed；af 448 passed、2 项既有 live swarm opt-in 跳过；ag 10 passed。录制 torrent 和 magnet XML 保持原样，模型响应、服务协议、证书与通知外壳明确合成。全仓 Ruff 通过。S3 静态构建 39 个文件与原冻结清单逐项哈希一致，沿用既有产物；本批无前端源代码修改。

仍需完整单元/API ≥95%、隔离集成 ≥85%、零失败；核对跳过理由、冻结输入、应用正常退出、四路覆盖率、导出与唯一项目清理。任何失败均保留并修正后重新满足对应门禁，不能用局部通过替代。未满足前不得从 TODO 删除 S1。

## ah/ai → aj/ak 门禁复核

ah 单元/API 4078 passed、12 skipped、97.11%；ai 的两项失败已保留。复核 aj 与 ah 全部冻结输入，仅 API 文档和三个集成测试文件变化：明确区分成功空 feed 与抓取失败，修复 mock 的参数兼容，并新增真实网络/生产路由证据；运行实现未为了旧测试退回吞错。因此复用 ah 单元门禁合理。ak 完整集成 3280 passed、17 skipped、88.33%，退出 0；3099 输入未变，跳过原因完全一致，两路应用正常退出，四路覆盖率合并及导出成功。最终部署仍保留 OS DNS 无法取消、HTTPX/Transmission 私有接点依赖固定版本这两项已文档化边界。

最终复核：逐项检查出站传输、所有生产接入差异及证据范围，未发现阻断项。main 的 37 个目标文件仍匹配候选基线；复制后逐字节匹配已测试候选。全仓 Ruff 与 diff --check 通过。五维结论：批准本地合入；不推送或部署。
