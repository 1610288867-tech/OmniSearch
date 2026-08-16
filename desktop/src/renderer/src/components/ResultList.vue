<script setup lang="ts">
import { computed } from "vue";
import { useSearchStore } from "../stores/search";
import ResultCard from "./ResultCard.vue";

const store = useSearchStore();

const degradedText = computed(() =>
  store.degradedChannels
    .map((c) => (c === "keyword" ? "关键词通道不可用" : "语义通道不可用"))
    .join("；"),
);
</script>

<template>
  <div class="result-list">
    <!-- loading -->
    <p v-if="store.searching" class="state">搜索中...</p>
    <!-- error -->
    <p v-else-if="store.error" class="state error">搜索失败：{{ store.error }}</p>
    <!-- empty：已搜索且无结果 -->
    <p v-else-if="store.searched && store.results.length === 0" class="state">
      未找到匹配「{{ store.query }}」的内容
    </p>
    <!-- results -->
    <template v-else-if="store.results.length">
      <p class="summary">
        共 {{ store.total }} 个结果（{{ store.latencyMs }} ms）
        <span v-if="degradedText" class="degraded">· {{ degradedText }}</span>
      </p>
      <ResultCard v-for="item in store.results" :key="item.file_id" :item="item" />
    </template>
    <!-- 未搜索 -->
    <p v-else class="state">输入关键词开始搜索</p>
  </div>
</template>

<style scoped>
.result-list {
  overflow-y: auto;
}
.state {
  text-align: center;
  color: #888;
  padding: 40px 0;
}
.state.error {
  color: #b91c1c;
}
.summary {
  font-size: 12px;
  color: #888;
  margin: 0 0 8px;
}
.degraded {
  color: #b45309;
}
</style>
