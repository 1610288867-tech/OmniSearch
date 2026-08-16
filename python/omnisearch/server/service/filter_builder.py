"""FilterBuilderService —— 唯一 canonical WHERE 生成器（architecture.md §12.2/§12.7）。

过滤正确性事实来源是 SQLite：本服务生成的 WHERE 必须用于
1) files 查询  2) FTS join  3) Vector 候选回表 join——三处一致，Qdrant 不做第二套过滤语义。
Metadata（time/file_type/extension/is_deleted）不参与 RRF。

时间链（§12.7，hint 由 QueryParser 从 query 动词决定）：
- exif（默认）：EXIF datetime_original 存在 → hard filter（exact）；
             无 EXIF → mtime 参与过滤（fallback，结果必须标注）；
             （mtime/ctime 恒非空，第三级 ctime 链实际不触发）
- ctime（创建/保存）：直接按 ctime 过滤
- mtime（修改）：直接按 mtime 过滤
"""
from __future__ import annotations

from dataclasses import dataclass, field

from omnisearch.server.service.time_range import TimeRange


@dataclass
class UnifiedFilter:
    """QueryParser 输出的唯一 Filter Model（架构 §12.2）。"""

    time_range: TimeRange | None = None
    file_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    include_deleted: bool = False


class FilterBuilderService:
    def build(self, filters: UnifiedFilter, alias: str = "f") -> tuple[str, list]:
        """返回 (canonical WHERE, params)。

        alias：files 表别名（三处调用统一用 "f"；需 LEFT JOIN exif e（仅时间过滤时））。
        """
        where: list[str] = []
        params: list = []

        if not filters.include_deleted:
            where.append(f"{alias}.is_deleted = 0")

        if filters.file_types:
            where.append(f"{alias}.file_type IN ({', '.join('?' * len(filters.file_types))})")
            params.extend(filters.file_types)

        if filters.extensions:
            # files.extension 存 '.pdf' 形式（含点）；parser 产出 'pdf' → 统一补点
            exts = [e if e.startswith(".") else "." + e for e in filters.extensions]
            where.append(f"{alias}.extension IN ({', '.join('?' * len(exts))})")
            params.extend(exts)

        self._build_time(filters, alias, where, params)

        return " AND ".join(where) or "1=1", params

    @staticmethod
    def needs_exif(filters: UnifiedFilter) -> bool:
        """时间过滤且 hint=exif 时需 LEFT JOIN exif 表。"""
        return filters.time_range is not None and filters.time_range.basis_hint == "exif"

    def _build_time(self, filters: UnifiedFilter, alias: str, where: list[str], params: list) -> None:
        tr = filters.time_range
        if tr is None:
            return
        lo, hi = tr.from_epoch, tr.to_epoch
        if tr.basis_hint == "ctime":
            where.append(f"{alias}.ctime_ns / 1000000000 >= ? AND {alias}.ctime_ns / 1000000000 < ?")
            params.extend([lo, hi])
        elif tr.basis_hint == "mtime":
            where.append(f"{alias}.mtime_ns / 1000000000 >= ? AND {alias}.mtime_ns / 1000000000 < ?")
            params.extend([lo, hi])
        else:  # exif（默认）：EXIF exact → mtime fallback（架构 §12.7）
            # 注意：整链必须括起来——SQL 优先级 AND > OR，若不分组，
            # 'is_deleted=0 AND (exif) OR (mtime)' 会被解析为 '(is_deleted AND exif) OR mtime'，
            # 导致 mtime 分支绕过 is_deleted / f.id 约束（实测发现的正确性 bug）
            where.append(
                f"((e.datetime_original_epoch IS NOT NULL"
                f" AND e.datetime_original_epoch >= ? AND e.datetime_original_epoch < ?)"
                f" OR (e.datetime_original_epoch IS NULL"
                f" AND {alias}.mtime_ns / 1000000000 >= ? AND {alias}.mtime_ns / 1000000000 < ?))"
            )
            params.extend([lo, hi, lo, hi])
