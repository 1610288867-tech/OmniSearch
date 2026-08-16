"""Qdrant 向量访问封装（M4：architecture.md §9，common 共享层——Worker 写入 + Server 查询）。

- 单 collection "omnisearch"：doc_chunk / ocr / image_caption 同一 BGE 文本语义空间
- Cosine + HNSW 初始参数（m=16, ef_construct=128, ef=64）——benchmark 前不调整
- 同步 upsert（wait=true）：成功 → embedding_status=SUCCESS；失败 → FAILED
- SQLite 是事实数据源；Qdrant 是可重建索引（本封装只做索引读写）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("omnisearch.vector")

COLLECTION_NAME = "omnisearch"
HNSW_M = 16
HNSW_EF_CONSTRUCT = 128
HNSW_EF = 64


@dataclass(frozen=True)
class VectorPoint:
    """一个待写入的向量点（payload 最小集：file_id/source_type/chunk_index/text）。"""

    point_id: int
    vector: list[float]
    file_id: int
    source_type: str
    chunk_index: int
    text: str


class VectorStore:
    def __init__(self, url: str, dim: int):
        self._client = QdrantClient(url=url, timeout=10)
        self._dim = dim

    # ---- collection ----
    def ensure_collection(self) -> None:
        """创建 collection（幂等）：Cosine + HNSW 初始参数（architecture.md §9.1）。"""
        exists = self._client.collection_exists(COLLECTION_NAME)
        if not exists:
            self._client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qm.VectorParams(
                    size=self._dim,
                    distance=qm.Distance.COSINE,
                    hnsw_config=qm.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
                ),
            )
            self._client.create_payload_index(
                COLLECTION_NAME, "file_id", qm.PayloadSchemaType.INTEGER
            )
            self._client.create_payload_index(
                COLLECTION_NAME, "source_type", qm.PayloadSchemaType.KEYWORD
            )
            logger.info("collection %s created (dim=%d)", COLLECTION_NAME, self._dim)

    # ---- 写入（wait=true，同步 upsert） ----
    def upsert_points(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        self._client.upsert(
            collection_name=COLLECTION_NAME,
            wait=True,  # 同步确认（architecture.md §9.3：成功 → SUCCESS）
            points=[
                qm.PointStruct(
                    id=p.point_id,
                    vector=p.vector,
                    payload={
                        "file_id": p.file_id,
                        "source_type": p.source_type,
                        "chunk_index": p.chunk_index,
                        "text": p.text,
                    },
                )
                for p in points
            ],
        )

    # ---- 查询（qdrant-client ≥1.13 用 query_points；旧版 search 兼容） ----
    def search(self, vector: list[float], top_k: int = 100) -> list[tuple[dict, float]]:
        """语义检索：返回 [(payload, score)]。过滤（is_deleted 等）由调用方回 SQLite 完成。"""
        if hasattr(self._client, "query_points"):
            resp = self._client.query_points(
                collection_name=COLLECTION_NAME,
                query=vector,
                limit=top_k,
                search_params=qm.SearchParams(hnsw_ef=HNSW_EF),
            )
            return [(h.payload, float(h.score)) for h in resp.points]
        hits = self._client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            limit=top_k,
            search_params=qm.SearchParams(hnsw_ef=HNSW_EF),
        )
        return [(h.payload, float(h.score)) for h in hits]

    # ---- 删除/对账 ----
    def list_keys_by_file(self, file_id: int) -> list[int]:
        """按 file_id 列出该文件全部 point_id（stale 差集计算用，architecture.md §11.5）。"""
        return [
            p.id
            for p in self._client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(key="file_id", match=qm.MatchValue(value=file_id))]
                ),
                limit=10000,
                with_payload=False,
            )[0]
        ]

    def delete_points(self, point_ids: list[int]) -> None:
        if point_ids:
            self._client.delete(collection_name=COLLECTION_NAME, points_selector=point_ids, wait=True)

    def count(self) -> int:
        return self._client.count(collection_name=COLLECTION_NAME).count
