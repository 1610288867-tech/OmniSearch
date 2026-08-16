"""content_hash —— xxh3_64 内容指纹（P2.2：AI 结果复用，architecture.md §7.1 预留列）。

- 流式读取（1MB chunk），不将超大文件一次性载入内存（Windows-safe）
- 内容相同 → 相同 hash；内容不同 → 极高概率不同（xxh3_64 128 位雪崩）
- 计算失败（不可读等）→ 返回 None：调用方不得据此复用 AI 产物
- 模型文件不参与 hash reuse（模型是独立资产，由 manifest 管理）
"""
from __future__ import annotations

import logging
from pathlib import Path

import xxhash

logger = logging.getLogger("omnisearch.hash")

HASH_CHUNK_SIZE = 1 << 20  # 1 MiB


def content_hash_xxh3(path: str | Path) -> str | None:
    """流式计算文件 xxh3_64 内容指纹；不可读 → None（调用方不得复用）。"""
    try:
        h = xxhash.xxh3_64()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning("content hash failed %s: %s", path, exc)
        return None
