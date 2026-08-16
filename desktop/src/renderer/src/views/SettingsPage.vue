<script setup lang="ts">
/**
 * SettingsPage —— M5 §16 + 扫描位置管理（产品增强）。
 * 扫描位置：添加文件夹（原生对话框）/ 添加磁盘（盘符枚举，均经 Main 进程）/
 * 多 Root 列表（启用/禁用/删除）/ 多 Root 顺序扫描进度。
 */
import { onMounted, onUnmounted, ref, watch } from "vue";
import { useSettingsStore } from "../stores/settings";
import { useIndexStore } from "../stores/index";
import type { SearchMode } from "../../../shared/contracts";

const store = useSettingsStore();
const index = useIndexStore();

const MODE_LABEL: Record<SearchMode, string> = { hybrid: "混合（FTS + 语义）", keyword: "关键词（FTS）", semantic: "语义（Vector）" };
const showDrives = ref(false);
const adding = ref(false);

// 扫描进度轮询：有 RUNNING 作业时每 2s 刷新（多 Root 顺序扫描）
let pollTimer: ReturnType<typeof setInterval> | null = null;
watch(
  () => index.jobs,
  (jobs) => {
    const running = jobs.some((j) => j.status === "RUNNING");
    if (running && !pollTimer) {
      pollTimer = setInterval(() => void index.loadStatus(), 2000);
    } else if (!running && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  },
);

onMounted(() => {
  void store.load();
  void index.loadRoots();
  void index.loadStatus();
});
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
});

async function pickFolder(): Promise<void> {
  adding.value = true;
  try {
    const path = await window.omnisearch.pickRootFolder();
    if (path) await index.addRoot(path);
  } finally {
    adding.value = false;
  }
}

async function pickDrive(): Promise<void> {
  if (!showDrives.value) await index.loadDrives();
  showDrives.value = !showDrives.value;
}

async function addDrive(drive: string): Promise<void> {
  showDrives.value = false;
  await index.addRoot(drive);
}

function fmtCount(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
</script>

<template>
  <div class="settings-page">
    <h2>设置</h2>
    <p v-if="store.error" class="error">{{ store.error }}</p>
    <p v-if="index.error" class="error">{{ index.error }}</p>

    <!-- ================= 扫描位置（产品增强） ================= -->
    <section class="card">
      <h3>扫描位置</h3>
      <div class="actions">
        <button class="primary" :disabled="adding" @click="pickFolder">+ 添加文件夹</button>
        <button :disabled="adding" @click="pickDrive">+ 添加磁盘</button>
      </div>
      <div v-if="showDrives" class="drives">
        <button
          v-for="d in index.drives"
          :key="d"
          class="drive"
          @click="addDrive(d)"
        >{{ d }}</button>
        <span v-if="!index.drives.length" class="hint">未检测到可用盘符</span>
      </div>

      <!-- 空状态：首次启动引导「选择要搜索的位置」 -->
      <div v-if="!index.roots.length" class="onboarding">
        <p>选择要搜索的位置</p>
        <p class="hint">添加文件夹或磁盘后开始索引；可以稍后跳过，随时在此添加。</p>
      </div>

      <!-- 扫描进度（多 Root 顺序扫描） -->
      <div v-if="index.runningJob()" class="scanning">
        扫描中：<b>{{ index.runningJob()?.root_path }}</b>
        <span v-if="index.runningJob()?.total_files">
          {{ fmtCount(index.runningJob()?.scanned_files ?? 0) }} / {{ fmtCount(index.runningJob()?.total_files ?? 0) }}
        </span>
        <span v-if="index.jobs.length > 1" class="hint">· 当前 Root {{ index.runningRootIndex() }} / {{ index.jobs.length }}</span>
      </div>

      <ul class="roots">
        <li v-for="r in index.roots" :key="r.path" class="root" :data-disabled="!r.enabled">
          <div class="root-main">
            <span class="root-path">{{ r.path }}</span>
            <span class="root-meta">{{ fmtCount(r.file_count) }} 个文件 · 添加于 {{ new Date(r.created_at * 1000).toLocaleDateString() }}</span>
          </div>
          <div class="root-actions">
            <button class="toggle" :data-on="r.enabled" :title="r.enabled ? '点击禁用（停止监听）' : '点击启用（恢复监听）'"
                    @click="index.toggleRoot(r.path, !r.enabled)">
              {{ r.enabled ? "监听中" : "已禁用" }}
            </button>
            <button class="danger" @click="index.removeRoot(r.path)">删除</button>
          </div>
        </li>
      </ul>
      <p class="hint">移除扫描位置不会删除已有索引数据；已有文件仍可能出现在搜索结果中。</p>
    </section>

    <!-- ================= 搜索 ================= -->
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
.actions {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
button {
  padding: 6px 14px;
  font-size: 13px;
  border: none;
  border-radius: 6px;
  background: #e5e7eb;
  color: #374151;
  cursor: pointer;
}
button.primary {
  background: #2563eb;
  color: #fff;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.drives {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.drive {
  background: #eef2ff;
  color: #4338ca;
}
.onboarding {
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 10px;
  text-align: center;
}
.onboarding p {
  margin: 0 0 4px;
  font-size: 14px;
  color: #374151;
}
.scanning {
  font-size: 13px;
  color: #1d4ed8;
  background: #eff6ff;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
}
.roots {
  list-style: none;
  margin: 0;
  padding: 0;
}
.root {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  margin-bottom: 6px;
}
.root[data-disabled="true"] {
  opacity: 0.55;
}
.root-main {
  min-width: 0;
}
.root-path {
  display: block;
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}
.root-meta {
  font-size: 11px;
  color: #9ca3af;
}
.root-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.toggle[data-on="true"] {
  background: #dcfce7;
  color: #15803d;
}
.toggle[data-on="false"] {
  background: #fee2e2;
  color: #b91c1c;
}
.danger {
  background: #fef2f2;
  color: #b91c1c;
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
.models, .storage {
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
