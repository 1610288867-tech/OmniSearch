import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Renderer 构建（Electron Main 开发模式加载 dev server，生产加载 dist）
export default defineConfig({
  root: "src/renderer",
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
    host: "127.0.0.1", // 修复：Windows 上默认 localhost 只监听 IPv6 ::1，与 Electron loadURL/其他进程不一致
  },
  build: {
    outDir: "../../dist",
    emptyOutDir: true,
  },
});
