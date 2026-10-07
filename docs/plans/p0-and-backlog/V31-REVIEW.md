# V31 最终审查与验收

按 code-review-and-quality 五轴审查正式 i 候选（基线 6548bcc，包含 V30）。英文界面出现中文操作说明已有真实 Chromium 红测，必要性成立，维持 P2；仅对组件拥有的文案做国际化，不改作品/来源/服务端错误的原始内容。

- 正确性：沿用单大括号插值设置；英文 one/other、中文数量提示覆盖 0/1/2。六个真实浏览器流程验证错误回退、原始错误、人工保护及请求载荷，打开后切换语言保留选择。TypeScript、Vite 生产构建、全前端 ESLint 各自退出 0。后端完整 j/k 两道门禁均零失败。
- 可读性：复用 common.search/common.operation，组件专属 key 集中在 metadataRefresh，未增加文案映射框架。浏览器脚本较紧凑，但有固定数据哈希、明确独立断言和失败 DOM 诊断，不构成阻塞项。
- 架构：使用既有 useTranslation，三个测试入口仅由独立 Vite 启动，不进入生产入口；无依赖或 API 协议变更。权威前端文档和测试清单同步。
- 安全：插值仍通过 React 文本渲染，未引入 HTML 拼接。测试拦截全部 API，录制电影身份/标题与合成响应、故障、差异分开说明，不访问外部服务或生产数据库。
- 性能：不增加请求、查询或循环，语言变化只触发现有组件重渲染；不存在新增后台任务。

j 完整单元/API：4183 passed、12 skipped、15 warnings，3199.79 秒，实际退出 0，23632/24399（96.86%）超过 95%。k 完整隔离集成：3861 passed、17 skipped、29 warnings，2698.60 秒，实际退出 0，21817/24399（89.42%）超过 85%。跳过名称/原因/位置与 V30 p/q 完全一致，不把跳过计为通过。两应用退出 0/0，覆盖率/导出退出 0，五份原始覆盖率、四份语料报告与 JUnit 已归档；两个唯一 Compose 项目均已清理且容器/网络/卷为空。

6519 个冻结输入未变，独立 Git 推导有效范围为 8 文件，复制后的全部 3309 个非计划输入与候选哈希一致。生成的前端 bundle 单独作为证据存档，源码候选保留既有 static 文件；Dockerfile 的 frontend-builder 会重新执行 pnpm build 并拷贝产物，本次没有部署。预检时 pnpm 不在 PATH，使用已安装的同一 Node 工具入口，未改变依赖。浏览器关闭后 Vite 被有意 SIGINT 停止，PTY 实际退出 1，与测试退出 0 分别记录。

未发现阻塞项，批准按这 8 文件范围合入本地 main。提交后核验 Git 实际变更范围、每个文件对象及全部 3309 个非计划输入。证据见 probes/metadata-refresh-i18n-j-audit.json、metadata-refresh-i18n-k-audit.json 与 metadata-refresh-i18n-j-k-accepted.json。不推送或部署。
