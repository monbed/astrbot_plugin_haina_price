"""海纳电商查价插件：把值得买「海纳开放平台」的 6 项能力注册为 LLM 工具。

接口文档：https://ai.zhidemai.com/haina-platform （网关 https://gw-openapi.zhidemai.com，
请求头 ``x-api-key``，所有能力共用同一个 Key）

| 能力           | 网关路径                                   | LLM 工具                      |
| -------------- | ------------------------------------------ | ----------------------------- |
| 商品检索 Pro   | /product/v1                                | haina_price_search            |
| 好价检索 Pro   | /hj-search/discount/v2                     | haina_discount_search         |
| 图搜商品       | /img-search-service/v1/img_search_by_pic   | haina_image_product_search    |
| 知识图谱检索   | /kg_graph/api/v1/search                    | haina_product_info            |
| 内容检索       | /content_search_rank/v1/search             | haina_content_search          |
| 内容检索 Pro   | /universal_search                          | haina_web_search              |

工具使用 FunctionTool + 完整 JSON Schema 注册，接口参数（排序、商城、价格区间、时间范围、
数据源、改写/增强开关等）全部暴露给 LLM 决定；LLM 未传的参数使用插件配置中的默认值。
"""

import asyncio
import base64
import html
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from pydantic.dataclasses import dataclass

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

GATEWAY = "https://gw-openapi.zhidemai.com"
PATHS = {
    "product": "/product/v1",
    "discount": "/hj-search/discount/v2",
    "image": "/img-search-service/v1/img_search_by_pic",
    "graph": "/kg_graph/api/v1/search",
    "content": "/content_search_rank/v1/search",
    "content_pro": "/universal_search",
}
NAMES = {
    "product": "商品检索 Pro",
    "discount": "好价检索 Pro",
    "image": "图搜商品",
    "graph": "知识图谱检索",
    "content": "内容检索",
    "content_pro": "内容检索 Pro",
}
PRODUCT_SORTS = ["price_asc", "price_desc", "sale_qty", "good_comments_share"]
TIME_RANGES = ["OneDay", "OneWeek", "OneMonth", "OneYear", "TwoYear", "NoLimit"]
GRAPH_CATEGORIES = [
    "电脑数码", "家用电器", "个护化妆", "母婴", "宠物", "运动户外", "食品酒水", "医药健康",
    "家居百货", "服饰", "鞋类", "箱包", "配饰", "手表", "乐器", "模玩盲盒", "图书",
]  # fmt: skip
# 图床上传端点（均为匿名上传，无需 Key）。AstrBot 4.26+ 会把入站图片改写为本地临时文件，
# 海纳图搜只接受公网 img_url，故本地图片需先上传图床。思路移植自同作者的
# astrbot_plugin_serpapi_imgsearch（https://github.com/monbed/astrbot_plugin_serpapi_imgsearch）。
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"  # 永久
LITTERBOX_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"  # 临时 1h
UGUU_UPLOAD_URL = "https://uguu.se/upload.php"  # 临时约 3h，响应为 JSON
IMAGE_HOSTS = ("litterbox", "uguu", "catbox")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# CQ 码字符串上报中图片/商城表情的 url 参数（值经 CQ 转义，需 HTML 反转义）
_CQ_IMAGE_URL_RE = re.compile(r"\[CQ:(?:image|mface)[^\]]*?,url=([^,\]]+)")

APPLY_HINT = "请到值得买海纳开放平台 https://ai.zhidemai.com/haina-platform 申请 API Key 并在插件配置中填写。"

# 技能不可用时（未开启 Computer Use 等），可选择把这段稳定的简版指南追加进系统提示词
SHOPPING_GUIDE = """[海纳购物助手指南]
处理购物消费问题时使用 haina_* 工具：
- 问价格/哪里便宜/比价 → haina_price_search（必要时再 haina_discount_search 查好价、券、活动）
- 商品值不值得买/怎么样 → haina_product_info + haina_content_search（加"评测""优缺点"）+ haina_price_search
- 推荐/怎么选 → haina_content_search（加"选购指南""推荐"）+ haina_price_search（带预算区间）
- A 和 B 怎么选 → 对每款 haina_product_info / haina_price_search + 对比类 haina_content_search
- 用户发图问是什么/多少钱 → haina_image_product_search，再用识别出的商品名查价
- 时效资讯（新品、大促规则）→ haina_web_search
原则：价格、链接、优惠只来自工具结果，不编造；无结果时如实说明并请用户补充品牌、型号或预算；价格以商城页面为准。"""


class HainaApiError(Exception):
    """海纳接口调用失败（网络错误、HTTP 错误或业务错误）。"""


# 当前插件实例。工具对象不直接持有插件实例，避免工具集被复制时连带复制 HTTP 会话。
_ACTIVE: dict[str, "HainaPricePlugin"] = {}


