/**
 * 健康监控（architecture.md §2.3：Main 每 5s 探测 FastAPI /health，连续失败判定）。
 * M0：探测 FastAPI（含 SQLite/Qdrant 组件状态），结果推送给 Renderer；崩溃自愈为 P2。
 */
import type { HealthResponse, SystemStatus } from "../../shared/contracts";
import type { ProcessManager } from "./ProcessManager";

const POLL_INTERVAL_MS = 5000;

export class HealthMonitor {
  constructor(private readonly pm: ProcessManager) {}

  private timer: NodeJS.Timeout | null = null;

  start(onStatus: (status: SystemStatus) => void): void {
    this.timer = setInterval(() => {
      void this.poll().then(onStatus);
    }, POLL_INTERVAL_MS);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async poll(): Promise<SystemStatus> {
    let backend: HealthResponse | null = null;
    try {
      const resp = await fetch(`${this.pm.fastapiUrl}/health`, { signal: AbortSignal.timeout(1500) });
      if (resp.ok) backend = (await resp.json()) as HealthResponse;
    } catch {
      backend = null; // FastAPI 尚未就绪或已退出
    }
    return {
      backend,
      worker: this.pm.getProcessState("worker"),
      fastapi: this.pm.getProcessState("fastapi"),
      qdrant: this.pm.getProcessState("qdrant"),
    };
  }
}
