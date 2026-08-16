<script setup lang="ts">
import { computed } from "vue";
import { useSearchStore } from "../stores/search";
import type { SearchMode } from "../../../shared/contracts";

const store = useSearchStore();

// M5 §14：关键词 → 语义 → 混合（默认 Hybrid）循环
const MODE_ORDER: SearchMode[] = ["hybrid", "keyword", "semantic"];
const MODE_LABEL: Record<SearchMode, string> = { hybrid: "混合", keyword: "关键词", semantic: "语义" };

const modeLabel = computed(() => MODE_LABEL[store.mode]);
const placeholder = computed(() =>
  store.mode === "keyword"
    ? "搜索文件名/正文/OCR..."
    : "自然语言搜索（如：昨天的自由女神照片）...",
);

function cycleMode(): void {
  const next = MODE_ORDER[(MODE_ORDER.indexOf(store.mode) + 1) % MODE_ORDER.length];
  store.setMode(next);
}
</script>

<template>
  <div class="search-bar">
    <input
      v-model="store.query"
      type="text"
      :placeholder="placeholder"
      spellcheck="false"
      :disabled="store.searching"
      @keyup.enter="store.search()"
    />
    <button
      class="mode-btn"
      :data-active="store.mode === 'hybrid'"
      :disabled="store.searching"
      :title="'当前模式：' + modeLabel"
      @click="cycleMode"
    >
      {{ modeLabel }}
    </button>
    <button :disabled="store.searching || !store.query.trim()" @click="store.search()">
      {{ store.searching ? "搜索中..." : "搜索" }}
    </button>
  </div>
</template>

<style scoped>
.search-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
input {
  flex: 1;
  padding: 10px 14px;
  font-size: 15px;
  border: 1px solid #ccc;
  border-radius: 8px;
}
button {
  padding: 10px 20px;
  font-size: 14px;
  border: none;
  border-radius: 8px;
  background: #2563eb;
  color: #fff;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.mode-btn {
  background: #6b7280;
  min-width: 72px;
}
.mode-btn[data-active="true"] {
  background: #7c3aed;
}
</style>
