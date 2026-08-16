/**
 * 前后端契约单一事实源（architecture.md §4.2：与后端 OpenAPI 对齐）。
 * IPC Channel 名 + 类型 —— main / preload / renderer 三处共用。
 */

// ---------- 健康状态（与 python omnisearch/common/contracts.py 对齐） ----------
export interface ComponentHealth {
  ok: boolean;
  detail?: string | null;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  components: Record<string, ComponentHealth>;
}

/** Electron 视角的整体系统状态（渲染层展示用）。 */
export interface SystemStatus {
  /** 后端 /health 响应（FastAPI + SQLite + Qdrant 组件） */
  backend: HealthResponse | null;
  /** AI Worker 子进程状态 */
  worker: ProcessState;
  /** FastAPI 子进程状态 */
  fastapi: ProcessState;
  /** Qdrant Sidecar 子进程状态 */
  qdrant: ProcessState;
}

export type ProcessState =
  | { state: "not_started" }
  | { state: "starting" }
  | { state: "running"; pid: number }
  | { state: "unavailable"; reason: string }   // 如 Qdrant 二进制缺失
  | { state: "stopped"; code: number | null };

// ---------- Search（M5：Hybrid Search，与 python server/api/schemas.py 对齐） ----------
export type SearchMode = "keyword" | "semantic" | "hybrid";

export interface SearchRequest {
  query: string;
  topK?: number;
  mode?: SearchMode; // 缺省 hybrid（M5 §14：默认 Hybrid）
}

export interface TimeRangeFilter {
  /** offset-aware RFC3339（如 2026-08-14T00:00:00+08:00，architecture.md §8） */
  from: string;
  to: string;
  basis_hint: string; // "exif" | "ctime" | "mtime"
}

export interface ParsedQuery {
  time_range: TimeRangeFilter | null;
  file_types: string[];
  extensions: string[];
  semantic_text: string;
  parse_method: "rule" | "fallback";
}

export interface MatchReason {
  channel: string; // keyword | body | ocr | semantic | metadata
  text: string;
  score?: number | null;
  basis?: string | null;      // metadata（时间）：exif | mtime | ctime
  confidence?: string | null; // metadata（时间）：exact | fallback
}

export interface TimeInfo {
  basis: string | null;
  confidence: string | null;
  value: string | null; // RFC3339（本地时区）
}

export interface SearchResultItem {
  file_id: number;
  path: string;
  filename: string;
  dir_path: string;
  extension: string;
  file_type: string;
  size_bytes: number;
  mtime_ns: number;
  rrf_score: number | null;     // RRF 融合分（仅 FTS + Vector，§12.4）；metadata-only = null
  keyword_score: number | null; // BM25 原始分；未命中通道 = null（§12.5）
  semantic_score: number | null; // cosine；未命中通道 = null
  time_info: TimeInfo;
  match_reasons: MatchReason[];
}

export interface SearchResponse {
  query: string;
  parsed: ParsedQuery;
  total: number;
  latency_ms: number;
  results: SearchResultItem[];
  degraded_channels: string[]; // ["keyword" | "semantic"]（§12.8）
}

// ---------- Semantic Search（M4 独立通道；M5 保留兼容） ----------
export interface SemanticSearchResultItem {
  file_id: number;
  path: string;
  filename: string;
  source_type: "doc_chunk" | "ocr" | "image_caption";
  chunk_index: number;
  text: string;
  semantic_score: number;
}

export interface SemanticSearchResponse {
  query: string;
  total: number;
  latency_ms: number;
  results: SemanticSearchResultItem[];
}

// ---------- Settings（M5 §16） ----------
export interface SettingsResponse {
  search_mode: SearchMode;
  w_kw: number;
  w_sem: number;
  topK: number;
  index_roots: string[];
  models: Record<string, string>; // {bge: ok|missing, caption: ok|missing}
  storage: { db_bytes: number; models_bytes: number };
}

export interface SettingsUpdate {
  search_mode?: SearchMode;
  w_kw?: number;
  w_sem?: number;
  topK?: number;
}

// ---------- Task Dashboard（M5 §17） ----------
export interface TaskStats {
  queue_length: number; // PENDING
  processing: number;   // RUNNING
  success: number;
  failed: number;
  total: number;
}

export interface FailedTaskItem {
  id: number;
  file_id: number;
  filename: string;
  attempt: number;
  max_attempts: number;
  last_error: string | null;
}

export interface TaskRetryResponse {
  status: string; // "retried" | "MAX_ATTEMPTS_EXCEEDED" | "TASK_NOT_FOUND"
}

// ---------- IPC Channels（architecture.md §4.2） ----------
export const IPC = {
  systemStatus: "system:status",
  healthEvents: "health:events",
  searchQuery: "search:query",   // M5：Hybrid Search
  searchSemantic: "search:semantic", // M4：语义通道独立（兼容）
  settingsGet: "settings:get",
  settingsSet: "settings:set",
  taskStatus: "task:status",
  taskFailed: "task:failed",
  taskRetry: "task:retry",
} as const;

// ---------- preload 暴露的 API（window.omnisearch） ----------
export interface OmnisearchApi {
  getSystemStatus(): Promise<SystemStatus>;
  /** 订阅健康事件推送，返回取消订阅函数 */
  onHealthStatus(cb: (status: SystemStatus) => void): () => void;
  search(req: SearchRequest): Promise<SearchResponse>; // M5：Hybrid
  searchSemantic(req: SearchRequest): Promise<SemanticSearchResponse>; // M4 兼容
  getSettings(): Promise<SettingsResponse>;
  setSettings(patch: SettingsUpdate): Promise<SettingsResponse>;
  getTaskStatus(): Promise<TaskStats>;
  getFailedTasks(): Promise<FailedTaskItem[]>;
  retryTask(taskId: number): Promise<TaskRetryResponse>;
}
