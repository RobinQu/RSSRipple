# S1：按输入来源约束出站请求

状态：已复核必要性并建立本地复现；独立副本已实现部分连接策略原型，尚未合入 main。副本 `/tmp/rssripple-v20-outbound-policy` 的运行基线为 M2 `756bce6`；M3 合入 `2274768` 后，所测 torrent/poster 两个服务文件没有变化。

最新续修目录：`/tmp/rssripple-v20-validation-h9wb61g5`，基于已验收 main 运行代码 `b0e2ce1`。aj 候选 37 有效文件、3099 冻结输入；源码、归档和冻结清单见 `probes/outbound-policy-aj-*`。完整单元/API ah：4078 passed、12 skipped、97.11%；完整集成 ak：3280 passed、17 skipped、88.33%，均退出 0。冻结输入未变，跳过理由无变化，两路应用退出 0，覆盖率与证据导出退出 0。尚待最后合入审查，未合入 main。机器证据见 `probes/outbound-policy-aj-ak-audit.json`；下文旧副本与未完成状态均为历史记录。

ai 首次完整集成 3274 passed / 2 failed / 17 skipped，覆盖率 88.27%，不能验收。两处失败分别为不可达 feed 旧用例期待 200 空结果（现有 API 的抓取失败契约为 400 FETCH_ERROR）、镜像 mock stream 不接受 auth 参数。aj 修正这两处测试并增加真实 HTTP→生产预览路由的有效 feed、成功空 feed、HTTP 失败和禁止跨源四类测试，专项 73 passed。仅三个集成测试文件及 API 文档相对 ah 改变，应用、单元/API、配置、依赖和夹具完全相同，因此复用 ah 完整单元门禁。ai 红测、清理退出及跳过审计保留在 `probes/outbound-policy-ah-ai-*`。

## 已证实的范围

a 正式候选测试文件 `tests/integration/security/test_untrusted_outbound.py` 四项均失败（0.56 秒）：torrent/poster 分别直接访问回环地址，以及从另一临时端口经 302 访问回环目标，均发生请求并缓存返回内容。两个服务器都由测试在 127.0.0.1 随机端口启动并 finally 停止，不访问公网、云 metadata 地址或真实内网服务。

torrent 返回体为未改写的已录制文件 `tests/fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent`，sha256 与文件名一致；poster 为明确合成的一像素 PNG。URL、重定向和“私有服务”均是受控夹具。该证据证明生产下载/海报函数缺少目的地址限制，不证明 DNS 重绑定、公网入侵或所有管理员配置入口都存在同一缺陷。

当前重定向红测的首跳也是回环地址，未来只拒绝首跳便可能使它通过；因此它不能替代“明确允许首跳，但禁止跳往其他私网端点”的逐跳验收。

## 输入来源与兼容性复核

| 路径 | 地址来源 | 处理要求 |
| --- | --- | --- |
| torrent 文件、metadata 海报 | feed/抓取/外部元数据派生，可能被外部内容控制 | 默认只允许可验证公网 HTTP(S)；每跳及实际连接均检查；内网资源需运维明确配置例外 |
| Channel RSS URL、用户提交的分析 URL | 管理员明确配置或提交的初始目标 | 保留合法内网订阅；授权只覆盖初始 origin，不可经重定向访问其他私网目标；不把原始 URL 直接交给支持本地文件读取的解析器 |
| magnet 镜像模板 | 管理员配置，替换变量为已校验 infohash | 初始目标可明确授权，重定向需受约束；保留现有内容 infohash 校验 |
| downloader、media-server、webhook、Wigolo、LLM base URL | 管理员配置的基础设施端点 | 保留明确配置的内网服务；检查 SDK 的重定向及凭证行为，不能因“允许私网”就判定为缺陷，也不能把信任传给返回内容中的任意 URL |
| 固定 Wikipedia/TMDB/Bangumi/Wikidata 端点 | 固定提供者地址加受限参数 | 核对是否有动态 URL 或重定向逃逸；不能把查询字符串等同于任意网络地址 |

代码复核发现 notify/media-server/Wigolo 的当前 HTTPX 构造并没有启用自动重定向，原清单的广泛表述不能代替逐个调用点证据。RSS 的 `feedparser.parse(url)` 则拥有自己的网络/文件处理路径；必须纳入同一来源边界，不能只修改两个已复现 helper 就关闭 S1。