@dataclass
class HainaTool(FunctionTool):
    """覆写 ``call`` 的 FunctionTool，按工具名分发到插件方法。

    不能用 ``handler=self.xxx``：AstrBot 加载插件时会把 ``__module__`` 属于该插件的
    handler 包成 ``partial(handler, 插件实例)``，已绑定方法会因此多收到一个位置参数。
    定义在 main.py 中，工具才能通过 ``__module__`` 归属到本插件并随插件启停。
    """

    async def call(self, context: Any, **kwargs: Any) -> str:
        plugin = _ACTIVE.get("plugin")
        if plugin is None:
            return "海纳插件尚未就绪，请稍后再试。"
        method = getattr(plugin, TOOL_SPECS[self.name]["method"])
        return await method(context.context.event, **kwargs)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _to_int(value: Any, default: int, low: int, high: int) -> int:
    """把任意值安全地转为限定范围内的整数。"""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _to_price(value: Any) -> int | None:
    """价格转为正整数（接口只支持整数价格）；0、空值或非法值视为不限。"""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _to_bool(value: Any, default: bool) -> bool:
    """容错地解析布尔值（LLM 可能传 "true"/1 等）。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip(text: Any, limit: int) -> str:
    """压缩空白并截断文本。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _brief(value: Any, limit: int = 200) -> str:
    """把任意结构（dict/list/JSON 字符串）压缩为一行短文本。"""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return _clip(value, limit)
    if isinstance(value, list):
        text = "；".join(_brief(v, limit) for v in value if v not in (None, "", [], {}))
        return _clip(text, limit)
    if isinstance(value, dict):
        text = "，".join(f"{k}:{_brief(v, 80)}" for k, v in value.items() if v not in (None, "", [], {}))
        return _clip(text, limit)
    return _clip(value, limit)


def _first(d: dict, *keys: str) -> Any:
    """返回第一个非空字段。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_list(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    """把数组或逗号分隔字符串转为字符串列表。"""
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


# ----------------------------------------------------------------------
# 图片辅助（本地图片 → 公网 URL）
# ----------------------------------------------------------------------
def _is_http(value: Any) -> bool:
    return str(value or "").strip().startswith(("http://", "https://"))


def _read_file_bytes(path: str) -> bytes:
    """读取本地文件（供 asyncio.to_thread 调用）。"""
    with open(path, "rb") as f:
        return f.read()


def _guess_image_type(data: bytes) -> tuple[str, str]:
    """按文件头推断 (扩展名, MIME)，图床按扩展名生成链接，未知回退 jpg。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


def _first_image(event: AstrMessageEvent | None) -> Comp.Image | None:
    """取消息中第一张图片组件（优先正文，其次引用消息）。"""
    if event is None:
        return None
    try:
        chain = list(event.get_messages() or [])
    except Exception:
        return None
    for comp in chain:
        if isinstance(comp, Comp.Image) and (comp.url or comp.file):
            return comp
    for comp in chain:
        if isinstance(comp, Comp.Reply):
            for sub in getattr(comp, "chain", None) or []:
                if isinstance(sub, Comp.Image) and (sub.url or sub.file):
                    return sub
    return None


def _urls_from_onebot_segments(message: Any) -> list[str]:
    """从 OneBot v11 消息段（dict 数组或 CQ 码字符串）提取 image/mface 直链。"""
    urls: list[str] = []
    if isinstance(message, str):
        for m in _CQ_IMAGE_URL_RE.finditer(message):
            url = html.unescape(m.group(1)).strip()
            if _is_http(url):
                urls.append(url)
    elif isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") in ("image", "mface"):
                url = str((seg.get("data") or {}).get("url") or "").strip()
                if _is_http(url):
                    urls.append(url)
    return urls


def _local_path(source: str) -> str:
    """把 file:// 或本地路径转为可读路径，非本地来源返回空串。"""
    if source.startswith("file://"):
        path = source[7:]
        if path.startswith("/") and len(path) > 2 and path[2] == ":":  # file:///C:/...
            path = path[1:]
        return path
    if re.match(r"^[A-Za-z]:[/\\]", source) or source.startswith("/"):
        return source
    return ""


