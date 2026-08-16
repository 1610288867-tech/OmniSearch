/**
 * tasks store —— M5 §17 Task Dashboard：queue/processing/success/failed + retry。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { FailedTaskItem, TaskStats } from "../../../shared/contracts";

const EMPTY_STATS: TaskStats = { queue_length: 0, processing: 0, success: 0, failed: 0, total: 0 };

export const useTasksStore = defineStore("tasks", () => {
  const stats = ref<TaskStats>(EMPTY_STATS);
  const failed = ref<FailedTaskItem[]>([]);
  const error = ref<string | null>(null);

  async function load(): Promise<void> {
    try {
      stats.value = await window.omnisearch.getTaskStatus();
      failed.value = await window.omnisearch.getFailedTasks();
      error.value = null;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "任务状态加载失败";
    }
  }

  async function retry(taskId: number): Promise<string> {
    const resp = await window.omnisearch.retryTask(taskId);
    await load();
    return resp.status;
  }

  return { stats, failed, error, load, retry };
});
