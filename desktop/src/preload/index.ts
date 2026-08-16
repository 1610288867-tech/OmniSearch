/**
 * preload —— contextBridge 暴露类型化 API（architecture.md §4.2 / §12 安全基线）。
 * sandbox: true 下仅可使用 electron 有限模块；经 ipcRenderer.invoke 走 main 进程。
 */
import { contextBridge, ipcRenderer } from "electron";
import {
  IPC,
  type FailedTaskItem,
  type OmnisearchApi,
  type SearchRequest,
  type SettingsUpdate,
  type SystemStatus,
  type TaskRetryResponse,
  type TaskStats,
} from "../shared/contracts";

const api: OmnisearchApi = {
  getSystemStatus: () => ipcRenderer.invoke(IPC.systemStatus) as Promise<SystemStatus>,
  onHealthStatus: (cb) => {
    const listener = (_evt: unknown, status: SystemStatus) => cb(status);
    ipcRenderer.on(IPC.healthEvents, listener);
    return () => ipcRenderer.removeListener(IPC.healthEvents, listener);
  },
  search: (req: SearchRequest) => ipcRenderer.invoke(IPC.searchQuery, req),
  searchSemantic: (req: SearchRequest) => ipcRenderer.invoke(IPC.searchSemantic, req),
  getSettings: () => ipcRenderer.invoke(IPC.settingsGet),
  setSettings: (patch: SettingsUpdate) => ipcRenderer.invoke(IPC.settingsSet, patch),
  getTaskStatus: () => ipcRenderer.invoke(IPC.taskStatus) as Promise<TaskStats>,
  getFailedTasks: () => ipcRenderer.invoke(IPC.taskFailed) as Promise<FailedTaskItem[]>,
  retryTask: (taskId: number) => ipcRenderer.invoke(IPC.taskRetry, taskId) as Promise<TaskRetryResponse>,
};

contextBridge.exposeInMainWorld("omnisearch", api);
