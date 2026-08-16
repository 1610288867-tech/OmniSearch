<script setup lang="ts">
/**
 * TaskDashboard —— M5 §17：queue length / processing / success / failed + retry。
 * retry：attempt >= max_attempts → MAX_ATTEMPTS_EXCEEDED（只能 reindex，§7.1）。
 */
import { onMounted } from "vue";
import { useTasksStore } from "../stores/tasks";

const store = useTasksStore();

onMounted(() => void store.load());

async function retry(taskId: number): Promise<void> {
  const status = await store.retry(taskId);
  if (status === "MAX_ATTEMPTS_EXCEEDED") {
    alert("已达最大重试次数（MAX_ATTEMPTS_EXCEEDED）——只能重建索引（reindex）");
  }
}
</script>

<template>
  <div class="task-dashboard">
    <h2>任务队列</h2>
    <p v-if="store.error" class="error">{{ store.error }}</p>

    <section class="stats">
      <div class="stat"><b>{{ store.stats.queue_length }}</b><span>排队中</span></div>
      <div class="stat"><b>{{ store.stats.processing }}</b><span>处理中</span></div>
      <div class="stat ok"><b>{{ store.stats.success }}</b><span>成功</span></div>
      <div class="stat bad"><b>{{ store.stats.failed }}</b><span>失败</span></div>
      <div class="stat"><b>{{ store.stats.total }}</b><span>总计</span></div>
    </section>

    <section v-if="store.failed.length" class="failed">
      <h3>失败任务（{{ store.failed.length }}）</h3>
      <table>
        <thead>
          <tr><th>文件</th><th>尝试</th><th>错误</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="t in store.failed" :key="t.id">
            <td class="name">{{ t.filename }}</td>
            <td>{{ t.attempt }}/{{ t.max_attempts }}</td>
            <td class="err">{{ t.last_error || "—" }}</td>
            <td><button @click="retry(t.id)">重试</button></td>
          </tr>
        </tbody>
      </table>
    </section>
    <p v-else class="hint">没有失败任务</p>
  </div>
</template>

<style scoped>
.task-dashboard {
  overflow-y: auto;
  padding: 4px 2px 20px;
}
h2 {
  margin: 0 0 12px;
  font-size: 18px;
}
.stats {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.stat {
  flex: 1;
  border: 1px solid #e2e2e2;
  border-radius: 10px;
  padding: 12px;
  text-align: center;
  background: #fff;
}
.stat b {
  display: block;
  font-size: 22px;
}
.stat span {
  font-size: 12px;
  color: #6b7280;
}
.stat.ok b { color: #15803d; }
.stat.bad b { color: #b91c1c; }
.failed h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  background: #fff;
  border: 1px solid #e2e2e2;
  border-radius: 8px;
  overflow: hidden;
}
th, td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #f0f0f0;
}
th {
  background: #f9fafb;
  color: #6b7280;
}
.name { font-weight: 600; max-width: 220px; word-break: break-all; }
.err { color: #b91c1c; max-width: 260px; word-break: break-all; }
button {
  padding: 4px 12px;
  font-size: 12px;
  border: none;
  border-radius: 6px;
  background: #2563eb;
  color: #fff;
  cursor: pointer;
}
.hint, .error {
  font-size: 13px;
  color: #9ca3af;
}
.error { color: #b91c1c; }
</style>
