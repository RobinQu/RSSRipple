# Filter DSL 规范

Agent 的 `filter_config` 和 AgentWork 的 `filter_overrides` 均使用统一的布尔查询 DSL，类 Elasticsearch bool query 结构。

### 类型定义

```
FilterConfig = BoolCondition

BoolCondition = {
  "combinator": "and" | "or",
  "conditions": Array<BoolCondition | FieldCondition>,
  "is_not": bool?   // 可选，对整个条件组取反，默认 false
}

FieldCondition = {
  "field": "subtitle_group" | "resolution" | "source" | "video_codec" |
           "audio_codec" | "subtitle_type" | "container" | "file_size" |
           "episode" | "season" | "episode_start" | "episode_end" |
           "absolute_episode" | "is_batch" | "subtitle_langs" |
           "episode_confidence" | "content_type" |
           "title_cn" | "title_en" | "search_title" |
           "movie.rating" | "movie.year" | "series.rating" | "series.year" |
           "movie.collection" | "series.collection" | "collection" |
           "series.genre" | "movie.genre" | "series.is_anime" | "movie.is_anime",
  "operator": "eq" | "ne" | "contains" | "fuzzy" | "in" | "regex" |
              "gt" | "gte" | "lt" | "lte" |
              "is_empty" | "is_not_empty",
  "value": string | number | boolean | string[]
           // is_empty / is_not_empty 不需要 value，key 可省略
}
```

### 求值语义

- **BoolCondition**：
  - `combinator="and"`：`conditions` 中所有子条件均通过时，本组通过。
  - `combinator="or"`：`conditions` 中任一子条件通过时，本组通过。
  - `is_not=true`：对最终结果取反。
- **字段类型与 operator 支持**：
  - 数字字段（`file_size`, `episode`, `season`, `episode_start`, `episode_end`, `absolute_episode`）支持：`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`。
  - 关联作品字段（`movie.rating`, `movie.year`, `series.rating`, `series.year`）是带命名空间的数字字段，取值来自资源关联的 Movie/TVSeries，支持的操作符同数字字段：
    - `rating`：作品评分，0-10 量程（TMDB `vote_average` 等来源，见 data-models.md）。
    - `year`：作品年份，由 `Movie.release_date` / `TVSeries.start_date` 取年份派生。
    - 资源未关联作品（或关联关系未加载）时字段值为空，适用空值语义：`gte 7` 不通过、`ne 7` 通过、可用 `is_empty`/`is_not_empty` 显式匹配。
    - 求值路径（rules-preview、test-filter、Agent 运行、回填提交）查询 FileResource 时必须 `selectinload` `series`/`movie` 关系，异步会话禁止触发 lazy load；未加载的关系按"无关联作品"处理。
  - 合集字段（`movie.collection`, `series.collection`）是带命名空间的字符串字段，取值为作品所属 WorkCollection 的显示名（`title_cn or title_en`），支持字符串字段操作符：
    - 解析链有两层：资源的 `series`/`movie` 关系 + 作品的 `collection` 关系，任一未 eager-load（或未关联）时值为空，适用标准空值语义（`eq` 不通过、`ne` 通过、空值匹配用 `is_empty`/`is_not_empty`）。
    - 所有求值过滤的查询点必须链式加载 `.selectinload(FileResource.series).selectinload(TVSeries.collection)`（movie 同理），否则过滤静默误评。已知求值点：Agent 派发（main.py 后台任务）、回填提交、rules-preview、test-filter、PendingDecision LLM 重选。
  - 资源级合集字段（`collection`，无命名空间）是字符串字段，取值为资源**直接**关联的 WorkCollection 显示名（franchise 多作品包的 `resource.collection_id`，见 data-models.md「合集资源识别」），操作符与空值语义同上；求值点须 `selectinload(FileResource.collection)`。
  - 布尔字段（`is_batch`, `series.is_anime`, `movie.is_anime`）支持：`eq`, `ne`。value 接受原生 bool、数字 `1/0`、字符串 `"true"/"false"/"yes"/"no"/"1"/"0"`。
    - `series.is_anime` / `movie.is_anime` 是带命名空间的三态布尔字段，取值来自资源关联的作品（True=日本动画 / False=确认实拍 / NULL=未判定，见 data-models.md「is_anime 判定约定」）。bool 空值语义与标量空值规则一致：值为 NULL 时正取值操作符（`eq`）不通过、`ne` 通过；区分「确认非动漫」（False）与「未判定」（NULL）必须用 `is_empty`/`is_not_empty`。（此前 bool 字段 NULL 被当 False 求值，已修正为与本文档记载的空值语义一致；`is_batch` 为 NOT NULL 不受影响。）
  - 列表字段（`subtitle_langs`）支持：`eq`, `ne`, `contains`, `in`。
    - `contains`：value 是单个 tag，若列表元素包含该 tag（大小写不敏感）则通过。
    - `in`：value 是 tag 数组，任一元素在列表中即通过。
    - `eq` / `ne`：视为**集合相等/不等**（忽略顺序）。
  - genre 字段（`series.genre`, `movie.genre`）是带命名空间的列表字段，取值为资源关联作品的 `genre`（封闭 TMDB 27 类英文 canonical 名，见 data-models.md「genre 取值约定」），支持操作符与逐元素语义同列表字段；资源未关联作品时值为空，适用标准空值语义。
  - 枚举字段（`episode_confidence`）在存储层是普通字符串，走字符串字段求值路径；UI 限制取值为 `"raw" | "reconciled" | "ambiguous" | "manual"`。
  - 作品类型字段（`content_type`）是派生枚举字符串字段：值由资源互斥的作品 FK 派生——`series_id` 非空 → `"tv"`、`movie_id` 非空 → `"movie"`、`audio_work_id` 非空 → `"audio"`、三者皆空（未识别）→ 空值（适用标准空值语义，`is_empty` 可匹配「未识别」）。走字符串字段求值路径；UI 限制取值为 `"tv" | "movie" | "audio"`。该派生只读 FK id，无需 eager-load 作品关系，所有求值点均安全。
  - 字符串字段（其余全部）支持：`eq`, `ne`, `contains`, `fuzzy`, `in`, `regex`。