## 修复方案与论证

在明确拥有此职责的出站 HTTP 模块中集中策略，不在每个服务中复制 IP 黑名单。区分不可信派生 URL 与管理员授权的初始 origin；origin 按 scheme、规范化 host、实际 port 精确匹配，不使用字符串后缀匹配或整个私网段默认放行。可配置例外只能由管理员定义，不能由 feed、metadata 或重定向响应自行扩大。

连接时解析地址并检查实际 IP 集合，拒绝回环、私有、链路本地、未指定、多播及其他非公网目标；补齐 IPv6/IPv4 映射与特殊地址形式测试。仅预解析后交给普通客户端再解析会产生 DNS 检查与连接的时间差，不能作为修复。受控连接层应拨号到已经验证的数字地址，同时保留原 Host、TLS SNI 和证书验证；逐跳重新约束 URL/连接，不能让环境代理绕过检查。来源故障或禁止地址必须失败关闭，不得改走无保护客户端重试。

本地锁定依赖为 httpx 0.28.1 / httpcore 1.0.9，已检查安装源码：ConnectionPool 提供 network_backend，SyncBackend.connect_tcp 接收 host/port；HTTPX HTTPTransport 负责请求/流适配。后续优先复用适配层，避免复制整套客户端；如果涉及库内部接点，必须将连接固定、HTTPS/重定向及版本兼容验证纳入门禁。当前仅完成可行性检查，尚未实现或证明安全 transport。

保留原下载大小限制、torrent 内容校验、流式读取、超时和缓存行为。海报残缺缓存仍是独立 P2，不以本批网络限制替代其修复。

## 严格验收标准

1. 已复现四项必须通过，且未发生私网请求、未生成缓存；另加明确允许的首跳向不同私网端点重定向，证明逐跳约束有效。
2. DNS 私网、混合公私地址、改变解析结果、IPv6/映射/非标准数字 host 等矩阵；检查传入底层连接的地址已固定，不能以仅 mock policy 返回 True 代替连接证据。
3. 公网目标成功、Host/SNI/证书校验保持、流式大小上限、正常重定向、连接复用与超时；本地受控 DNS/拨号替身与真实 HTTP/TLS 的边界要明确标注。
4. 管理员明确配置的内网 RSS/downloader/media-server/webhook/Wigolo/LLM 兼容性，以及重定向不得把凭证和信任传到新目标；按实际调用点建立覆盖表，不能用一个 helper 的单元测试代表全部出站路径。
5. 对原录制 torrent/feed 数据进行端到端回放，保留原始文件；恶意 URL 变体必须注明合成。新增配置、API 行为、错误处理、业务流程和部署约定同步到权威文档。
6. 每批完整单元/API ≥95%、隔离集成 ≥85%，零失败，跳过/冻结哈希/退出/清理审计完整。所有测试 Compose 使用唯一项目名。未完成所有适用路径前 S1 保留 TODO。

## b/c 连接层原型验证

b 15 项通过（2.74 秒）；增加 HTTPS 证据后 c 18 项通过（3.46 秒），Ruff 通过。候选仅四个有效文件：新增 `app/clients/outbound_http.py`、torrent/poster 两个接入点、正式集成测试文件。候选归档为 `probes/outbound-policy-c-candidate.tar.gz`，文件哈希见同前缀 source JSON。未在 main 修改运行实现，不能视为完整验收。

原型通过 HTTPX 现有适配层与 httpcore 的 network_backend 接点，在实际建连时解析并检查整个 DNS 答案，再向底层传入数字地址。HTTP/HTTPS 分别约束 origin，私网例外不跨 scheme/host/port 继承；禁用环境代理。原始四项红测变绿，额外证明允许的私网首跳不能跳转到另一个未授权私网 origin，而显式授权目标本身可以成功。

DNS 矩阵覆盖回环、RFC1918、链路本地、IPv6 回环/映射/NAT64/6to4、多播和混合公私结果，均在拨号前拒绝。公网成功路径只替换 DNS 和数字地址拨号映射到本地服务器，不接触真实公网；真实 HTTP 请求成功且原 hostname 不被再次解析。TLS 三项用临时合成证书和真实握手证明保留原 Host/SNI、拒绝未信任证书与 hostname 不匹配。录制 torrent 字节继续保持原样；不能把受控公网 IP 映射称为真实公网验收。

