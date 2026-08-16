import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";

const app = createApp(App);
app.use(createPinia());

// 诊断：Vue 渲染错误定位（console-message 会被 verify-omnisearch 捕获）
app.config.errorHandler = (err, instance, info) => {
  const comp = (instance as { $options?: { name?: string } } | undefined)?.$options?.name ?? "?";
  console.error(`[vue-error] ${info} (${comp}): ${err instanceof Error ? err.message : String(err)}`);
};

app.mount("#app");
