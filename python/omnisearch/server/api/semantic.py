"""Semantic Search API（M4 独立通道；M5 合并进 /search Hybrid）。"""
from __future__ import annotations

import time

from fastapi import APIRouter

from omnisearch.server.api.schemas import (
    SemanticSearchRequest,
    SemanticSearchResponse,
)
from omnisearch.server.service.semantic_search import SemanticSearchService

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_SEMANTIC: SemanticSearchService | None = None


def configure_semantic(semantic: SemanticSearchService) -> None:
    global _SEMANTIC
    _SEMANTIC = semantic


@router.post("/semantic", response_model=SemanticSearchResponse)
def semantic_search(req: SemanticSearchRequest) -> SemanticSearchResponse:
    assert _SEMANTIC is not None
    started = time.perf_counter()
    items = _SEMANTIC.search(req.query, top_k=req.topK)
    latency = int((time.perf_counter() - started) * 1000)
    return SemanticSearchResponse(query=req.query, total=len(items), latency_ms=latency, results=items)