复审尚待解决：其余 RSS/镜像/异步调用路径与配置例外未接入；现有基于 HTTPX 构造器的测试需迁移到受控 HTTP 边界，避免绕过策略；DNS 本身的阻塞超时仍需评估；企业 CA/代理部署迁移策略未定；HTTPX `_pool` 接点和直接使用 httpcore 的依赖声明必须在最终方案中明确。URL 内嵌凭证当前一律拒绝，管理员 RSS basic-auth 的兼容方式需在接入前论证。尚无完整覆盖率门禁，S1 继续待办。

## d/e 成功回放与数字地址扩展

增加生产 `fetch_torrent_file` / `download_and_cache_poster` 完整调用：受控公网 DNS/拨号映射下通过真实 HTTP 拉取、校验并缓存，torrent 输出 sha256 等于原录制文件名。另加 `127.1`、十进制整型、十六进制与八进制写法，均断言底层未拨号。d 23 passed / 1 failed（3.62 秒）：八进制形式被 HTTPX URL 解析层提前以 InvalidURL 拒绝，测试错误地只允许策略异常；调整为接受两种拒绝类型，保留无拨号断言，e 24 passed（3.95 秒）。运行实现未修改，c/d/e 的差异仅在测试。最终原型/哈希及完整失败日志保存在 `outbound-policy-e-*` 与 d/e 日志，c 归档保留。其余调用路径和完整门禁仍未完成。

## f–j：RSS 与缓存镜像必要性复核及原型

f 2 passed / 3 failed（0.45 秒）：使用既有 `tests/fixtures/magnet_feed.xml`，现有 RSS 正常支持管理员内网直连和同源重定向，但也会经 302 访问另一个私网 origin，并接受本地路径及 file URI、读出三条资源。夹具 XML 未改写；标题/磁力链接来自已记录生产行，XML 包装本身是既有测试构造，不宣称完整线上 RSS 原始响应。HTTP 服务器、URL 和临时文件均为受控测试数据。

修订 `_parse_feed_sync`：先受控 HTTP 抓取，再把响应字节交给 feedparser；保留最终 content-location 与内容类型供解析相对链接和编码。管理员明确输入的初始 origin 可访问内网，但授权不继承到其他 origin；URL 内嵌 Basic auth 拆成请求认证，传输/解析 URL 不再含凭证。同源跳转保留认证；用真实 HTTP 和受控公网 DNS/拨号证明跨源不转发。g 46 passed（2.38 秒）；保留原 feedparser User-Agent 后 h 46 passed（2.27 秒）。拒绝测试从初始复现的“结果不可用”加强为明确 DestinationDenied，且跨私网场景确认首跳发生、目标未收到请求。

i 镜像矩阵 2 passed / 2 failed（0.56 秒）：正确/错误 infohash 的私网直连行为符合原契约，但两种情况都会先沿重定向触达另一私网目标；正确 hash 还会落盘。镜像 helper 改为同一受控客户端，仅授权管理员模板生成 URL 的初始 origin，保留字节上限与后续结构/infohash 校验。j 组合 50 passed（2.65 秒）；之后只修正 import 排序并同步文档，Ruff 通过。候选十个有效文件及哈希为 `outbound-policy-j-candidate.tar.gz` / `outbound-policy-j-source.json`；完整 f–j 失败/通过日志与结果均保留。

下一步复核发现：安装版本的 transmission-rpc 在构造器中即调用 `get_session()`，内部 Requests Session.post 未禁用重定向；OpenAI SDK `_base_client.py` 的默认 HTTPX 客户端显式 follow_redirects=True。两者必须验证真实 SDK 行为及凭证/目的地边界，不能只审核项目中的 HTTPX 构造器。Wiki REST summary 的异步重定向也未接入。notify/media-server/Wigolo 当前不自动跟随重定向，仍需实际兼容性证据。原有 magnet 集成使用本地文件路径回放的夹具必须迁移到 HTTP 服务；不为旧测试恢复生产文件读取。管理员私网资源例外、代理/CA 策略、直接 httpcore 依赖声明、DNS 阻塞上限与完整门禁仍待完成。S1 不关闭、不合入。

