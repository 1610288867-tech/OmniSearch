/**
 * IPC handler 注册（architecture.md §4.2）。
 * - system:status / health:events：四进程健康状态
 * - search:query：M5 Hybrid Search（keyword/semantic/hybrid 经 mode）
 * - settings:* / task:*：M5 Settings + Task Dashboard
 */
import { BrowserWindow, dialog, ipcMain } from "electron";
import * as fs from "node:fs";
import {
  IPC,
  type FailedTaskItem,
  type IndexRoot,
  type IndexStatusResponse,
  type RootsResponse,
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

  // 扫描位置管理：原生对话框与盘符枚举只在 Main 进程（Renderer 不接触 fs/dialog）
  ipcMain.handle(IPC.rootAddDialog, async (): Promise<string | null> => {
    const win = BrowserWindow.getFocusedWindow() ?? undefined;
    const result = await dialog.showOpenDialog(win as never, {
      title: "选择要搜索的文件夹",
      properties: ["openDirectory"],
    });
    return result.canceled || result.filePaths.length === 0 ? null : result.filePaths[0];
  });

  ipcMain.handle(IPC.rootListDrives, (): string[] => {
    // Windows 本机可用盘符（A-Z 存在即列出；Root 为盘符根）
    const drives: string[] = [];
    for (let ch = 65; ch <= 90; ch++) {
      const letter = String.fromCharCode(ch);
      try {
        if (fs.existsSync(`${letter}:\\`)) drives.push(`${letter}:\\`);
      } catch {
        /* 无权限/软驱等：跳过 */
      }
    }
    return drives;
  });

  ipcMain.handle(IPC.rootList, (): Promise<RootsResponse> => guard(() => backend.getRoots()));
  ipcMain.handle(IPC.rootAdd, (_evt, path: string): Promise<IndexRoot> => guard(() => backend.addRoot(path)));
  ipcMain.handle(IPC.rootRemove, (_evt, path: string): Promise<RootsResponse> => guard(() => backend.removeRoot(path)));
  ipcMain.handle(IPC.rootToggle, (_evt, path: string, enabled: boolean): Promise<IndexRoot> =>
    guard(() => backend.toggleRoot(path, enabled)),
  );
  ipcMain.handle(IPC.indexStatus, (): Promise<IndexStatusResponse> => guard(() => backend.getIndexStatus()));
}
