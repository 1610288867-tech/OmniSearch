/**
 * system store —— 四进程健康状态（architecture.md §2.1）。
 * 数据来源：preload window.omnisearch（health:events 推送 + system:status 主动查询）。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { SystemStatus } from "../../../shared/contracts";

const UNKNOWN: SystemStatus = {
  backend: null,
  worker: { state: "not_started" },
  fastapi: { state: "not_started" },
  qdrant: { state: "not_started" },
};

export const useSystemStore = defineStore("system", () => {
  const status = ref<SystemStatus>(UNKNOWN);
  const lastUpdated = ref<number | null>(null);

  function apply(next: SystemStatus): void {
    status.value = next;
    lastUpdated.value = Date.now();
  }

  async function refresh(): Promise<void> {
    const next = await window.omnisearch.getSystemStatus();
    apply(next);
  }

  /** 订阅 health:events 推送；返回取消订阅函数（组件卸载时调用）。 */
  function subscribe(): () => void {
    return window.omnisearch.onHealthStatus(apply);
  }

  return { status, lastUpdated, refresh, subscribe };
});
