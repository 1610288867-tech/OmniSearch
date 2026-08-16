"""TimeRangeService —— 用户时间表达的唯一解析实现（architecture.md §8/§12.2）。

只做时间：今天/昨天/前天/本周/上周/本月/具体日期/日期区间 → UTC epoch 区间
（半开区间 [from, to)，API 展示为 offset-aware RFC3339）。
禁止 server/worker/frontend 各自实现日期计算——统一经本服务 + common/utils/time.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from omnisearch.common.utils.time import day_start, epoch_to_local_iso, local_tz, now_local


@dataclass(frozen=True)
class TimeRange:
    """半开时间区间 [from_epoch, to_epoch)；from_iso/to_iso 为 RFC3339 展示。"""

    from_epoch: int
    to_epoch: int
    from_iso: str
    to_iso: str
    basis_hint: str  # "exif"（默认）| "ctime" | "mtime"（由 query 动词决定，architecture.md §12.7）


# 具体日期/区间（支持 2026-08-14、2026/8/14、2026年8月14日、8月14日、2026.08.01）。
# H1 修正：① 两种精确形态（4 位年完整日期 / 中文「X月X日」）——'3.11'/'1.5' 版本号不命中；
# ② ASCII 字母数字边界（'python3.11' 中 'n3' 相邻 → 不匹配）。
_DATE_YMD = re.compile(
    r"(?<![A-Za-z0-9])(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?(?![A-Za-z0-9])"
)
_DATE_CN = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})月(\d{1,2})日?(?![A-Za-z0-9])")
_DATE = _DATE_YMD  # 单日期匹配入口（YMD 优先；CN 形态由 _resolve_date 内分派）
_RANGE_SEP = r"\s*(?:[~至到—]|-\s+)\s*"  # 区间分隔符：~ 至 到 —；裸 - 仅当后随空白（防与日期内连字符冲突）
_RANGE_FULL = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:(?:\d{4})[年./-]\d{1,2}[月./-]\d{1,2}日?|\d{1,2}月\d{1,2}日?)"
    + _RANGE_SEP
    + r"(?:(?:\d{4})[年./-]\d{1,2}[月./-]\d{1,2}日?|\d{1,2}月\d{1,2}日?)"
    r"(?![A-Za-z0-9])"
)
# 相对时间词（按词长降序，先匹配长词）
_RELATIVE = {
    "今天": 1, "今日": 1, "昨天": 2, "昨日": 2, "前天": 3,
    "本周": "week", "上周": "week-1", "本月": "month", "这个月": "month",
}

_BASIS_VERBS = {"exif": "拍摄摄照", "ctime": "创建保存", "mtime": "修改"}


class TimeRangeService:
    def resolve(
        self,
        expr: str,
        now: datetime | None = None,
        tz: tzinfo | None = None,
        hint: str | None = None,
    ) -> TimeRange | None:
        """解析用户时间表达 → TimeRange；无法识别返回 None（调用方负责剥离）。

        hint：时间字段优先级（缺省按 expr 动词推导；QueryParser 传原始 query 的 hint——
        '昨天拍的照片' 中 '拍' 在时间词之外，须由完整 query 决定，architecture.md §12.7）。
        now/tz 仅供测试注入；生产默认 Windows 当前时区（architecture.md §8）。
        """
        tz = tz or local_tz()
        now = (now or now_local()).astimezone(tz)
        today = day_start(now)
        r = self._resolve_relative(expr, today, tz)
        if r is None:
            r = self._resolve_date(expr, today, tz)
        if r is None:
            return None
        return TimeRange(
            from_epoch=int(r[0].timestamp()),
            to_epoch=int(r[1].timestamp()),
            from_iso=epoch_to_local_iso(int(r[0].timestamp())),
            to_iso=epoch_to_local_iso(int(r[1].timestamp())),
            basis_hint=hint or self.basis_hint_for(expr),
        )

    @staticmethod
    def basis_hint_for(text: str) -> str:
        """query 动词 → 时间字段优先级（architecture.md §12.7 默认 EXIF → mtime → ctime）。"""
        for hint, verbs in _BASIS_VERBS.items():
            if any(v in text for v in verbs):
                return hint
        return "exif"

    # ---- 相对时间（本周一为一周起点，中国习惯） ----
    def _resolve_relative(self, expr: str, today: datetime, tz: tzinfo):
        for word, kind in _RELATIVE.items():
            if word not in expr:
                continue
            monday = today - timedelta(days=today.weekday())
            if kind == 1:
                return today, today + timedelta(days=1)
            if kind == 2:
                return today - timedelta(days=1), today
            if kind == 3:
                return today - timedelta(days=2), today - timedelta(days=1)
            if kind == "week":
                return monday, monday + timedelta(days=7)
            if kind == "week-1":
                return monday - timedelta(days=7), monday
            if kind == "month":
                first = today.replace(day=1)
                nxt = (first + timedelta(days=32)).replace(day=1)
                return first, nxt
        return None

    # ---- 具体日期 / 日期区间 ----
    def _resolve_date(self, expr: str, today: datetime, tz: tzinfo):
        m = _RANGE_FULL.search(expr)
        if m:
            # 区间两侧分别为日期表达（分隔符与 _RANGE_SEP 一致）
            parts = re.split(_RANGE_SEP, m.group(0))
            start = self._parse_date_expr(parts[0], today, tz)
            end = self._parse_date_expr(parts[1], today, tz)
            if start and end and end >= start:
                return start, end + timedelta(days=1)
        m = _DATE_YMD.search(expr.strip())
        if m:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz)
            return d, d + timedelta(days=1)
        m = _DATE_CN.search(expr.strip())
        if m:
            d = datetime(today.year, int(m.group(1)), int(m.group(2)), tzinfo=tz)
            return d, d + timedelta(days=1)
        return None

    def _parse_date_expr(self, text: str, today: datetime, tz: tzinfo) -> datetime | None:
        """解析单个日期表达（YMD 完整或中文月日）→ datetime；无法解析 → None。"""
        m = _DATE_YMD.search(text)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz)
        m = _DATE_CN.search(text)
        if m:
            return datetime(today.year, int(m.group(1)), int(m.group(2)), tzinfo=tz)
        return None
