/// <reference types="vite/client" />

// 纯 ambient 文件（无顶层 import，保证 wildcard module 声明生效）。
// Window.omnisearch 的类型声明见 src/preload/index.d.ts。
declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>;
  export default component;
}