- **operator 语义**（字符串比较均忽略大小写）：
  - `eq`：字段值等于 value（字符串去首尾空格后比较）。
  - `ne`：字段值不等于 value。
  - `contains`：字段值包含 value 子串。
  - `fuzzy`：使用 thefuzz `fuzz.ratio` >= 70 判定为匹配。
  - `in`：value 为字符串数组（或逗号分隔字符串拆分为数组），字段值命中任一元素（子串匹配，等价于多值 OR contains）。
  - `regex`：用 `re.search(pattern, field_value, re.IGNORECASE)` 匹配。
  - `gt/gte/lt/lte`：数值大小比较。
- **空值操作符**：`is_empty` / `is_not_empty` 对所有字段类型可用，不需要 `value`（key 可省略）。空定义为 `None`、空白字符串或空列表；数字 `0` 与布尔 `false` 不算空。匹配空字段必须用这两个操作符，**禁止**用 `eq ""` 表达。
- **空值处理**：若字段值为 None/空：
  - 对于 `is_required` 语义由 DSL 外层决定——即空值时 `eq/contains/fuzzy/regex/gt/...` 判定为不通过；`ne` 判定为通过。
- **非空值校验**：所有取值操作符（`eq/ne/contains/fuzzy/in/regex/gt/...`）的 `value` 在保存时（`validate_filter_config`，Agent 创建/更新接口）必须非空——空字符串、纯空白、null 一律返回 422。原因是 `eq ""` 这类条件对任何资源都不通过（正操作符遇空值失败，非空值又不等于 `""`），保存即静默过滤掉全部资源。校验只拦截保存，存量配置的求值语义不变。
- **合并规则**：AgentWork 的 `filter_overrides` 若存在，则与全局 `filter_config` 按 AND 包装：
  ```json
  { "combinator": "and", "conditions": [agent.filter_config, work.filter_overrides] }
  ```
  若其中任一为 null，则直接使用另一个；两者均为 null 视为全部通过。

### 示例

**示例 1**：字幕组必须是"XX字幕组"或"YY字幕组"，且分辨率为 1080p 或 2160p。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "subtitle_group", "operator": "in", "value": ["XX字幕组", "YY字幕组"] },
    { "field": "resolution", "operator": "in", "value": ["1080p", "2160p"] }
  ]
}
```

**示例 2**：字幕组包含"动漫"且文件大于 1GB，或字幕组等于"官方"且分辨率为 2160p。

```json
{
  "combinator": "or",
  "conditions": [
    {
      "combinator": "and",
      "conditions": [
        { "field": "subtitle_group", "operator": "contains", "value": "动漫" },
        { "field": "file_size", "operator": "gte", "value": 1073741824 }
      ]
    },
    {
      "combinator": "and",
      "conditions": [
        { "field": "subtitle_group", "operator": "eq", "value": "官方" },
        { "field": "resolution", "operator": "eq", "value": "2160p" }
      ]
    }
  ]
}
```

**示例 3**：排除 MKV 以外的容器，且视频编码不是 AVC。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "container", "operator": "eq", "value": "mkv" },
    { "field": "video_codec", "operator": "ne", "value": "AVC" }
  ]
}
```

**示例 4**：只要单集（排除合集）。

```json
{ "field": "is_batch", "operator": "eq", "value": false }
```

**示例 5**：只要覆盖 8 集以上的合集。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "is_batch", "operator": "eq", "value": true },
    { "field": "episode_end", "operator": "gte", "value": 8 }
  ]
}
```

**示例 6**：只要含简体或繁体中文字幕。

```json
{ "field": "subtitle_langs", "operator": "in", "value": ["zh-CN", "zh-TW"] }
```

**示例 7**：必须同时含简体和日文字幕。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "subtitle_langs", "operator": "contains", "value": "zh-CN" },
    { "field": "subtitle_langs", "operator": "contains", "value": "ja" }
  ]
}
```

**示例 8**：排除集号 ambiguous 的资源（仅下载 raw / reconciled / manual）。

```json
{ "field": "episode_confidence", "operator": "ne", "value": "ambiguous" }
```

**示例 9**：只要还没解析出字幕组的资源。

```json
{ "field": "subtitle_group", "operator": "is_empty" }
```

**示例 10**：只要评分不低于 7 的作品（电影或剧集均可；未关联作品的资源不通过）。

```json
{
  "combinator": "or",
  "conditions": [
    { "field": "movie.rating", "operator": "gte", "value": 7 },
    { "field": "series.rating", "operator": "gte", "value": 7 }
  ]
}
```

**示例 11**：只要 2020 年及以后的剧集。

```json
{ "field": "series.year", "operator": "gte", "value": 2020 }
```

---

