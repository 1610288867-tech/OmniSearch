"""QueryParserService —— MVP 规则解析器（architecture.md §12.2 MVP 边界）。

只做：① 时间表达（今天/昨天/前天/本周/上周/本月/具体日期/日期区间）
      ② 文件类型（image/document）③ 扩展名 ④ 剩余文本 → semantic_text
禁止：意图识别、实体抽取、多轮理解、LLM、自由 JSON schema。
解析失败兜底：semantic_text = 原始 query，Parser 失败不得导致搜索失败。

职责分离：Parser 只产出结构化 filter + semantic_text；FTS 关键词查询由 FTS Query
Builder 负责（fts_query_forms，架构 §12.2/§8.3）——两个职责不混在一起。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from omnisearch.common.utils.seg import seg_text
from omnisearch.server.service.time_range import TimeRange, TimeRangeService, _DATE, _RANGE_FULL

logger = logging.getLogger("omnisearch.server.query_parser")

# 文件类型词（中文优先 + 英文兼容）
_TYPE_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("image", ("图片", "照片", "图像", "image", "photo", "picture")),
    ("doc", ("文档", "文稿", "document", "doc")),
]
# 扩展名词（独立 token 或 .ext 形式）
_EXTENSIONS = (
    "pdf", "docx", "doc", "txt", "md", "markdown", "png", "jpg", "jpeg",
    "gif", "bmp", "webp", "pptx", "ppt", "xlsx", "xls",
)
_EXT_RE = re.compile(r"\.([a-z0-9]{2,5})\b", re.IGNORECASE)
_RELATIVE_WORDS = ("今天", "今日", "昨天", "昨日", "前天", "本周", "上周", "本月", "这个月")
# 最小停用词（结构化剥离后残留的高频词，不进入语义/关键词通道）
_STOPWORDS = {"的", "了", "在", "关于", "查找", "找", "请", "帮我", "包含", "含有"}


@dataclass
class ParsedQuery:
    """Parser 唯一输出：结构化 filter + semantic_text（架构 §12.2 UnifiedFilter）。"""

    time_range: TimeRange | None = None
    file_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    semantic_text: str = ""
    parse_method: str = "rule"  # "rule" | "fallback"


class QueryParser:
    def __init__(self, time_ranges: TimeRangeService):
        self._time = time_ranges

    def parse(
        self, query: str, now: datetime | None = None, tz: tzinfo | None = None
    ) -> ParsedQuery:
        try:
            return self._parse(query, now, tz)
        except Exception:  # noqa: BLE001 —— 解析失败兜底：semantic_text=原始 query
            logger.warning("query parse failed, fallback to raw: %r", query, exc_info=True)
            return ParsedQuery(semantic_text=query, parse_method="fallback")

    # ---- 规则解析 ----
    def _parse(self, query: str, now: datetime | None, tz: tzinfo | None) -> ParsedQuery:
        text = query.strip()
        out = ParsedQuery()
        if not text:
            return out

        # 1) 时间表达（动词优先级由完整 query 决定，architecture.md §12.7）
        hint = self._time.basis_hint_for(text)
        text = self._strip_time(text, out, hint, now, tz)

        # 2) 文件类型（剥离后不再进入语义/关键词通道）
        text = self._strip_file_types(text, out)

        # 3) 扩展名（.ext 形式 + 独立 token）
        text = self._strip_extensions(text, out)

        # 4) 剩余文本 → semantic_text。
        #    中文无词边界（'昨天的…' 剥离后残留 '的自由女神' 为一整 token），须经 jieba
        #    分词后按停用词过滤（与 FTS 查询同一分词器，seg_text 已在 server 搜索路径）；
        #    空 → 语义/关键词通道按空处理（semantic_text empty → 只走 FTS，§12.8）
        out.semantic_text = " ".join(t for t in seg_text(text).split() if t not in _STOPWORDS)
        return out

    def _strip_time(self, text: str, out: ParsedQuery, hint: str, now, tz) -> str:
        for word in _RELATIVE_WORDS:
            if word in text:
                out.time_range = self._time.resolve(word, now=now, tz=tz, hint=hint)
                if out.time_range is not None:
                    return text.replace(word, " ", 1)
        m = _RANGE_FULL.search(text) or _DATE.search(text)
        if m:
            out.time_range = self._time.resolve(m.group(0), now=now, tz=tz, hint=hint)
            if out.time_range is not None:
                return text.replace(m.group(0), " ", 1)
        return text

    def _strip_file_types(self, text: str, out: ParsedQuery) -> str:
        """类型词只在「语义末尾」抽取（M5 收口 2，防误判）。

        规则：jieba 分词（与 FTS 同分词器）后，仅最后一个非停用词 token 是类型词时抽取。
        - 有效：'自由女神照片'（末尾 照片）、'机器学习文档'（末尾 文档）、'图片'（整词）
        - 无效：'图片搜索系统'/'图片识别'/'图片文字'（'图片' 是修饰语，末尾是 搜索/识别/文字）、
                '机器学习图片搜索系统'（末尾 系统）、'关于图片的文档'（末尾 文档 → doc，image 不抽）
        禁止 substring 匹配（'doc' 不得命中 'docx' 内部，用显式 ASCII 边界）。
        """
        tokens = [t for t in seg_text(text).split() if t not in _STOPWORDS]
        if not tokens:
            return text
        last = tokens[-1]
        for ftype, words in _TYPE_WORDS:
            if last.lower() in words:
                out.file_types.append(ftype)
                text = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(last)}(?![A-Za-z0-9])", " ", text, count=1
                )
                break
        return text

    def _strip_extensions(self, text: str, out: ParsedQuery) -> str:
        m = _EXT_RE.search(text)
        if m and m.group(1).lower() in _EXTENSIONS:
            out.extensions.append(m.group(1).lower())
            text = text.replace(m.group(0), " ", 1)
        # 独立扩展名 token；显式 ASCII 边界（Python \b 对 CJK 无效，见 _EXT_RE 注释）
        for tok in re.findall(r"[A-Za-z0-9]{2,10}", text):
            low = tok.lower()
            if low in _EXTENSIONS and low not in out.extensions:
                out.extensions.append(low)
                text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", " ", text, count=1)
        return text
