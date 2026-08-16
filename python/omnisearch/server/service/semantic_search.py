"""SemanticSearchService —— M4 独立语义通道（architecture.md §12.3 的 Vector 部分）。

Query → BGE embed → Qdrant topK → SQLite chunks 三元组校验 → files canonical WHERE
→ file_id 去重（多 chunk 取最高分）→ 语义结果。
防污染：orphan/stale point、deleted file、已不存在 chunk 一律剔除。
M4 不做 RRF/Hybrid/QueryParser（M5）。
"""
from __future__ import annotations

import logging
import time

from omnisearch.common.database import Database
from omnisearch.common.embedding import EmbeddingProvider
from omnisearch.common.vector import VectorStore

logger = logging.getLogger("omnisearch.server.semantic")


class SemanticSearchService:
    def __init__(self, db: Database, embedder: EmbeddingProvider, vector_store: VectorStore):
        self._db = db
        self._embedder = embedder
        self._vector = vector_store

    def search(self, semantic_text: str, top_k: int = 50, where: str = "f.is_deleted = 0", params: list | None = None) -> list[dict]:
        """语义检索 → 三元组校验 → files canonical WHERE → file_id 去重（取最高分）。

        where/params：FilterBuilderService 生成的 canonical WHERE（M5 Hybrid 三处一致的
        第 3 处，§12.2）；缺省 = is_deleted=0（M4 独立通道语义，过滤正确性以 SQLite 为准）。
        """
        started = time.perf_counter()
        if not semantic_text.strip():
            return []
        query_vec = self._embedder.embed_query(semantic_text)
        hits = self._vector.search(query_vec, top_k=top_k)

        results: dict[int, dict] = {}
        params = params or []
        with self._db.connect() as c:
            for payload, score in hits:
                fid = payload.get("file_id")
                source_type = payload.get("source_type")
                chunk_index = payload.get("chunk_index")
                if None in (fid, source_type, chunk_index):
                    continue
                # 三元组校验（防 stale/orphan point 污染，architecture.md §12.3）
                ok = c.execute(
                    """SELECT 1 FROM chunks
                       WHERE file_id = ? AND source_type = ? AND chunk_index = ?""",
                    (fid, source_type, chunk_index),
                ).fetchone()
                if not ok:
                    continue
                # canonical WHERE（时间/类型/扩展名/is_deleted 与 files 查询、FTS join 一致）
                frow = c.execute(
                    f"""SELECT f.id, f.path, f.filename, e.datetime_original_epoch
                        FROM files f LEFT JOIN exif e ON e.file_id = f.id
                        WHERE f.id = ? AND {where}""",
                    (fid, *params),
                ).fetchone()
                if not frow:
                    continue
                # file_id 去重：同文件多 chunk 取最高分，保留命中 chunk 证据
                if fid not in results or score > results[fid]["semantic_score"]:
                    results[fid] = {
                        "file_id": fid,
                        "path": frow["path"],
                        "filename": frow["filename"],
                        "source_type": source_type,
                        "chunk_index": chunk_index,
                        "text": payload.get("text", ""),
                        "semantic_score": score,
                    }

        ordered = sorted(results.values(), key=lambda r: -r["semantic_score"])[:top_k]
        logger.debug("semantic %r → %d results (%.1fms)", semantic_text, len(ordered), (time.perf_counter() - started) * 1000)
        return ordered
