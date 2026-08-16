import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// 独立于 vite.config.ts（该配置 root=src/renderer，会影响测试解析）
// vue 插件：支持 .vue 单文件组件测试（SearchPage.test.ts）
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
