<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useSystemStore } from "./stores/system";
import SearchPage from "./views/SearchPage.vue";
import SettingsPage from "./views/SettingsPage.vue";
import TaskDashboard from "./views/TaskDashboard.vue";

const system = useSystemStore();
let unsubscribe: (() => void) | null = null;

const TABS = [
  { key: "search", label: "搜索" },
  { key: "tasks", label: "任务" },
  { key: "settings", label: "设置" },
] as const;
const activeTab = ref<(typeof TABS)[number]["key"]>("search");

const backendOk = computed(() => system.status.backend != null);
const workerState = computed(() => system.status.worker.state);
// M5 收口 4：semantic readiness（语义模型不可用 ≠ FastAPI 崩溃，仅语义通道降级）
const semanticReady = computed(() => {
  const b = system.status.backend;
  return b?.components.semantic?.ok ?? false;
});

onMounted(() => {
  unsubscribe = system.subscribe();
  void system.refresh();
});
onUnmounted(() => unsubscribe?.());
</script>

<template>
  <main class="shell">
    <header class="topbar">
      <h1>OmniSearch</h1>
      <nav class="tabs">
        <button
          v-for="t in TABS"
          :key="t.key"
          class="tab"
          :data-active="activeTab === t.key"
          @click="activeTab = t.key"
        >
          {{ t.label }}
        </button>
      </nav>
      <div class="badges">
        <span class="badge" :data-ok="backendOk">FastAPI {{ backendOk ? "●" : "○" }}</span>
        <span class="badge" :data-ok="workerState === 'running'">Worker {{ workerState === "running" ? "●" : "○" }}</span>
        <span class="badge" :data-ok="semanticReady" :title="semanticReady ? '语义通道就绪' : '语义模型不可用（关键词搜索正常，语义自动降级）'">
          语义 {{ semanticReady ? "●" : "○" }}
        </span>
      </div>
    </header>
    <SearchPage v-if="activeTab === 'search'" />
    <TaskDashboard v-else-if="activeTab === 'tasks'" />
    <SettingsPage v-else />
  </main>
</template>

<style scoped>
.shell {
  font-family: system-ui, "Segoe UI", sans-serif;
  max-width: 860px;
  margin: 0 auto;
  padding: 20px 24px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  color: #1a1a1a;
  box-sizing: border-box;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
}
h1 {
  margin: 0;
  font-size: 22px;
}
.tabs {
  display: flex;
  gap: 4px;
}
.tab {
  padding: 6px 16px;
  font-size: 13px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #6b7280;
  cursor: pointer;
}
.tab[data-active="true"] {
  background: #eef2ff;
  color: #4338ca;
  font-weight: 600;
}
.badges {
  display: flex;
  gap: 8px;
}
.badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 10px;
  background: #eee;
  color: #888;
}
.badge[data-ok="true"] {
  background: #dcfce7;
  color: #166534;
}
</style>
