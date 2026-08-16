/**
 * index store —— 扫描位置管理（Roots）+ 多 Root 扫描进度。
 * 数据源：window.omnisearch（对话框/盘符仅经 Main 进程，Renderer 不接触 fs）。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { IndexJob, IndexRoot } from "../../../shared/contracts";

export const useIndexStore = defineStore("index", () => {
  const roots = ref<IndexRoot[]>([]);
  const jobs = ref<IndexJob[]>([]);
  const drives = ref<string[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  /** 正在运行的作业（多 Root 顺序扫描 → 当前 root 序号 = 该作业在 jobs 中的位置） */
  const runningJob = (): IndexJob | null => jobs.value.find((j) => j.status === "RUNNING") ?? null;

  const runningRootIndex = (): number => {
    const j = runningJob();
    if (!j) return 0;
    const idx = jobs.value.findIndex((x) => x.id === j.id);
    return idx >= 0 ? idx + 1 : 0;
  };

  async function loadRoots(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const resp = await window.omnisearch.getRoots();
      roots.value = resp.roots;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "扫描位置加载失败";
    } finally {
      loading.value = false;
    }
  }

  async function loadDrives(): Promise<void> {
    try {
      drives.value = await window.omnisearch.listDrives();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "盘符枚举失败";
    }
  }

  async function loadStatus(): Promise<void> {
    try {
      jobs.value = (await window.omnisearch.getIndexStatus()).jobs;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "扫描进度加载失败";
    }
  }

  async function addRoot(path: string): Promise<{ ok: boolean; message?: string }> {
    error.value = null;
    try {
      await window.omnisearch.addRoot(path);
      await Promise.all([loadRoots(), loadStatus()]);
      return { ok: true };
    } catch (e) {
      const msg = e instanceof Error ? e.message : "添加失败";
      error.value = msg;
      return { ok: false, message: msg };
    }
  }

  async function removeRoot(path: string): Promise<void> {
    try {
      await window.omnisearch.removeRoot(path);
      await loadRoots();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "删除失败";
    }
  }

  async function toggleRoot(path: string, enabled: boolean): Promise<void> {
    try {
      await window.omnisearch.toggleRoot(path, enabled);
      await loadRoots();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "切换失败";
    }
  }

  return {
    roots, jobs, drives, loading, error,
    runningJob, runningRootIndex, loadRoots, loadDrives, loadStatus, addRoot, removeRoot, toggleRoot,
  };
});