k 真实 SDK 验证：通过生产 `TransmissionWrapper.test_connection` 与 `feed_analyzer._call_openai`，本地协议模拟器两项直连通过，两个 307 跨私网 origin 用例失败（2 passed / 2 failed，0.73 秒）。目标确实收到 SDK 发出的请求；LLM 的跨源 Authorization 已剥离，但合成请求体仍被发送，不能把没有凭证泄露误判成没有目的地问题。没有模拟或替换 SDK 的 HTTP 方法；账号、响应和请求内容均为合成。完整测试原文及日志见 `outbound-policy-k-*`。尚未修改 SDK 运行实现，后续需选择支持安全连接/重定向约束的注入点，禁止全局 monkeypatch SDK 或 Requests Session。

## l–s：真实 SDK 接入与新 main 基线

新增受控异步 backend/transport，共用 IP 判定，异步 DNS 等待计入连接预算；HTTPX 请求/流/异常适配继续复用。普通 OpenAI 调用通过 SDK 的 http_client 参数注入受控客户端，并使用 async context 关闭资源。l 两项真实 SDK 通过（2.08 秒）。同步/异步 DNS 拒绝矩阵和真实 TLS 六项一起验证；m 49 passed（7.48 秒），当时尚未修 Transmission 的两项明确 deselect，不能当全量验收。

n 继续验证 SSE 路径，1 passed / 1 failed（0.58 秒）：普通调用修复没有覆盖流式调用，跨私网目标仍收到请求。流式调用改用同一策略、保留 delta/reasoning/解析/重试语义并关闭 SDK 后，o 四项普通/流式实际 SDK 测试通过（12.15 秒）。现有有界重试仍保留，策略拒绝不会导致目标被访问。

Transmission 方案：其构造器立即发出 RPC，公开参数不提供自定义 session；本地 SDK 子类仅覆写 `_http_query`，在首次请求前为该实例安装 response hook，拒绝跨 origin 重定向并关闭被拒绝响应，不修改全局 Requests。RPC 与资源下载不同：它发送管理请求，重定向仅保留配置的 scheme/host/port，即便跳往公网也不能转交；HTTP→HTTPS 需直接配置最终端点。同源重定向和原生 409 session-id 协商必须保留。p 七项真实 SDK 通过（12.36 秒），后续增加 409 断言，SDK 专项共八项。

q 组合 144 passed / 20 failed（12.06 秒）：真实网络专项通过，20 项旧 OpenAI mock 的 `__aenter__` 返回了另一个未配置 mock，导致原响应/重试断言失败。只更新夹具为真实 SDK async context 约定，保留全部原业务断言；r 164 passed（11.48 秒）。q 的完整失败日志、l–r 的所有报告及旧候选归档均保留。

之后三方合并到已验收 main `b0e2ce1`。仅 integration-inventory 顶部发生文字冲突，保留 S3 绑定与 S1 出站两节；运行代码无冲突。新目录 s 再跑同一组合 164 passed，14 个源文件哈希不变。后续实现应使用新目录，不继续在旧 M2 副本开发。

剩余项：batch_content_analysis 的普通/流式 OpenAI、metadata_agent 的 LangChain 模型、OpenRouter SDK 和 Wiki REST 异步跳转仍需逐路径论证/验证；配置私网资源例外、企业代理/CA 与同步 DNS 超时策略尚未定案；httpcore 直接依赖、HTTPX/Transmission 私有接点须明确约束与兼容测试；旧本地文件 feed 夹具和相关客户端 mock 须迁移。最后必须在完整新基线执行 ≥95%/≥85% 两道门禁、零失败与完整审计，才可关闭 S1。

## t–af：补齐 LLM/Wiki 路径、连接边界与旧测试适配

必要性续验覆盖实际 SDK：u 的四个直连控制通过，batch 普通/流式与 LangChain 同步/异步四个跨私网重定向失败，目标实际收到请求；t 的部分失败来自误用 `path` 而非 `name` 的文件夹具，不能用作漏洞证据。改用录制 torrent `987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent` 的生产解析文件表，接入受控客户端后 v 八项通过。w 再证实 OpenRouter 普通/流式两条路径越界（2 passed / 2 failed）；SDK 注入并尊重配置的 server_url 后 x 20 项通过。y 的 Wiki pageimages→REST summary 路径 1 passed / 1 failed，修复 REST 跳转后 z 网络组合 69 项通过。模型输出、HTTP 服务及证书为合成，保留真实 HTTP/TLS 与 SDK 调用，不宣称访问真实模型服务。

