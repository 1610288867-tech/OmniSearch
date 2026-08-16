"""时间与时区单一实现（architecture.md §8，冻结规则）。

- SQLite 内部统一 UTC unixepoch seconds（整数）
- 用户输入（今天/昨天/日期区间）按 Windows 当前时区解释
- EXIF datetime_original 无时区 → 解释为「拍摄设备本地时间」，按本地时区换算 epoch
- API/UI 展示用 RFC3339（offset-aware）
- 禁止 server/worker/frontend 各自实现日期计算——统一经本模块
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

# EXIF 时间格式："2026:08:14 19:23:11" / "2026:08:14"（时区未知）
_EXIF_DT = re.compile(r"^(\d{4}):(\d{2}):(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$")
_EXIF_DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})$")


class _LocalTz(tzinfo):
    """OS 本地时区（DST 感知，无第三方依赖）。

    H4 背景：`datetime.now().astimezone().tzinfo` 返回「当前时刻的固定偏移」，
    对夏令时时区，用它在冬季/任意过去日期做换算会差 1 小时（如 8 月拿 UTC-4 去算
    1 月的 00:00）。本类按「具体时刻」经平台 mktime/localtime 求该时刻的正确偏移
    （Windows localtime 含 DST 规则），因此任意日期的解析都正确；
    非 DST 时区（UTC+8 中国）结果与之前完全一致。

    用法：所有本地时间构造/换算统一经本类（架构 §8 单一实现）；tzinfo 方法只接收
    携带本类实例的 datetime，其墙钟分量即视为本地时间。
    """

    _cache: dict[int, timedelta] = {}

    @classmethod
    def _offset_for_epoch(cls, epoch: int) -> timedelta:
        off = cls._cache.get(epoch)
        if off is None:
            local = datetime.fromtimestamp(epoch)  # 该时刻 OS 本地墙钟（含 DST）
            utc = datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)
            off = local - utc
            if len(cls._cache) > 4096:
                cls._cache.clear()
            cls._cache[epoch] = off
        return off

    def _wall(self, dt: datetime | None) -> datetime:
        if dt is None:
            dt = datetime.now()
        return dt.replace(tzinfo=None)  # 墙钟分量（naive）

    def utcoffset(self, dt: datetime | None) -> timedelta:
        wall = self._wall(dt)
        epoch = int(wall.timestamp())  # naive → 本地墙钟 → mktime（含 DST 规则）
        return self._offset_for_epoch(epoch)

    def dst(self, dt: datetime | None) -> timedelta:
        wall = self._wall(dt)
        # 标准偏移估算：同年 1 月 1 日的偏移（南半球少见的「反 DST」时区可接受近似）
        std = self.utcoffset(wall.replace(month=1, day=1).replace(tzinfo=None))
        return self.utcoffset(wall) - std

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def fromutc(self, dt: datetime) -> datetime:
        """UTC aware → 本地墙钟（每时刻偏移经 OS localtime 求，DST 正确）。"""
        epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())  # dt 表示 UTC
        return datetime.fromtimestamp(epoch).replace(tzinfo=self)


_LOCAL = _LocalTz()  # 单例：所有携带本 tz 的 aware datetime 可正常比较


def local_tz() -> tzinfo:
    """OS 本地时区（DST 感知，用户输入的本地语义基准）。"""
    return _LOCAL


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
