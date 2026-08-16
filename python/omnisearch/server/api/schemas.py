"""Pydantic DTO（与 desktop/src/shared/contracts.ts 对齐，architecture.md §13）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------- Search（M5：Hybrid Search，architecture.md §13） ----------------
class TimeRangeFilter(BaseModel):
    """offset-aware RFC3339 时间区间（禁止 timezone-naive，architecture.md §8）。"""

    from_: str = Field(..., alias="from")
    to: str
    basis_hint: str  # "exif" | "ctime" | "mtime"（§12.7，query 动词决定）


class ParsedQuery(BaseModel):
    """QueryParser 输出（§12.2 UnifiedFilter + semantic_text）。"""

    time_range: TimeRangeFilter | None = None
    file_types: list[str] = []
    extensions: list[str] = []
    semantic_text: str = ""
    parse_method: str = "rule"  # "rule" | "fallback"（Parser 失败兜底，不导致搜索失败）


class MatchReason(BaseModel):
    """匹配原因（§12.6）：channel ∈ keyword|body|ocr|semantic|metadata。"""

    channel: str
    text: str
    score: float | None = None
    basis: str | None = None      # metadata（时间）：exif | mtime | ctime
    confidence: str | None = None  # metadata（时间）：exact | fallback


class TimeInfo(BaseModel):
    """时间可信度（§12.7）：exif=exact；mtime/ctime=fallback；无 → unknown 排除。"""

    basis: str | None
    confidence: str | None
    value: str | None  # RFC3339（本地时区）


class SearchRequest(BaseModel):
    query: str = Field(..., description="自然语言/关键词查询（经 QueryParser 结构化）")
    topK: int = Field(50, ge=1, le=200)
    mode: str = Field("hybrid", pattern="^(keyword|semantic|hybrid)$")
    stages: bool = Field(False, description="返回分项耗时（parser/fts/semantic/finalize，M5 §20 benchmark 用）")


class SearchResultItem(BaseModel):
    file_id: int
    path: str
    filename: str
    dir_path: str
    extension: str
    file_type: str
    size_bytes: int
    mtime_ns: int
    rrf_score: float | None  # RRF 融合分（仅 FTS + Vector，§12.4）；metadata-only = null
    keyword_score: float | None  # BM25 原始分；未命中通道 = null（§12.5）
    semantic_score: float | None  # cosine；未命中通道 = null
    time_info: TimeInfo
    match_reasons: list[MatchReason]


class SearchResponse(BaseModel):
    query: str
    parsed: ParsedQuery
    total: int
    latency_ms: int
    results: list[SearchResultItem]
    degraded_channels: list[str] = []  # ["keyword" | "semantic"]（§12.8）
    stages: dict[str, float] | None = None  # 仅 stages=true 时返回（分项耗时，M5 §20）


# ---------------- Semantic Search（M4 独立通道；M5 合并进 /search Hybrid，保留兼容） ----------------
class SemanticSearchRequest(BaseModel):
    query: str
    topK: int = Field(50, ge=1, le=200)


class SemanticSearchResultItem(BaseModel):
    file_id: int
    path: str
    filename: str
    source_type: str  # doc_chunk | ocr | image_caption
    chunk_index: int
    text: str
    semantic_score: float


class SemanticSearchResponse(BaseModel):
    query: str
    total: int
    latency_ms: int
    results: list[SemanticSearchResultItem]


# ---------------- Settings（M5，architecture.md §13） ----------------
class SettingsUpdate(BaseModel):
    search_mode: str | None = Field(None, pattern="^(keyword|semantic|hybrid)$")
    w_kw: float | None = Field(None, ge=0.1, le=10)
    w_sem: float | None = Field(None, ge=0.1, le=10)
    topK: int | None = Field(None, ge=1, le=200)


class SettingsResponse(BaseModel):
    search_mode: str
    w_kw: float
    w_sem: float
    topK: int
    index_roots: list[str]
    models: dict[str, str]  # {bge: ok|missing, caption: ok|missing}
    storage: dict[str, int]  # {db_bytes, models_bytes}


# ---------------- Task Dashboard（M5，architecture.md §13） ----------------
class TaskStatsResponse(BaseModel):
    queue_length: int  # PENDING
    processing: int    # RUNNING
    success: int
    failed: int
    total: int


class FailedTaskItem(BaseModel):
    id: int
    file_id: int
    filename: str
    attempt: int
    max_attempts: int
    last_error: str | None


class TaskRetryResponse(BaseModel):
    status: str  # "retried" | "MAX_ATTEMPTS_EXCEEDED" | "TASK_NOT_FOUND"


# ---------------- Index ----------------
class ScanRequest(BaseModel):
    root_paths: list[str] = Field(..., min_length=1)
    scan_type: str = Field("full", pattern="^(full|incremental)$")


class ScanResponse(BaseModel):
    job_id: int
    root_path: str
    status: str


class IndexStatusResponse(BaseModel):
    running: bool
    jobs: list[dict]
