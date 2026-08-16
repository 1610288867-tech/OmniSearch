"""时间与时区单一实现（architecture.md §8，冻结规则）。

- SQLite 内部统一 UTC unixepoch seconds（整数）
- 用户输入（今天/昨天/日期区间）按 Windows 当前时区解释
- EXIF datetime_original 无时区 → 解释为「拍摄设备本地时间」，按本地时区换算 epoch
- API/UI 展示用 RFC3339（offset-aware）
- 禁止 server/worker/frontend 各自实现日期计算——统一经本模块
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo

# EXIF 时间格式："2026:08:14 19:23:11" / "2026:08:14"（时区未知）
_EXIF_DT = re.compile(r"^(\d{4}):(\d{2}):(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$")
_EXIF_DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})$")


def local_tz() -> tzinfo:
    """Windows 当前时区（用户输入的本地语义基准）。"""
    return datetime.now().astimezone().tzinfo


def now_local() -> datetime:
    return datetime.now(local_tz())


def epoch_to_local_iso(epoch: int) -> str:
    """UTC epoch → offset-aware RFC3339（如 2026-08-14T19:23:11+08:00）。"""
    dt = datetime.fromtimestamp(epoch, local_tz())
    return dt.isoformat(timespec="seconds")


def exif_str_to_epoch(s: str) -> int | None:
    """EXIF 时间字符串（设备本地时间）→ UTC epoch；无法解析返回 None。

    规则（architecture.md §7.2）：EXIF 无时区信息 → 按本地时区换算。
    """
    try:
        m = _EXIF_DT.match(s.strip())
        if m is None:
            m = _EXIF_DATE.match(s.strip())
            if m is None:
                return None
            dt = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=local_tz())
            return int(dt.timestamp())
        dt = datetime(
            int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]),
            tzinfo=local_tz(),
        )
        return int(dt.timestamp())
    except ValueError:
        return None  # 非法日期（13月/99日等）


def epoch_to_exif_str(epoch: int) -> str:
    """本地 epoch → EXIF 原始格式字符串（Worker 写回用）。"""
    dt = datetime.fromtimestamp(epoch, local_tz())
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def day_start(dt: datetime) -> datetime:
    """本地当日 00:00（时区保持）。"""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def add_days(dt: datetime, n: int) -> datetime:
    return dt + timedelta(days=n)
