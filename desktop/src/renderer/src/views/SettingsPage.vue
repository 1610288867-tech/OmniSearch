<script setup lang="ts">
/**
 * SettingsPage —— M5 §16：search mode / keyword-vector weight / topK /
 * index roots / model status / basic storage。不做复杂配置（架构 MVP 边界）。
 */
import { onMounted } from "vue";
import { useSettingsStore } from "../stores/settings";
import type { SearchMode } from "../../../shared/contracts";

const store = useSettingsStore();

onMounted(() => void store.load());

const MODE_LABEL: Record<SearchMode, string> = { hybrid: "混合（FTS + 语义）", keyword: "关键词（FTS）", semantic: "语义（Vector）" };

function saveMode(e: Event): void {
  void store.save({ search_mode: (e.target as HTMLSelectElement).value });
}
function saveWKw(e: Event): void {
  void store.save({ w_kw: Number((e.target as HTMLInputElement).value) });
}
function saveWSem(e: Event): void {
  void store.save({ w_sem: Number((e.target as HTMLInputElement).value) });
}
function saveTopK(e: Event): void {
  void store.save({ topK: Number((e.target as HTMLInputElement).value) });
}
</script>

<template>
  <div class="settings-page">
    <h2>设置</h2>
    <p v-if="store.error" class="error">{{ store.error }}</p>

    <section class="card">
      <h3>搜索</h3>
      <label>默认模式
        <select :value="store.searchMode" :disabled="store.saving" @change="saveMode">
          <option v-for="(label, m) in MODE_LABEL" :key="m" :value="m">{{ label }}</option>
        </select>
      </label>
      <label>关键词权重 w_kw
        <input type="number" step="0.1" min="0.1" max="10" :value="store.wKw" :disabled="store.saving" @change="saveWKw" />
      </label>
      <label>语义权重 w_sem
        <input type="number" step="0.1" min="0.1" max="10" :value="store.wSem" :disabled="store.saving" @change="saveWSem" />
      </label>
      <label>结果数 topK
        <input type="number" step="10" min="1" max="200" :value="store.topK" :disabled="store.saving" @change="saveTopK" />
      </label>
    </section>

    <section class="card">
      <h3>索引目录</h3>
      <ul v-if="store.indexRoots.length" class="roots">
        <li v-for="r in store.indexRoots" :key="r">{{ r }}</li>
      </ul>
      <p v-else class="hint">尚未添加索引目录（在搜索页无法添加时请重新扫描）</p>
    </section>

    <section class="card">
      <h3>模型</h3>
      <ul class="models">
        <li>BGE（语义嵌入）<span :data-ok="store.models['bge'] === 'ok'">{{ store.models["bge"] === "ok" ? "就绪" : "缺失" }}</span></li>
        <li>Chinese-CLIP（图片描述）<span :data-ok="store.models['caption'] === 'ok'">{{ store.models["caption"] === "ok" ? "就绪" : "缺失" }}</span></li>
      </ul>
    </section>

    <section class="card">
      <h3>存储</h3>
      <ul class="storage">
        <li>数据库：{{ (store.storage.db_bytes / 1024 / 1024).toFixed(1) }} MB</li>
        <li>模型：{{ (store.storage.models_bytes / 1024 / 1024).toFixed(1) }} MB</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.settings-page {
  overflow-y: auto;
  padding: 4px 2px 20px;
}
h2 {
  margin: 0 0 12px;
  font-size: 18px;
}
.card {
  border: 1px solid #e2e2e2;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
  background: #fff;
}
.card h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  color: #374151;
  margin-bottom: 8px;
}
select, input {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid #ccc;
  border-radius: 6px;
  width: 160px;
}
.roots, .models, .storage {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  color: #4b5563;
}
.models li span {
  float: right;
  font-size: 12px;
  color: #b91c1c;
}
.models li span[data-ok="true"] {
  color: #15803d;
}
.hint {
  margin: 0;
  font-size: 12px;
  color: #9ca3af;
}
.error {
  color: #b91c1c;
  font-size: 13px;
}
</style>