方案补充：`OUTBOUND_PRIVATE_ORIGINS` 仅接受精确 HTTP(S) origin，默认无例外；不接受凭证、通配、路径/查询/片段，默认端口规范化。aa 暴露大写 scheme 配合默认端口未归一化，修正后 ab 57 项通过，含同步/异步 SSL_CERT_FILE 可信 CA 正例。受控客户端仍关闭环境代理；没有使用 verify=False。DNS 共用最多四线程和八个未完成查询，响应超时不释放尚在执行的系统查询槽；ac 62 项通过，包含同步/异步超时与容量边界。依赖锁仅明确 HTTPX 0.28.1/httpcore 1.0.9 的现有版本，无包升级。MetadataAgent 零空闲连接池避免配置重置后遗留空闲连接，同时允许在途调用完成。

ad 在 Transmission hook 消除 self 引用环后再次验证实际 SDK，并适配旧 batch/feed mock 的 async context 生命周期，136 passed、1 warning、47.13 秒。ae 扩大到 torrent、magnet、MetadataAgent、RSS 和集成覆盖，443 passed / 5 failed / 2 skipped、84.65 秒：四项是测试迁移漏加 HTTPX 导入，一项是镜像 mock 不接受新增 auth 参数；均保留失败日志。magnet E2E 现以本地 HTTP 回放未改写的既有 XML，首轮两个离线端到端用例通过；两项 live swarm 用例保持原 opt-in 条件。修正 mock 后 af 重跑相同范围：448 passed、2 skipped、7 warnings、77.71 秒，退出 0；两项仍为原 live swarm opt-in 跳过。全仓 Ruff 通过。隔离 Compose 已为测试资源明确配置 `http://test-server:8080`，没有放宽生产默认策略。

当前 31 文件候选及主干基础哈希见 `probes/outbound-policy-ae-source.json`，可从同名前缀 candidate.tar.gz 恢复；t–ae 原始日志/JUnit 均以 gzip 留存，结果见 `outbound-policy-t-ae-results.json`。main 运行代码未变，S1 未验收、未合入。af 结束后 31 文件候选哈希与归档一致，终态见 `probes/outbound-policy-af-result.json`。下一步补非重定向 webhook/media-server/Wigolo 的真实请求兼容性证据，检查完整集成环境与剩余旧 mock，然后执行完整单元/API ≥95%、隔离集成 ≥85%（零失败、跳过审计、冻结哈希、应用退出、覆盖率导出、唯一项目清理）。局部通过不能替代这两道完整门禁。


## ag–ai：管理员端点复核与完整门禁

ag 的 10 项真实 HTTP/Turso 用例全部通过（1.51 秒）：Plex/Emby/Jellyfin/Wigolo 的内网直连和认证保持正常，307 不触达第二端点；持久 webhook 使用已录制 torrent 的解析文件列表与合成通知外壳，正常投递 done，重定向拒绝后 pending 且计数/退避持久化。未改动这些已正常的生产路径。Wigolo 的空 307 响应当前在 JSON 解析时报错，仅证明目的地边界，不声称其错误分类统一。

[门禁前五维审查](V20-REVIEW.md) 已完成，全仓 Ruff 和隔离 Compose 配置校验通过。依赖镜像按候选锁文件重新构建为独立 `rssripple-v20-tests:local`，退出 0；Compose 新增可选 RSSRIPPLE_TEST_IMAGE，保留原默认值，避免本轮构建覆盖其他测试镜像。沿用 S3 构建的静态文件，39 项哈希与原冻结清单一致。代码与集成标准同步写入候选权威文档。

ah 完整单元/API 已启动（session 8726，日志 `/tmp/rssripple-v20-unit-ah.log`），门槛 ≥95%。专用 PostgreSQL `rssripple-v20-unit-pg-ah` 使用回环随机端口 32850、tmpfs、--rm；真实迁移测试不依赖生产数据库。ai 完整隔离集成已启动（session 38311，日志 `/tmp/rssripple-v20-integration-ai.log`），唯一项目 `rssripple-v20-final-ai` 启动退出 0，门槛 ≥85%。当前两会话均确认存活，未出现终态；不得修改被冻结的候选或提前清理服务。后续必须记录完整终态、跳过差异、哈希、应用退出、覆盖率汇总/导出和项目清理，才可判断合入。
