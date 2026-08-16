"""域 A：QueryParser（M5 §18A）—— 时间/类型/扩展名/关键词/失败兜底。

时间解析用固定时区注入（zoneinfo），断言确定性与机器时区无关（architecture.md §8）。
"""
from __future__ import annotations

from datetime import datetime

from zoneinfo import ZoneInfo

from omnisearch.server.service.query_parser import QueryParser
from omnisearch.server.service.time_range import TimeRangeService

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 16, 14, 30, tzinfo=TZ)  # 周日
P = QueryParser(TimeRangeService())


def _parse(q: str):
    return P.parse(q, now=NOW, tz=TZ)


# ---------------- 时间表达 ----------------
def test_today():
    r = _parse("今天").time_range
    assert r.from_iso == "2026-08-16T00:00:00+08:00"
    assert r.to_iso == "2026-08-17T00:00:00+08:00"


def test_yesterday():
    r = _parse("昨天").time_range
    assert r.from_iso == "2026-08-15T00:00:00+08:00"
    assert r.to_iso == "2026-08-16T00:00:00+08:00"


def test_day_before_yesterday():
    r = _parse("前天").time_range
    assert r.from_iso == "2026-08-14T00:00:00+08:00"
    assert r.to_iso == "2026-08-15T00:00:00+08:00"


def test_this_week():
    r = _parse("本周").time_range
    assert r.from_iso == "2026-08-10T00:00:00+08:00"  # 周一
    assert r.to_iso == "2026-08-17T00:00:00+08:00"


def test_last_week():
    r = _parse("上周").time_range
    assert r.from_iso == "2026-08-03T00:00:00+08:00"
    assert r.to_iso == "2026-08-10T00:00:00+08:00"


def test_this_month():
    r = _parse("本月").time_range
    assert r.from_iso == "2026-08-01T00:00:00+08:00"
    assert r.to_iso == "2026-09-01T00:00:00+08:00"


def test_explicit_date():
    for q in ("2026-08-14", "2026/8/14", "2026年8月14日", "8月14日"):
        r = _parse(q).time_range
        assert r is not None, q
        assert r.from_iso == "2026-08-14T00:00:00+08:00", q


def test_date_range():
    for q in ("2026-08-01 到 2026-08-14", "2026-08-01~2026-08-14", "2026-08-01 — 2026-08-14"):
        r = _parse(q).time_range
        assert r is not None, q
        assert r.from_iso == "2026-08-01T00:00:00+08:00"
        assert r.to_iso == "2026-08-15T00:00:00+08:00"  # 区间闭到 14 日 23:59:59


# ---------------- 类型 / 扩展名 / 关键词 ----------------
def test_image_type():
    x = _parse("昨天的自由女神照片")
    assert x.file_types == ["image"]
    assert x.time_range is not None and x.time_range.from_iso == "2026-08-15T00:00:00+08:00"
    assert x.semantic_text == "自由女神"  # 架构 §12.2 示例语义


def test_document_type():
    assert _parse("关于神经网络的文档").file_types == ["doc"]
    assert _parse("机器学习文档").file_types == ["doc"]


def test_extension():
    x = _parse("机器学习pdf文档")
    assert x.extensions == ["pdf"]
    x2 = _parse("report.docx 会议")
    assert x2.extensions == ["docx"]


def test_simple_keywords():
    x = _parse("机器学习架构")
    # 停用词过滤经 jieba 分词（中文无词边界，'的' 需切分后识别）→ semantic_text 为分词文本
    assert x.semantic_text == "机器 学习 架构"
    assert x.file_types == [] and x.time_range is None


def test_basis_hint_by_verb():
    """动词 → 时间字段优先级（§12.7）：拍/摄/照 → exif；创建/保存 → ctime；修改 → mtime。"""
    assert _parse("昨天拍的照片").time_range.basis_hint == "exif"
    assert _parse("上周摄于北京").time_range.basis_hint == "exif"
    assert _parse("本月创建的文件").time_range.basis_hint == "ctime"
    assert _parse("昨天保存的").time_range.basis_hint == "ctime"
    assert _parse("上周修改的文档").time_range.basis_hint == "mtime"
    assert _parse("昨天的文件").time_range.basis_hint == "exif"  # 默认 exif → mtime → ctime


def test_no_time_no_type():
    x = _parse("hello world")
    assert x.time_range is None and x.file_types == [] and x.semantic_text == "hello world"


def test_parser_failure_fallback():
    """Parser 失败 → semantic_text=原始 query，不导致搜索失败（§12.2）。"""
    class BadParser(QueryParser):
        def _parse(self, query, now=None, tz=None):  # noqa: ARG002
            raise RuntimeError("boom")

    out = BadParser(TimeRangeService()).parse("任意查询", now=NOW, tz=TZ)
    assert out.parse_method == "fallback"
    assert out.semantic_text == "任意查询"
