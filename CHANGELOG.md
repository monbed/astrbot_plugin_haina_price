# 更新日志

本项目的所有重要变更都会记录在此文件中。

## [1.2.2] - 2026-11-07

### 修复

- 图搜商品不可用：AstrBot 收到的图片通常保存为本地临时文件，而海纳图搜只接受公网 `img_url`。现在消息（或引用消息）中的图片会先还原为字节，匿名上传到图床换取公网链接后再识别；模型直接提供公网图片链接时仍直接使用。
- 接口返回 HTTP 4xx/5xx 时在日志中记录响应内容，便于排查。
- 图搜商品返回 HTTP 500：`is_crop`、`is_auto_first_cate_name` 改为仅在开启时传 `"True"`，关闭时不传；配置「开启主体识别」默认值改为关闭。
- 商品检索 Pro：商城、品牌、品类、价格区间、排序等可选参数只在有值时传，不再发送空字符串或 `null`；商品检索与好价检索不再发送空的 `fields`。
- 内容检索：`return_size` 上限按文档调整为 100。

### 变更

- 默认参数改为海纳文档示例值：商品检索返回 20 条、图搜返回 2 个且默认不返回关联好价（`with_haojia=0`）、内容检索返回 20 条、内容检索 Pro 返回 50 条。
- 新增配置 `web_size`（内容检索 Pro 默认返回条数），`content_size` 只用于内容检索。
- 修正「接口超时时间」提示：内容检索固定至少 60 秒。

### 新增

- 配置 `image_allow_upload`（允许上传图床）、`image_host`（litterbox / uguu / catbox）、`image_upload_proxy`（上传代理）。

## [1.2.0] - 2026-11-06

### 新增

- 自带技能 `skills/haina-shopping-assistant`（海纳购物管家）：意图解析 → 查询改写 → 检索 → 按模板输出，含推荐/总结/对比/好价 4 套模板与基本原则；参考公开的同名技能（infometa/workbuddyskills 中的副本）重新编写。
- 配置 `inject_guide`：未开启 Computer Use 时，可把简版购物指南注入系统提示词。

### 变更

- 6 个 LLM 工具改用 `FunctionTool` + 完整 JSON Schema 注册（integer / enum / required），接口参数全部交给模型决定：
  - 商品检索：`mall_id`、`query_process`、`sort` 等；结果中显示 `mall_id` 便于模型二次筛选。
  - 好价检索：`query_process`、`size`。
  - 图搜商品：`threshold`、`is_crop`、`is_auto_first_cate_name`、`with_haojia`、`img_num`。
  - 知识图谱：`categories` 枚举。
  - 内容检索：`query_rewrite`、`correlate_enhance`、`start_time`、`data_source`、`return_size`。
  - 内容检索 Pro：`time_range`、`recall_num`、`ranker_advanced`、`rewrite`、`search_type`。
- 工具参数名与接口字段保持一致（如 `keyword` → `product_query`）；模型未传的参数回退到插件配置默认值。
- 内容检索超时取配置值与 60 秒的较大者。
- `astrbot_version` 下限调整为 4.24.0。

## [1.1.0] - 2026-11-06

### 新增

- 接入海纳开放平台全部 6 项能力，新增 LLM 工具：
  - `haina_discount_search`（好价检索 Pro）
  - `haina_image_product_search`（图搜商品，自动读取消息或引用中的图片）
  - `haina_product_info`（知识图谱检索：型号参数、价格、口碑）
  - `haina_web_search`（内容检索 Pro，全网内容，可限定时间范围）
- `haina_price_search` 支持排序参数，并展示近 180 天价格、销量、好评率。
- 新指令：`/识图查价`、`/产品参数`、`/测评`、`/全网搜`。
- 新配置：统一 `api_key`、网关地址、单次结果最大字数及各能力参数。

### 变更

- `/好价` 改为调用好价检索 Pro；原内容检索改用 `/测评`。
- 价格区间按接口要求转为整数。

### 兼容

- 未填写 `api_key` 时自动沿用 1.0.0 的 `product_api_key` / `content_api_key`。

## [1.0.0] - 2026-11-06

### 新增

- 首次发布：商品检索 Pro 查价、内容检索，指令 `/查价`、`/好价`。
