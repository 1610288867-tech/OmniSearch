"""Qdrant point_id（architecture.md §9.2 冻结，ADR-005）。

logical_key = f"{file_id}:{source_type}:{chunk_index}"
point_id    = xxh3_64(logical_key)（64 位无符号整数）
算法变更必须触发 Qdrant 全量重建。
"""
from __future__ import annotations

import xxhash


def logical_key(file_id: int, source_type: str, chunk_index: int) -> str:
    return f"{file_id}:{source_type}:{chunk_index}"


def point_id(file_id: int, source_type: str, chunk_index: int) -> int:
    return xxhash.xxh3_64_intdigest(logical_key(file_id, source_type, chunk_index).encode("utf-8"))
