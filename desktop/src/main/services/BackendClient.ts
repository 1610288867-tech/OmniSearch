/**
 * FastAPI HTTP 客户端（architecture.md §13 / §12 安全基线）。
 * 本机 token（X-Omni-Token）由 Main 注入；错误统一抛 {code, message}。
 */
import type {
  FailedTaskItem,
  SearchRequest,
  SearchResponse,
  SemanticSearchResponse,
  SettingsResponse,
  SettingsUpdate,
  TaskRetryResponse,
  TaskStats,
} from "../../shared/contracts";

export class BackendError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "BackendError";
  }
}

export class BackendClient {
  constructor(
    private readonly baseUrl: () => string,
    private readonly token: () => string,
  ) {}

  private async post<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
    const resp = await fetch(`${this.baseUrl()}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Omni-Token": this.token() },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) {
      const detail = await resp.text().catch(() => "");
      throw new BackendError("BACKEND_ERROR", `HTTP ${resp.status}: ${detail.slice(0, 200)}`);
    }
    return (await resp.json()) as T;
  }

  private async get<T>(path: string, timeoutMs: number): Promise<T> {
    const resp = await fetch(`${this.baseUrl()}${path}`, {
      headers: { "X-Omni-Token": this.token() },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) {
      throw new BackendError("BACKEND_ERROR", `HTTP ${resp.status}`);
    }
    return (await resp.json()) as T;
  }

  // M5：Hybrid Search（关键词/语义/混合；首次语义查询模型已 warmup）
  search(req: SearchRequest): Promise<SearchResponse> {
    return this.post<SearchResponse>(
      "/api/v1/search",
      { query: req.query, topK: req.topK ?? 50, mode: req.mode ?? "hybrid" },
      15000,
    );
  }

  // M4 兼容：语义通道独立（UI 已合并进 /search）
  searchSemantic(req: SearchRequest): Promise<SemanticSearchResponse> {
    return this.post<SemanticSearchResponse>(
      "/api/v1/search/semantic",
      { query: req.query, topK: req.topK ?? 50 },
      15000,
    );
  }

  // M5 §16 Settings
  getSettings(): Promise<SettingsResponse> {
    return this.get<SettingsResponse>("/api/v1/settings", 5000);
  }

  setSettings(patch: SettingsUpdate): Promise<SettingsResponse> {
    return this.put<SettingsResponse>("/api/v1/settings", patch);
  }

  private async put<T>(path: string, body: unknown): Promise<T> {
    const resp = await fetch(`${this.baseUrl()}${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Omni-Token": this.token() },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) {
      throw new BackendError("BACKEND_ERROR", `HTTP ${resp.status}`);
    }
    return (await resp.json()) as T;
  }

  // M5 §17 Task Dashboard
  getTaskStatus(): Promise<TaskStats> {
    return this.get<TaskStats>("/api/v1/task/status", 5000);
  }

  getFailedTasks(): Promise<FailedTaskItem[]> {
    return this.get<FailedTaskItem[]>("/api/v1/task/failed", 5000);
  }

  retryTask(taskId: number): Promise<TaskRetryResponse> {
    return this.post<TaskRetryResponse>(`/api/v1/task/${taskId}/retry`, {}, 5000);
  }
}
