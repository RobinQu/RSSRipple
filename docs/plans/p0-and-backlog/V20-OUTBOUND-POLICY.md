# S1：按输入来源约束出站请求

状态：已复核必要性并建立本地复现；独立副本已实现部分连接策略原型，尚未合入 main。副本 `/tmp/rssripple-v20-outbound-policy` 的运行基线为 M2 `756bce6`；M3 合入 `2274768` 后，所测 torrent/poster 两个服务文件没有变化。

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
