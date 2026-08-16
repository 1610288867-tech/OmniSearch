/**
 * IPC handler 注册（architecture.md §4.2）。
 * - system:status / health:events：四进程健康状态
 * - search:query：M5 Hybrid Search（keyword/semantic/hybrid 经 mode）
 * - settings:* / task:*：M5 Settings + Task Dashboard
 */
import { ipcMain } from "electron";
import {
  IPC,
  type FailedTaskItem,
  type SearchRequest,
  type SearchResponse,
  type SemanticSearchResponse,
  type SettingsResponse,
  type SettingsUpdate,
  type TaskRetryResponse,
  type TaskStats,
} from "../../shared/contracts";
import { BackendClient, BackendError } from "../services/BackendClient";
import { HealthMonitor } from "../services/HealthMonitor";
import type { ProcessManager } from "../services/ProcessManager";

function guard<T>(fn: () => Promise<T>): Promise<T> {
  return fn().catch((err) => {
    if (err instanceof BackendError) throw err;
    throw new BackendError("BACKEND_UNAVAILABLE", "backend not reachable");
  });
}

export function registerIpcHandlers(pm: ProcessManager): void {
  const monitor = new HealthMonitor(pm);
  const backend = new BackendClient(
    () => pm.fastapiUrl,
    () => pm.authToken,
  );

  ipcMain.handle(IPC.systemStatus, () => monitor.poll());

  // M5：Hybrid Search（mode: keyword | semantic | hybrid）
  ipcMain.handle(IPC.searchQuery, (_evt, req: SearchRequest): Promise<SearchResponse> =>
    guard(() => backend.search(req)),
  );

  // M4 兼容：语义通道独立
  ipcMain.handle(IPC.searchSemantic, (_evt, req: SearchRequest): Promise<SemanticSearchResponse> =>
    guard(() => backend.searchSemantic(req)),
  );

  // M5 §16 Settings
  ipcMain.handle(IPC.settingsGet, (): Promise<SettingsResponse> => guard(() => backend.getSettings()));
  ipcMain.handle(IPC.settingsSet, (_evt, patch: SettingsUpdate): Promise<SettingsResponse> =>
    guard(() => backend.setSettings(patch)),
  );

  // M5 §17 Task Dashboard
  ipcMain.handle(IPC.taskStatus, (): Promise<TaskStats> => guard(() => backend.getTaskStatus()));
  ipcMain.handle(IPC.taskFailed, (): Promise<FailedTaskItem[]> => guard(() => backend.getFailedTasks()));
  ipcMain.handle(IPC.taskRetry, (_evt, taskId: number): Promise<TaskRetryResponse> =>
    guard(() => backend.retryTask(taskId)),
  );
}