# ----------------------------------------------------------------------
# 工具 JSON Schema（全部接口参数暴露给 LLM）
# ----------------------------------------------------------------------
def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOL_SPECS: dict[str, dict] = {
    "haina_price_search": {
        "method": "tool_price_search",
        "api": "product",
        "description": (
            "【海纳·商品检索Pro】电商查价：在京东、天猫、淘宝、拼多多等全网电商按商品词检索商品，"
            "返回商城、页面价、到手价、价格标签、近180天价格、销量、好评率与购买链接。"
            "用户问多少钱、哪里买便宜、比价、预算内买什么时调用。"
        ),
        "parameters": _obj(
            {
                "product_query": {
                    "type": "string",
                    "description": "商品搜索词：只保留品牌、产品、型号、规格等核心词，去掉问句、价格词、意图词。例：'iPhone 16 256G'",
                },
                "min_price": {"type": "integer", "minimum": 0, "description": "最低价（整数元），不限则不传"},
                "max_price": {"type": "integer", "minimum": 0, "description": "最高价（整数元），须大于最低价，不限则不传"},
                "brand_name": {"type": "string", "description": "品牌筛选，例：'Apple'，不确定则不传"},
                "category_name": {"type": "string", "description": "品类筛选，例：'手机'，不确定则不传"},
                "mall_id": {
                    "type": "string",
                    "description": "限定商城ID，多个用英文分号分隔（如 '183;247'）。商城ID可从之前查价结果中的 mall_id 获得，不限则不传",
                },
                "sort": {
                    "type": "string",
                    "enum": PRODUCT_SORTS,
                    "description": "排序，不传=综合相关度；price_asc=价格升序（找最便宜），price_desc=价格降序，sale_qty=销量，good_comments_share=好评率",
                },
                "size": {"type": "integer", "minimum": 1, "maximum": 30, "description": "返回商品数"},
                "query_process": {
                    "type": "integer",
                    "enum": [1, 0],
                    "description": "是否让服务端处理/改写商品词：1=是（默认），0=否（搜精确型号时用）",
                },
            },
            ["product_query"],
        ),
    },
    "haina_discount_search": {
        "method": "tool_discount_search",
        "api": "discount",
        "description": (
            "【海纳·好价检索Pro】检索什么值得买社区的好价爆料：好价/史低价、优惠券、凑单活动、购买步骤、爆料时间与链接。"
            "用户问有没有优惠、好价、券、什么时候买最划算时调用；比价时可与 haina_price_search 配合。"
        ),
        "parameters": _obj(
            {
                "product_query": {"type": "string", "description": "商品词，例：'AirPods Pro 2'"},
                "size": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回好价条数"},
                "query_process": {"type": "integer", "enum": [1, 0], "description": "是否处理商品词：1=是，0=否"},
            },
            ["product_query"],
        ),
    },
    "haina_image_product_search": {
        "method": "tool_image_search",
        "api": "image",
        "description": (
            "【海纳·图搜商品】识别图片中的商品，返回相似商品名称、品牌、分类、相似度、建议查价词与关联好价。"
            "用户发商品图问这是什么、多少钱、哪里买时调用；识别后可再调用 haina_price_search。"
        ),
        "parameters": _obj(
            {
                "img_url": {
                    "type": "string",
                    "description": "可选。公网可访问的图片 http(s) 直链；不传则自动使用用户当前消息或引用消息中的图片（本地图片会自动上传图床转为公网链接）",
                },
                "img_num": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回相似商品数"},
                "threshold": {"type": "number", "minimum": 0, "maximum": 1, "description": "相似度阈值0~1，结果太杂时调高，不传用服务默认"},
                "is_crop": {"type": "boolean", "description": "是否开启主体识别（图片背景复杂、多物体时建议开启）"},
                "is_auto_first_cate_name": {"type": "boolean", "description": "是否自动预测一级分类（更准但更慢）"},
                "with_haojia": {"type": "boolean", "description": "是否返回关联好价"},
            },
            [],
        ),
    },
    "haina_product_info": {
        "method": "tool_product_info",
        "api": "graph",
        "description": (
            "【海纳·知识图谱检索】按具体型号查询产品实体：核心参数、价格与商城报价、产品特征、分维度用户评价及来源文章。"
            "用户问某型号参数配置、怎么样、优缺点，或多款型号对比时调用。只支持具体商品型号。"
        ),
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "具体商品型号实体词，例：'iPhone 15 Pro'、'戴森 HD15'"},
                "categories": {"type": "string", "enum": GRAPH_CATEGORIES, "description": "所属领域，不确定则不传，由服务识别"},
            },
            ["query"],
        ),
    },
    "haina_content_search": {
        "method": "tool_content_search",
        "api": "content",
        "description": (
            "【海纳·内容检索】检索值得买社区、知乎、微信、今日头条、B站、抖音等平台的评测、选购指南、使用体验与口碑文章。"
            "用户问值不值得买、怎么选、推荐、评价如何时调用。可多次调用覆盖不同角度（评测/优缺点/选购指南/对比）。"
        ),
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "搜索问题，1~200字，例：'扫地机器人 选购指南'、'戴森吹风机 评测'"},
                "return_size": {"type": "integer", "minimum": 1, "maximum": 50, "description": "返回文章数"},
                "query_rewrite": {"type": "boolean", "description": "是否让服务端改写问题（口语化问题可开启，会增加耗时）"},
                "correlate_enhance": {"type": "boolean", "description": "是否开启相关性增强（默认开启）"},
                "start_time": {"type": "string", "description": "只要此时间之后的内容，格式 'YYYY-MM-DD HH:MM:SS'；需要近期内容时传，否则不传"},
                "data_source": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定来源平台标识，例：['zdm_shequ','Zhihu','weixin','jinritoutiao']；不限则不传",
                },
            },
            ["query"],
        ),
    },
    "haina_web_search": {
        "method": "tool_web_search",
        "api": "content_pro",
        "description": (
            "【海纳·内容检索Pro】全网网页内容检索，可限定时间范围，适合新品发布、大促活动规则、近期消费资讯等时效性问题。"
        ),
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "搜索问题，1~100字"},
                "time_range": {"type": "string", "enum": TIME_RANGES, "description": "时间范围：一天/一周/一月/一年/两年/不限"},
                "recall_num": {"type": "integer", "minimum": 1, "maximum": 50, "description": "返回文章数"},
                "ranker_advanced": {"type": "boolean", "description": "是否使用增强排序（默认开启）"},
                "rewrite": {"type": "boolean", "description": "是否改写用户问题"},
                "search_type": {"type": "string", "enum": ["web"], "description": "检索类型，当前仅支持 web"},
            },
            ["query"],
        ),
    },
}


