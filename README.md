# 海纳电商查价 (astrbot_plugin_haina_price)

让 AstrBot 的大模型在聊天中**自动查询全网电商价格**并给出消费决策建议。数据来自值得买「[海纳开放平台](https://ai.zhidemai.com/haina-platform/content_search)」，已接入平台全部 6 项能力。

## 功能特性

| 海纳能力 | LLM 工具 | 指令 | 用途 |
| --- | --- | --- | --- |
| 商品检索 Pro | `haina_price_search` | `/查价`（`/比价`） | 全网查价：各商城价格、到手价、近 180 天价格、销量、好评率 |
| 好价检索 Pro | `haina_discount_search` | `/好价` | 什么值得买好价爆料、优惠券、活动、购买步骤 |
| 图搜商品 | `haina_image_product_search` | `/识图查价` | 发图识别商品，再自动查价 |
| 知识图谱检索 | `haina_product_info` | `/产品参数` | 型号级参数、价格、特征与分维度口碑 |
| 内容检索 | `haina_content_search` | `/测评` | 值得买/知乎/微信/头条等评测、选购指南 |
| 内容检索 Pro | `haina_web_search` | `/全网搜` | 全网内容，可限定时间范围 |

- 用户自然提问时由模型自动选择工具并可组合调用（如：识图 → 查价 → 查好价）。
- **接口参数全部暴露给模型**：工具使用完整 JSON Schema（含枚举、整数、必填），排序、价格区间、商城、品牌、时间范围、数据源、改写/增强开关、图搜阈值等都由模型按需决定；模型未传的参数使用插件配置中的默认值。
- **自带技能「海纳购物管家」**（`skills/haina-shopping-assistant`）：参考公开的同名技能重新编写，按“意图解析 → 查询改写 → 检索 → 模板输出”处理推荐、总结、对比、好价四类问题。
- 所有错误都转为友好提示，不会中断对话；结果长度可限制，避免占满上下文。

## 关于技能

- 安装插件后，技能会出现在 WebUI「插件 → 技能」中，可启用/禁用。
- AstrBot 的技能是“渐进式加载”：系统提示词里只放技能名和简介，模型需要用文件读取工具读取 `SKILL.md`。因此需要在 AstrBot 配置中开启 **Computer Use**。
- 未开启 Computer Use 时，AstrBot 仍会把技能列进提示词，并提示模型“请用户开启 Computer Use”。这种情况建议在 WebUI 禁用本技能，并打开插件配置「向系统提示词注入简版购物指南」作为替代。

## 环境要求

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| AstrBot | >= 4.24.0 | 需使用支持函数调用（Function Calling）的模型 |
| 海纳 API Key | — | 在 [海纳开放平台](https://ai.zhidemai.com/haina-platform) 申请，并开通需要的能力 |

**平台支持**：全平台。图搜商品需要平台提供公网可访问的图片 URL（如 QQ/aiocqhttp）。

## 安装

将 `astrbot_plugin_haina_price` 目录放入 `AstrBot/data/plugins/`，在 WebUI 插件页面重载；或上传到 GitHub 后通过链接安装。

## 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 海纳 API Key | 6 项能力共用 | 空 |
| 注入简版购物指南 | 未开启 Computer Use 时的技能替代 | 关 |
| 结果中包含链接 | — | 开 |
| 单次工具结果最大字数 | 超出截断 | 6000 |
| 【商品检索】返回数 / 排序 / 商城 ID | 商城 ID 多个用 `;` 分隔 | 10 / 综合 / 全网 |
| 【好价检索】返回条数 | — | 5 |
| 【图搜商品】返回数 / 主体识别 / 关联好价 | — | 5 / 开 / 开 |
| 【知识图谱】最多实体数 | — | 3 |
| 【内容检索】返回条数 / 摘要字数 / 问题改写 / 数据来源 / 起始时间 | — | 5 / 300 / 关 / 全部 / 不限 |
| 【内容检索 Pro】时间范围 | — | 两年 |
| 超时、调用方标识、网关地址 | 一般无需修改 | 30 秒 / `astrbot` / 官方网关 |

不需要的能力可在 WebUI「函数调用工具」中单独停用对应工具。

## 使用方法

直接和机器人聊天即可，例如：

- “iPhone 16 256G 现在多少钱，哪里买最便宜？”
- “AirPods Pro 2 最近有好价吗？”
- （发一张图）“这个多少钱？”
- “iPhone 15 Pro 和小米 14 参数对比一下”
- “3000 以内的扫地机怎么选？”
- “双十一苹果有什么活动？”

| 指令 | 说明 |
| --- | --- |
| `/查价 <商品名>` | 全网查价（别名 `/比价`） |
| `/好价 <商品名>` | 好价爆料 |
| `/识图查价` + 图片 | 以图搜商品（也可引用一张图片发送） |
| `/产品参数 <型号>` | 参数与口碑 |
| `/测评 <关键词>` | 评测/选购内容 |
| `/全网搜 <关键词>` | 全网内容检索 |

## 注意事项

- 价格、好价实时变动，结果仅供参考，以商城页面为准。
- 403 表示 Key 未开通对应能力；429 表示超过 QPS 或额度。
- 接口参数依据海纳开放平台文档实现；若平台调整网关地址，可在配置中修改。
- 结构参考了 [astrbot_plugin_goofish_catcher](https://github.com/RichardLiuda/astrbot_plugin_goofish_catcher) 的“LLM 直接调用工具”思路，未复制其代码。

## 目录结构

```text
astrbot_plugin_haina_price/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── LICENSE
└── skills/
    └── haina-shopping-assistant/
        ├── SKILL.md
        └── references/
            ├── principles.md
            ├── price.md
            ├── summary.md
            ├── recommend.md
            └── comparison.md
```

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT
