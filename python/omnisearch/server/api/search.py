"""Search API（architecture.md §13：POST /api/v1/search —— Hybrid Search）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from omnisearch.server.api.schemas import ParsedQuery, SearchRequest, SearchResponse, TimeRangeFilter
from omnisearch.server.service.search import SearchError, SearchService

router = APIRouter(prefix="/api/v1", tags=["search"])

_SEARCH: SearchService | None = None


def configure_search(search: SearchService) -> None:
    global _SEARCH
    _SEARCH = search


def _parsed_dto(parsed) -> ParsedQuery:
    return ParsedQuery(
        time_range=(
            # 字段别名 'from' 为 Python 保留字：经 dict 构造（Pydantic v2 构造器按别名取值）
            TimeRangeFilter(**{"from": parsed.time_range.from_iso, "to": parsed.time_range.to_iso,
                               "basis_hint": parsed.time_range.basis_hint})
            if parsed.time_range
            else None
        ),
        file_types=parsed.file_types,
        extensions=parsed.extensions,
        semantic_text=parsed.semantic_text,
        parse_method=parsed.parse_method,
    )


@router.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    assert _SEARCH is not None
    started = time.perf_counter()
    try:
        outcome = _SEARCH.search(req.query, top_k=req.topK, mode=req.mode)
    except SearchError as e:
        raise HTTPException(status_code=502, detail=f"search channels failed: {e}") from e
    latency = int((time.perf_counter() - started) * 1000)
    return SearchResponse(
        query=req.query,
        parsed=_parsed_dto(outcome.parsed),
        total=len(outcome.results),
        latency_ms=latency,
        results=outcome.results,
        degraded_channels=outcome.degraded,
        stages=outcome.stages if req.stages else None,
    )