class HainaPricePlugin(Star):
    """海纳电商查价插件入口类。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        _ACTIVE["plugin"] = self
        # 用 FunctionTool 注册，可声明 integer / enum / required 等完整 JSON Schema
        self.context.add_llm_tools(
            *(
                HainaTool(name=name, description=spec["description"], parameters=spec["parameters"])
                for name, spec in TOOL_SPECS.items()
            )
        )

    async def initialize(self) -> None:
        if not self._api_key():
            logger.warning("[haina_price] 未配置海纳 API Key，所有查询工具将不可用。")

    async def terminate(self) -> None:
        """插件停用/重载时关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _api_key(self) -> str:
        # 兼容 1.0.0 的 product_api_key / content_api_key
        for k in ("api_key", "product_api_key", "content_api_key"):
            v = str(self._cfg(k, "") or "").strip()
            if v:
                return v
        return ""

    def _url(self, name: str) -> str:
        base = str(self._cfg("gateway_base", GATEWAY) or GATEWAY).rstrip("/")
        return base + PATHS[name]

    def _request_from(self) -> str:
        return str(self._cfg("request_from", "astrbot") or "astrbot")

    def _show_link(self) -> bool:
        return bool(self._cfg("show_link", True))

    def _finish(self, text: str) -> str:
        """限制单次工具结果长度，避免撑爆上下文。"""
        limit = _to_int(self._cfg("max_output_chars", 6000), 6000, 500, 50000)
        return text if len(text) <= limit else text[:limit] + "\n…（结果过长已截断）"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _call(self, name: str, payload: dict, timeout: int = 30) -> dict:
        """调用指定能力，校验 HTTP 状态与业务错误，失败抛 HainaApiError。"""
        if not self._api_key():
            raise HainaApiError("插件未配置海纳 API Key。" + APPLY_HINT)
        # 取配置值与接口建议超时的较大者（内容检索文档建议 60 秒）
        timeout = max(timeout, _to_int(self._cfg("timeout", 30), 30, 5, 110))
        session = await self._get_session()
        headers = {"Content-Type": "application/json", "x-api-key": self._api_key()}
        try:
            async with session.post(
                self._url(name),
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise HainaApiError("鉴权失败（401），API Key 缺失或无效")
                if resp.status == 403:
                    raise HainaApiError(f"无权限（403），该 Key 可能未开通「{NAMES[name]}」能力或已停用")
                if resp.status == 429:
                    raise HainaApiError("调用过于频繁或额度已用完（429），请稍后重试")
                if resp.status >= 400:
                    raise HainaApiError(f"接口返回 HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except HainaApiError:
            raise
        except TimeoutError as exc:
            raise HainaApiError("请求超时") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise HainaApiError(f"网络或解析错误：{type(exc).__name__}") from exc

        if not isinstance(data, dict):
            raise HainaApiError("接口返回格式异常")
        code = data.get("error_code")
        if code not in (0, "0", None):
            raise HainaApiError(f"接口错误 {code}：{data.get('error_msg') or '未知错误'}")
        if data.get("success") is False:
            raise HainaApiError(f"接口错误：{_brief(data.get('error')) or '未知错误'}")
        return data

    async def _run(self, name: str, work: Callable[[], Awaitable[str]]) -> str:
        """统一执行 + 异常兜底，返回给 LLM/用户的文本。"""
        try:
            return self._finish(await work())
        except HainaApiError as exc:
            logger.warning(f"[haina_price] {NAMES[name]} 失败：{exc}")
            return f"{NAMES[name]}失败：{exc}"
        except Exception as exc:
            logger.error(f"[haina_price] {NAMES[name]} 异常：{exc}", exc_info=True)
            return f"{NAMES[name]}失败：插件内部错误，请稍后再试。"

    @staticmethod
    def _rows(data: dict, *keys: str) -> list[dict]:
        """从 data.xxx / xxx / data 中取结果列表。"""
        inner = data.get("data")
        for k in keys:
            if isinstance(inner, dict) and isinstance(inner.get(k), list):
                return _as_list(inner[k])
            if isinstance(data.get(k), list):
                return _as_list(data[k])
        return _as_list(inner)

    # ------------------------------------------------------------------
    # 1. 商品检索 Pro
    # ------------------------------------------------------------------
    async def tool_price_search(self, event: AstrMessageEvent, **kw: Any) -> str:
        """haina_price_search 处理函数，kw 为模型传入的工具参数。"""
        keyword = str(kw.get("product_query") or kw.get("keyword") or "").strip()
        if not keyword:
            return "错误：product_query 不能为空，请提供要查价的商品名称。"
        lo, hi = _to_price(kw.get("min_price")), _to_price(kw.get("max_price"))
        if lo and hi:
            lo, hi = min(lo, hi), max(lo, hi)
            if lo == hi:  # 接口要求 max_price 大于 min_price
                hi = lo + 1
        sort = kw.get("sort")
        sort = str(self._cfg("product_sort", "") if sort is None else sort).strip()
        if sort not in PRODUCT_SORTS:  # 空值即综合排序
            sort = ""
        size = _to_int(kw.get("size"), 0, 0, 30) or _to_int(self._cfg("product_size", 10), 10, 1, 30)
        mall_id = kw.get("mall_id")
        mall_id = str(self._cfg("product_mall_id", "") if mall_id in (None, "") else mall_id)
        payload = {
            "request_from": self._request_from(),
            "question_id": uuid.uuid4().hex,
            "product_query": keyword,
            "query_process": 0 if str(kw.get("query_process", 1)) == "0" else 1,
            "size": size,
            "mall_id": mall_id.replace("；", ";").replace(",", ";").strip(),
            "brand_name": str(kw.get("brand_name") or kw.get("brand") or "").strip(),
            "category_name": str(kw.get("category_name") or kw.get("category") or "").strip(),
            "min_price": lo,
            "max_price": hi,
            "sort": sort,
            "fields": "",
        }

        async def work() -> str:
            rows = self._rows(await self._call("product", payload), "rows")[:size]
            return self._fmt_products(keyword, rows)

        return await self._run("product", work)

    def _fmt_products(self, keyword: str, rows: list[dict]) -> str:
        if not rows:
            return f"未在全网电商中找到「{keyword}」的相关商品，可尝试更通用的关键词或放宽筛选条件。"
        lines = [f"「{keyword}」全网查价结果（{len(rows)} 条，来源：值得买海纳商品检索）："]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {_clip(r.get('title'), 70)}")
            mall = r.get("mall_name") or "未知商城"
            mall_id = r.get("mall_id")
            shop = r.get("shop_name")
            lines.append(
                f"   商城：{mall}"
                + (f"（mall_id={mall_id}）" if mall_id not in (None, "") else "")
                + (f"　店铺：{shop}" if shop else "")
            )
            price_desc = []
            if r.get("page_price") not in (None, "", 0, 0.0):
                price_desc.append(f"页面价 ¥{r.get('page_price')}")
            if r.get("price") not in (None, "") and str(r.get("price")) != str(r.get("page_price")):
                price_desc.append(f"到手价 {_clip(r.get('price'), 40)}")
            if r.get("price_tags"):
                price_desc.append(f"标签 {_brief(r.get('price_tags'), 60)}")
            lines.append("   价格：" + ("；".join(price_desc) or "暂无"))
            hist = _brief(r.get("price_info_180_days"), 160)
            if hist:
                lines.append(f"   近180天价格：{hist}")
            stats = []
            sales = _first(r, "standard_month_sale_qty", "sales_cnt")
            if sales:
                stats.append(f"销量 {sales}")
            if r.get("comment_cnt"):
                stats.append(f"评论 {r.get('comment_cnt')}")
            if r.get("good_comments_share"):
                stats.append(f"好评率 {r.get('good_comments_share')}")
            if r.get("brand_name"):
                stats.append(f"品牌 {r.get('brand_name')}")
            if stats:
                lines.append("   " + "　".join(str(s) for s in stats))
            if self._show_link() and r.get("url"):
                lines.append(f"   链接：{r.get('url')}")
        lines.append("（价格实时变动，以商城页面为准）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 2. 好价检索 Pro
    # ------------------------------------------------------------------
    async def tool_discount_search(self, event: AstrMessageEvent, **kw: Any) -> str:
        keyword = str(kw.get("product_query") or kw.get("keyword") or "").strip()
        if not keyword:
            return "错误：product_query 不能为空。"
        size = _to_int(kw.get("size"), 0, 0, 20) or _to_int(self._cfg("discount_size", 5), 5, 1, 20)
        payload = {
            "request_from": self._request_from(),
            "question_id": uuid.uuid4().hex,
            "product_query": keyword,
            "query_process": 0 if str(kw.get("query_process", 1)) == "0" else 1,
            "size": size,
            "fields": "",
        }

        async def work() -> str:
            rows = self._rows(await self._call("discount", payload), "rows")[:size]
            if not rows:
                return f"暂未找到「{keyword}」的好价爆料。"
            lines = [f"「{keyword}」好价爆料（{len(rows)} 条，来源：什么值得买）："]
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}. {_clip(r.get('title'), 70)}")
                price = _first(r, "digital_price", "price")
                desc = [f"好价 {price}"] if price else []
                if r.get("page_price"):
                    desc.append(f"页面价 {r.get('page_price')}")
                if r.get("mall_name"):
                    desc.append(f"商城 {r.get('mall_name')}")
                if r.get("pubdate"):
                    desc.append(f"爆料时间 {r.get('pubdate')}")
                if desc:
                    lines.append("   " + "　".join(str(x) for x in desc))
                for label, key in (("优惠券", "coupon"), ("活动", "activity"), ("购买步骤", "product_buy_step")):
                    v = _brief(r.get(key), 120)
                    if v:
                        lines.append(f"   {label}：{v}")
                reason = _brief(_first(r, "bl_reason", "content"), 150)
                if reason:
                    lines.append(f"   推荐理由：{reason}")
                link = _first(r, "url", "direct_link")
                if self._show_link() and link:
                    lines.append(f"   链接：{link}")
            lines.append("（好价有时效性，可能已失效，以商城页面为准）")
            return "\n".join(lines)

        return await self._run("discount", work)

    # ------------------------------------------------------------------
    # 3. 图搜商品
    # ------------------------------------------------------------------
    @staticmethod
    async def _onebot_image_url(event: AstrMessageEvent | None) -> str:
        """aiocqhttp 专用：从 OneBot 原始上报中取图片直链。

        AstrBot 4.26+ 会把入站 Image 组件的 url 改写为本地路径，但当前消息的 raw_message
        与被引用消息的 get_msg 返回中仍保留 QQ 直链，复用它可省去图床上传。
        """
        if event is None:
            return ""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return ""
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            message = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
            urls = _urls_from_onebot_segments(message)
            if urls:
                return urls[0]
            reply = next((c for c in (event.get_messages() or []) if isinstance(c, Comp.Reply)), None)
            if reply is None or reply.id is None:
                return ""
            msg = await event.bot.get_msg(message_id=int(reply.id))
            urls = _urls_from_onebot_segments((msg or {}).get("message"))
            return urls[0] if urls else ""
        except Exception as exc:
            logger.debug(f"[haina_price] 读取 OneBot 原始图片直链失败：{exc}")
            return ""

    @staticmethod
    async def _read_source_bytes(source: str) -> bytes | None:
        """读取本地路径 / file:// / base64:// / data:image 来源的图片字节。"""
        source = (source or "").strip()
        if not source:
            return None
        try:
            if source.startswith("base64://"):
                return base64.b64decode(source[9:])
            m = re.match(r"data:image/[\w.+-]+;base64,(.+)", source, re.S)
            if m:
                return base64.b64decode(m.group(1))
            path = _local_path(source)
            if path and os.path.isfile(path):
                return await asyncio.to_thread(_read_file_bytes, path)
        except Exception as exc:
            logger.debug(f"[haina_price] 读取本地图片失败：{exc}")
        return None

    async def _image_bytes(self, image: Comp.Image | None) -> bytes | None:
        """还原图片组件字节：优先框架的 convert_to_base64（兼容本地临时文件与直链下载），再本地读取。"""
        if image is None:
            return None
        try:
            b64 = await image.convert_to_base64()
            if b64:
                return base64.b64decode(b64)
        except Exception as exc:
            logger.debug(f"[haina_price] convert_to_base64 失败，回退本地读取：{exc}")
        return await self._read_source_bytes(image.url or image.file or "")

    async def _upload_image(self, data: bytes) -> str:
        """把图片匿名上传到配置的图床，返回公网 URL，失败抛 HainaApiError。"""
        if len(data) > MAX_UPLOAD_BYTES:
            raise HainaApiError("图片过大，无法上传图床")
        host = str(self._cfg("image_host", "litterbox") or "litterbox")
        if host not in IMAGE_HOSTS:
            host = "litterbox"
        ext, mime = _guess_image_type(data)
        form = aiohttp.FormData()
        if host == "uguu":
            endpoint, field = UGUU_UPLOAD_URL, "files[]"
        else:
            endpoint, field = (CATBOX_UPLOAD_URL, "fileToUpload") if host == "catbox" else (LITTERBOX_UPLOAD_URL, "fileToUpload")
            form.add_field("reqtype", "fileupload")
            if host == "litterbox":
                form.add_field("time", "1h")
        form.add_field(field, data, filename=f"image.{ext}", content_type=mime)
        proxy = str(self._cfg("image_upload_proxy", "") or "").strip()
        proxy = proxy if _is_http(proxy) else None  # aiohttp 仅支持 http/https 代理
        session = await self._get_session()
        try:
            async with session.post(
                endpoint,
                data=form,
                headers={"User-Agent": USER_AGENT},
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=_to_int(self._cfg("timeout", 30), 30, 5, 110)),
            ) as resp:
                text = (await resp.text()).strip()
                status = resp.status
        except TimeoutError as exc:
            raise HainaApiError(f"上传图床 {host} 超时") from exc
        except aiohttp.ClientError as exc:
            raise HainaApiError(f"上传图床 {host} 失败：{type(exc).__name__}") from exc

        url = ""
        if status == 200 and host == "uguu":
            try:
                obj = json.loads(text)
                if obj.get("success") and obj.get("files"):
                    url = str((obj["files"][0] or {}).get("url") or "")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                url = ""
        elif status == 200:
            url = text
        if not url.startswith("https://"):
            logger.warning(f"[haina_price] 图床 {host} 返回异常：HTTP {status} {text[:200]}")
            raise HainaApiError(f"上传图床 {host} 失败（HTTP {status}），可在配置中更换图床或设置代理")
        logger.info(f"[haina_price] 图片已上传到 {host}：{url}")
        return url

    async def _resolve_image_url(self, event: AstrMessageEvent | None, given: str) -> str:
        """得到可供海纳访问的公网图片 URL。

        顺序：模型传入的公网 URL → OneBot 原始直链 → 消息图片组件的公网 URL →
        还原本地/内联图片字节并上传图床。找不到图片返回空串。
        """
        given = (given or "").strip()
        if _is_http(given):
            return given
        url = await self._onebot_image_url(event)
        if url:
            return url
        image = _first_image(event)
        for v in (getattr(image, "url", ""), getattr(image, "file", "")) if image else ():
            if _is_http(v):
                return str(v).strip()
        data = await self._read_source_bytes(given) if given else None
        if not data and image is not None:
            data = await self._image_bytes(image)
        if not data:
            return ""
        if not bool(self._cfg("image_allow_upload", True)):
            raise HainaApiError("图片为本地文件，且已在配置中禁止上传图床，无法识别；请开启「允许上传图床」或发送公网图片链接")
        return await self._upload_image(data)

    async def tool_image_search(self, event: AstrMessageEvent, **kw: Any) -> str:
        payload: dict[str, Any] = {
            "img_num": _to_int(kw.get("img_num"), 0, 0, 20) or _to_int(self._cfg("image_num", 5), 5, 1, 20),
            "with_haojia": 1 if _to_bool(kw.get("with_haojia"), bool(self._cfg("image_with_haojia", True))) else 0,
            # 文档中这两个开关以字符串 "True"/"False" 传递
            "is_crop": "True" if _to_bool(kw.get("is_crop"), bool(self._cfg("image_crop", True))) else "False",
        }
        if kw.get("is_auto_first_cate_name") is not None:
            payload["is_auto_first_cate_name"] = "True" if _to_bool(kw.get("is_auto_first_cate_name"), False) else "False"
        threshold = _to_float(kw.get("threshold"))
        if threshold is not None and 0 <= threshold <= 1:
            payload["threshold"] = threshold

        async def work() -> str:
            img_url = await self._resolve_image_url(event, str(kw.get("img_url") or ""))
            if not img_url:
                return "错误：未在消息中找到图片。请让用户直接发送商品图片，或引用一条包含图片的消息后再试。"
            payload["img_url"] = img_url
            rows = self._rows(await self._call("image", payload))
            if not rows:
                return "未识别到相似商品，可让用户换一张主体更清晰的商品图，或降低 threshold。"
            lines = [f"图片识别到 {len(rows)} 个相似商品（来源：值得买海纳图搜）："]
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}. {_clip(_first(r, 'pro_name', 'keyword'), 70)}")
                cates = "/".join(str(r.get(f"cate_name_{n}")) for n in range(1, 5) if r.get(f"cate_name_{n}"))
                meta = []
                if r.get("brand_name"):
                    meta.append(f"品牌 {r.get('brand_name')}")
                if cates:
                    meta.append(f"分类 {cates}")
                if r.get("score") is not None:
                    meta.append(f"相似度 {r.get('score')}")
                if meta:
                    lines.append("   " + "　".join(meta))
                if r.get("keyword"):
                    lines.append(f"   建议查价词：{r.get('keyword')}")
                if r.get("haojia_ids"):
                    lines.append(f"   关联好价ID：{_brief(r.get('haojia_ids'), 80)}")
                if self._show_link() and r.get("pro_url"):
                    lines.append(f"   链接：{r.get('pro_url')}")
            lines.append("如需价格，可用识别出的商品名再调用 haina_price_search。")
            return "\n".join(lines)

        return await self._run("image", work)

    # ------------------------------------------------------------------
    # 4. 知识图谱检索（型号级产品信息）
    # ------------------------------------------------------------------
    async def tool_product_info(self, event: AstrMessageEvent, **kw: Any) -> str:
        product = str(kw.get("query") or kw.get("product") or "").strip()
        if not product:
            return "错误：query 不能为空，请提供具体商品型号。"
        category = str(kw.get("categories") or kw.get("category") or "").strip()
        payload = {
            "query": product,
            "categories": category if category in GRAPH_CATEGORIES else "",
            "request_from": self._request_from(),
        }

        async def work() -> str:
            data = await self._call("graph", payload)
            rows = _as_list(data.get("search_result")) or self._rows(data, "search_result")
            if not rows:
                return f"知识图谱中未找到「{product}」，可尝试更标准的型号名称。"
            limit = _to_int(self._cfg("graph_size", 3), 3, 1, 10)
            lines = [f"「{product}」产品信息（来源：值得买知识图谱）："]
            for i, e in enumerate(rows[:limit], 1):
                head = _first(e, "entity_name", "graph_entity_name") or product
                brand = e.get("brand")
                lines.append(f"{i}. {head}" + (f"（{brand}）" if brand else ""))
                for label, key, n in (
                    ("简介", "entity_description", 200),
                    ("核心参数", "entity_parameter", 400),
                    ("价格", "price", 200),
                    ("特征", "features", 300),
                    ("用户评价", "entity_comments", 400),
                ):
                    v = _brief(e.get(key), n)
                    if v:
                        lines.append(f"   {label}：{v}")
                for p in _as_list(e.get("product_list"))[:5]:
                    title = _clip(_first(p, "title", "name", "product_name"), 50)
                    price = _first(p, "price", "page_price")
                    mall = p.get("mall_name") or ""
                    url = _first(p, "url", "link")
                    line = f"   - {title} {mall} {price or ''}".rstrip()
                    if self._show_link() and url:
                        line += f" {url}"
                    lines.append(line)
            return "\n".join(lines)

        return await self._run("graph", work)

    # ------------------------------------------------------------------
    # 5. 内容检索
    # ------------------------------------------------------------------
    async def tool_content_search(self, event: AstrMessageEvent, **kw: Any) -> str:
        query = str(kw.get("query") or "").strip()
        if not query:
            return "错误：query 不能为空。"
        size = _to_int(kw.get("return_size"), 0, 0, 50) or _to_int(self._cfg("content_size", 5), 5, 1, 50)
        payload: dict[str, Any] = {
            "query": query[:200],
            "question_id": uuid.uuid4().hex,
            "query_rewrite": _to_bool(kw.get("query_rewrite"), bool(self._cfg("content_query_rewrite", False))),
            "correlate_enhance": _to_bool(kw.get("correlate_enhance"), True),
            "return_size": size,
            "request_from": self._request_from(),
        }
        start_time = str(kw.get("start_time") or self._cfg("content_start_time", "") or "").strip()
        if start_time:
            payload["start_time"] = start_time
        sources = _str_list(kw.get("data_source")) or _str_list(self._cfg("content_data_source", []))
        if sources:
            payload["data_source"] = sources

        async def work() -> str:
            items = self._rows(await self._call("content", payload, 60), "content_list")[:size]
            return self._fmt_articles(query, items, "消费内容（值得买社区/知乎/微信等）")

        return await self._run("content", work)

    def _fmt_articles(self, query: str, items: list[dict], label: str) -> str:
        if not items:
            return f"未找到与「{query}」相关的内容。"
        max_len = _to_int(self._cfg("content_max_chars", 300), 300, 50, 2000)
        lines = [f"「{query}」相关{label}："]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_clip(_first(it, 'works_title', 'title'), 80)}")
            meta = " | ".join(
                str(x)
                for x in (
                    _first(it, "website_cname", "source"),
                    _first(it, "works_pub_time", "publish_time"),
                    _first(it, "author", "works_author"),
                )
                if x
            )
            if meta:
                lines.append(f"   {meta}")
            body = _first(it, "works_content", "context", "content")
            if body:
                lines.append(f"   摘要：{_clip(body, max_len)}")
            link = _first(it, "works_link", "article_link", "url")
            if self._show_link() and link:
                lines.append(f"   链接：{link}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 6. 内容检索 Pro（全网）
    # ------------------------------------------------------------------
    async def tool_web_search(self, event: AstrMessageEvent, **kw: Any) -> str:
        query = str(kw.get("query") or "").strip()
        if not query:
            return "错误：query 不能为空。"
        time_range = str(kw.get("time_range") or "").strip()
        if time_range not in TIME_RANGES:
            time_range = str(self._cfg("web_time_range", "TwoYear") or "TwoYear")
        size = _to_int(kw.get("recall_num"), 0, 0, 50) or _to_int(self._cfg("content_size", 5), 5, 1, 50)
        payload = {
            "query": query[:100],
            "time_range": time_range,
            "request_from": self._request_from(),
            "recall_num": size,
            "ranker_advanced": _to_bool(kw.get("ranker_advanced"), True),
            "rewrite": _to_bool(kw.get("rewrite"), False),
            "search_type": "web",
        }

        async def work() -> str:
            items = self._rows(await self._call("content_pro", payload), "items")[:size]
            return self._fmt_articles(query, items, "全网内容")

        return await self._run("content_pro", work)

    # ------------------------------------------------------------------
    # 可选：把简版购物指南追加到系统提示词（稳定内容，不破坏缓存）
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_guide(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """未启用 AstrBot 技能系统时，可开启此项让模型获得工具使用指南。"""
        if not self._cfg("inject_guide", False):
            return
        if SHOPPING_GUIDE not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + "\n\n" + SHOPPING_GUIDE

    # ------------------------------------------------------------------
    # 指令（不经过 LLM，直接查询）
    # ------------------------------------------------------------------
    @staticmethod
    def _rest_text(event: AstrMessageEvent, fallback: str) -> str:
        """取指令名之后的全部文本，带空格的商品名无需加引号。"""
        parts = (event.message_str or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else str(fallback or "").strip()

    @filter.command("查价", alias={"比价"})
    async def cmd_price(self, event: AstrMessageEvent, keyword: str = ""):
        """全网查价。用法：/查价 商品名"""
        keyword = self._rest_text(event, keyword)
        if not keyword:
            yield event.plain_result("用法：/查价 商品名，例如 /查价 iPhone 16 256G")
        else:
            yield event.plain_result(await self.tool_price_search(event, product_query=keyword))
        event.stop_event()

    @filter.command("好价")
    async def cmd_discount(self, event: AstrMessageEvent, keyword: str = ""):
        """查好价爆料。用法：/好价 商品名"""
        keyword = self._rest_text(event, keyword)
        if not keyword:
            yield event.plain_result("用法：/好价 商品名，例如 /好价 戴森吹风机")
        else:
            yield event.plain_result(await self.tool_discount_search(event, product_query=keyword))
        event.stop_event()

    @filter.command("识图查价")
    async def cmd_image(self, event: AstrMessageEvent):
        """以图搜商品。用法：发送 /识图查价 并附带图片，或引用一张图片发送"""
        yield event.plain_result(await self.tool_image_search(event))
        event.stop_event()

    @filter.command("产品参数")
    async def cmd_info(self, event: AstrMessageEvent, product: str = ""):
        """查询型号参数与口碑。用法：/产品参数 型号"""
        product = self._rest_text(event, product)
        if not product:
            yield event.plain_result("用法：/产品参数 型号，例如 /产品参数 iPhone 15 Pro")
        else:
            yield event.plain_result(await self.tool_product_info(event, query=product))
        event.stop_event()

    @filter.command("测评")
    async def cmd_content(self, event: AstrMessageEvent, query: str = ""):
        """搜索评测/选购内容。用法：/测评 关键词"""
        query = self._rest_text(event, query)
        if not query:
            yield event.plain_result("用法：/测评 关键词，例如 /测评 扫地机器人 选购")
        else:
            yield event.plain_result(await self.tool_content_search(event, query=query))
        event.stop_event()

    @filter.command("全网搜")
    async def cmd_web(self, event: AstrMessageEvent, query: str = ""):
        """全网内容检索。用法：/全网搜 关键词"""
        query = self._rest_text(event, query)
        if not query:
            yield event.plain_result("用法：/全网搜 关键词")
        else:
            yield event.plain_result(await self.tool_web_search(event, query=query))
        event.stop_event()
