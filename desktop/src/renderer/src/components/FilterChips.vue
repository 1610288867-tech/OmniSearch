<script setup lang="ts">
/**
 * FilterChips —— M5 §14：QueryParser 结构化结果展示（时间/类型/扩展名）。
 * 数据来自 search 响应 parsed（架构 §12.2 UnifiedFilter）。
 */
import { computed } from "vue";
import { useSearchStore } from "../stores/search";

const store = useSearchStore();

const chips = computed<Array<{ text: string; kind: string }>>(() => {
  const p = store.parsed;
  if (!p || (!p.time_range && p.file_types.length === 0 && p.extensions.length === 0)) {
    return [];
  }
  const out: Array<{ text: string; kind: string }> = [];
  if (p.time_range) {
    out.push({ text: `${p.time_range.from} ~ ${p.time_range.to}`, kind: "time" });
  }
  for (const t of p.file_types) {
    out.push({ text: t === "image" ? "图片" : "文档", kind: "type" });
  }
  for (const e of p.extensions) {
    out.push({ text: `.${e}`, kind: "ext" });
  }
  return out;
});
</script>

<template>
  <div v-if="chips.length" class="filter-chips">
    <span v-for="(c, i) in chips" :key="i" class="chip" :data-kind="c.kind">{{ c.text }}</span>
    <span v-if="store.parsed.semantic_text" class="sem-text">「{{ store.parsed.semantic_text }}」</span>
  </div>
</template>

<style scoped>
.filter-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.chip {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 10px;
  background: #f3f4f6;
  color: #374151;
}
.chip[data-kind="time"] {
  background: #dbeafe;
  color: #1d4ed8;
}
.chip[data-kind="type"] {
  background: #fce7f3;
  color: #be185d;
}
.chip[data-kind="ext"] {
  background: #dcfce7;
  color: #15803d;
}
.sem-text {
  font-size: 12px;
  color: #6b7280;
  align-self: center;
}
</style>
