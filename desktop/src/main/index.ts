/**
 * Electron Main 入口（architecture.md §4）。
 * 职责：创建窗口（安全基线：contextIsolation/sandbox）、spawn 三子进程（FastAPI/Worker/Qdrant）、
 * 健康监控、IPC 注册、退出时清理子进程。
 */
import { app, BrowserWindow, type WebContents } from "electron";
import * as path from "node:path";
import { registerIpcHandlers } from "./ipc/handlers";
import { ProcessManager } from "./services/ProcessManager";
import { HealthMonitor } from "./services/HealthMonitor";

let mainWindow: BrowserWindow | null = null;
const processManager = new ProcessManager();
const healthMonitor = new HealthMonitor(processManager);

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    title: "OmniSearch",
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      contextIsolation: true, // 安全基线（architecture.md §12）
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // dev：vite dev server（5173，strictPort）；prod：构建产物
  // 修复：VITE_DEV_SERVER_URL 未注入时 dev 模式仍应走 dev server，
  //       否则会 loadFile 不存在的 dist/index.html → 白屏
  const isDev = process.env.NODE_ENV === "development";
  const devServerUrl = process.env.VITE_DEV_SERVER_URL ?? "http://localhost:5173";
  if (isDev) {
    void mainWindow.loadURL(devServerUrl);
  } else {
    void mainWindow.loadFile(path.join(__dirname, "../../dist/index.html"));
  }

  // ============ GUI 自动验证信号（verify-omnisearch Skill 检查依据） ============
  // E2 修正：探针仅开发模式执行（生产构建不得注入搜索词/自动切 tab；isDev 定义见上方）
  mainWindow.webContents.on("did-finish-load", () => {
    console.log("[main] renderer loaded OK");
    if (!isDev) return;
    // 延迟等待 Vue 挂载 + store 初始化后探测 bridge / DOM / 搜索 smoke
    setTimeout(() => {
      if (mainWindow) {
        void probeRenderer(mainWindow.webContents);
      }
    }, 2000);
  });
  mainWindow.webContents.on("did-fail-load", (_evt, code, desc) => {
    console.error(`[main] renderer load FAILED: ${code} ${desc}`);
  });

  // renderer console 错误捕获（uncaught/preload 运行时错误 → 日志可 grep）
  mainWindow.webContents.on("console-message", (_evt: unknown, levelOr: unknown, messageOr?: string) => {
    const level = typeof levelOr === "object" && levelOr !== null ? (levelOr as { level?: number }).level : (levelOr as number | undefined);
    const message = typeof levelOr === "object" && levelOr !== null ? (levelOr as { message?: string }).message : messageOr;
    const isError = level === 3 || (typeof level === "string" && level === "error");
    if (isError && message && /(uncaught|error|failed|unable to load)/i.test(message)) {
      console.error(`[main] renderer-console-error: ${message}`);
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  registerIpcHandlers(processManager);
  await processManager.start(); // spawn FastAPI / Worker / Qdrant（端口探测 + token 注入）
  healthMonitor.start((status) => {
    mainWindow?.webContents.send("health:events", status);
  });
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

/**
 * GUI 自动验证探针（verify-omnisearch Skill 检查）：
 * executeJavaScript 在 renderer 主世界执行，探测 bridge 可用性 + 关键 DOM + 搜索 smoke。
 * 结果以 [main] gui-smoke: <json> 输出（Skill grep 校验）。
 */
async function probeRenderer(wc: WebContents): Promise<void> {
  const script = `
    (async () => {
      const out = { hasApp: false, hasSearchInput: false, modeBtn: null, bridge: null, smoke: null };
      out.hasApp = !!document.getElementById('app') && document.getElementById('app').childElementCount > 0;
      const input = document.querySelector('.search-bar input');
      out.hasSearchInput = !!input;
      const modeBtn = document.querySelector('.mode-btn');
      out.modeBtn = modeBtn ? modeBtn.getAttribute('data-active') : null;
      out.bridge = {
        omnisearch: typeof window.omnisearch,
        onHealthStatus: typeof (window.omnisearch && window.omnisearch.onHealthStatus),
        search: typeof (window.omnisearch && window.omnisearch.search),
      };
      if (input && window.omnisearch && typeof window.omnisearch.search === 'function') {
        // GUI 搜索 smoke：输入已知文件名（E2E verify-root 数据）→ 检查结果卡片渲染
        input.value = 'verify-alpha';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
        await new Promise((r) => setTimeout(r, 1500));
        out.smoke = {
          cards: document.querySelectorAll('.result-card').length,
          totalText: (document.querySelector('.summary') || {}).textContent || '',
        };
        // M5 收口 3：metadata-only GUI smoke（纯过滤查询 → FilterChips + 结果卡）
        input.value = '昨天的照片';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
        await new Promise((r) => setTimeout(r, 1500));
        out.metaSmoke = {
          chips: document.querySelectorAll('.filter-chips .chip').length,
          cards: document.querySelectorAll('.result-card').length,
          totalText: (document.querySelector('.summary') || {}).textContent || '',
        };
      }
      // M5 收口 4：semantic readiness badge（语义模型不可用 ≠ FastAPI 崩溃）
      const semBadge = Array.from(document.querySelectorAll('.badge')).find((b) => b.textContent.includes('语义'));
      out.semantic = semBadge ? { badgeOk: semBadge.getAttribute('data-ok'), text: (semBadge.textContent || '').trim() } : null;

      // M5 页面 smoke：任务 Dashboard + 设置页真实渲染（tab 切换 + 数据加载）
      const tabs = Array.from(document.querySelectorAll('.tab'));
      const tabClick = async (label) => {
        const t = tabs.find((b) => b.textContent === label);
        if (!t) return false;
        t.click();
        await new Promise((r) => setTimeout(r, 1200));
        return true;
      };
      if (await tabClick('任务')) {
        out.tasks = { statCards: document.querySelectorAll('.stat').length, hasFailed: !!document.querySelector('.failed, .hint') };
      }
      if (await tabClick('设置')) {
        const page = document.querySelector('.settings-page');
        const btns = page ? Array.from(page.querySelectorAll('button')) : [];
        out.settings = {
          hasPage: !!page,
          hasCards: (page ? page.querySelectorAll('.card').length : 0),
          // 扫描位置管理：添加按钮 + Root 列表 + 开关/删除
          roots: (page ? page.querySelectorAll('.root').length : 0),
          hasAddFolder: btns.some((b) => b.textContent.includes('添加文件夹')),
          hasAddDrive: btns.some((b) => b.textContent.includes('添加磁盘')),
          hasToggle: !!page.querySelector('.root .toggle'),
          hasRemove: !!page.querySelector('.root .danger'),
          // 模型状态真实渲染（settings 500 回归：models 显示"就绪"）
          modelsReady: page ? page.querySelectorAll('.models li span[data-ok="true"]').length : 0,
        };
      }
      return out;
    })()
  `;
  try {
    const result = await wc.executeJavaScript(script, true);
    console.log(`[main] gui-smoke: ${JSON.stringify(result)}`);
  } catch (err) {
    console.error(`[main] gui-smoke FAILED: ${err instanceof Error ? err.message : String(err)}`);
  }
}

// 退出清理：终止子进程（优雅退出 drain 为 P2，architecture.md §15.2）
app.on("before-quit", () => {
  healthMonitor.stop();
  processManager.stopAll();
});
