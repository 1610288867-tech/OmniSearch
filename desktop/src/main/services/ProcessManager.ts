/**
 * 子进程编排（architecture.md §2.3 / §4.3）。
 * - spawn FastAPI / AI Worker / Qdrant（Sidecar）
 * - 端口：FastAPI 8734 顺延；Qdrant HTTP/gRPC 成对顺延（6333/6334 → 6335/6336 → …）
 * - 生成并注入本机 token（X-Omni-Token 鉴权，architecture.md §12）
 * - Qdrant 二进制缺失 → 标记 unavailable（不阻塞其余组件）
 */
import { spawn, type ChildProcess } from "node:child_process";
import * as crypto from "node:crypto";
import * as net from "node:net";
import * as path from "node:path";
import * as fs from "node:fs";
import type { ProcessState } from "../../shared/contracts";

const FASTAPI_PORT_BASE = 8734;
const QDRANT_HTTP_BASE = 6333;
const QDRANT_GRPC_BASE = 6334;

export interface ManagedProcess {
  state: ProcessState;
  child: ChildProcess | null;
}

function portInUse(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = net.createConnection({ host: "127.0.0.1", port });
    s.on("connect", () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });
}

async function findFreePort(base: number): Promise<number> {
  let port = base;
  while ((await portInUse(port)) && port < base + 100) port += 1;
  return port;
}

/** Qdrant HTTP/gRPC 必须成对分配（architecture.md §4.3），端口间隔恒为 1。 */
async function findFreePortPair(baseHttp: number, baseGrpc: number): Promise<[number, number]> {
  let http = baseHttp;
  while (http < baseHttp + 100) {
    const grpc = http + (baseGrpc - baseHttp); // 保持 6333/6334 的相邻关系
    if (!(await portInUse(http)) && !(await portInUse(grpc))) return [http, grpc];
    http += 2;
  }
  throw new Error(`no free port pair near ${baseHttp}`);
}

export class ProcessManager {
  private readonly procs: Record<string, ManagedProcess> = {
    fastapi: { state: { state: "not_started" }, child: null },
    worker: { state: { state: "not_started" }, child: null },
    qdrant: { state: { state: "not_started" }, child: null },
  };

  private backendUrl = "";
  private token = "";

  get fastapiUrl(): string {
    return this.backendUrl;
  }

  get authToken(): string {
    return this.token;
  }

  async start(): Promise<void> {
    this.token = crypto.randomBytes(16).toString("hex");
    const fastapiPort = await findFreePort(FASTAPI_PORT_BASE);
    const [qdrantHttp, qdrantGrpc] = await findFreePortPair(QDRANT_HTTP_BASE, QDRANT_GRPC_BASE);
    this.backendUrl = `http://127.0.0.1:${fastapiPort}`;

    const pythonDir = path.resolve(__dirname, "../../../../python");
    const dataDir = process.env.OMNISEARCH_DEV_DATA
      ? path.resolve(process.env.OMNISEARCH_DEV_DATA)
      : path.join(process.env.LOCALAPPDATA ?? path.join(process.env.USERPROFILE ?? ".", "AppData/Local"), "OmniSearch");

    // 可用 venv 解释器：OMNISEARCH_PYTHON（如 .venv/Scripts/python.exe）→ 回退 PATH 中的 python
    const pythonBin = process.env.OMNISEARCH_PYTHON ?? "python";

    // 公共环境：token + 端口 + 数据目录（FastAPI/Worker 共享，与 dev.py 注入一致）
    const baseEnv: NodeJS.ProcessEnv = {
      ...process.env,
      OMNISEARCH_DEV_DATA: dataDir,
      OMNISEARCH_TOKEN: this.token,
      OMNISEARCH_FASTAPI_PORT: String(fastapiPort),
      OMNISEARCH_QDRANT_HTTP_PORT: String(qdrantHttp),
      OMNISEARCH_QDRANT_GRPC_PORT: String(qdrantGrpc),
      OMNISEARCH_POLL_INTERVAL_MS: "500",
    };

    // ---- Qdrant Sidecar（二进制缺失 → unavailable）----
    const qdrantBin =
      process.env.OMNISEARCH_QDRANT_BIN ??
      path.resolve(__dirname, "../../../../qdrant/bin/qdrant.exe");
    if (fs.existsSync(qdrantBin)) {
      this.spawnProcess("qdrant", qdrantBin, [], {
        ...baseEnv,
        QDRANT__SERVICE__HTTP_PORT: String(qdrantHttp),
        QDRANT__SERVICE__GRPC_PORT: String(qdrantGrpc),
        QDRANT__STORAGE__STORAGE_PATH: path.join(dataDir, "qdrant"),
        QDRANT__TELEMETRY_DISABLED: "true",
      });
    } else {
      this.procs.qdrant.state = {
        state: "unavailable",
        reason: `qdrant.exe not found (${qdrantBin})`,
      };
    }

    // ---- FastAPI（先启动并等待就绪——架构 §2.3 启动顺序）----
    this.spawnProcess(
      "fastapi",
      pythonBin,
      ["-m", "uvicorn", "omnisearch.server.main:app", "--host", "127.0.0.1", "--port", String(fastapiPort), "--log-level", "info"],
      baseEnv,
      pythonDir,
    );
    await this.waitForBackendReady(fastapiPort, 30000);

    // ---- AI Worker（server 就绪后启动：schema 已迁移，避免 claim 竞态）----
    this.spawnProcess("worker", pythonBin, ["-m", "omnisearch.worker"], baseEnv, pythonDir);
  }

  private spawnProcess(
    key: "fastapi" | "worker" | "qdrant",
    bin: string,
    args: string[],
    env: NodeJS.ProcessEnv,
    cwd?: string,
  ): void {
    const entry = this.procs[key];
    const child = spawn(bin, args, { env, cwd, stdio: ["ignore", "pipe", "pipe"] });
    entry.child = child;
    entry.state = { state: "running", pid: child.pid ?? -1 };

    const label = `[${key}]`;
    child.stdout?.on("data", (d: Buffer) => process.stdout.write(`${label} ${d.toString()}`));
    child.stderr?.on("data", (d: Buffer) => process.stderr.write(`${label} ${d.toString()}`));
    child.on("exit", (code) => {
      entry.child = null;
      if (entry.state.state === "running") {
        entry.state = { state: "stopped", code };
      }
    });
    child.on("error", (err) => {
      entry.child = null;
      entry.state = { state: "stopped", code: -1 };
      process.stderr.write(`${label} spawn error: ${err.message}\n`);
    });
  }

  /** 等待 FastAPI /health 就绪（架构 §2.3：Worker 必须在 schema 迁移完成后启动）。 */
  private async waitForBackendReady(port: number, timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const resp = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1000) });
        if (resp.ok) return;
      } catch {
        // 未就绪，继续等待
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    process.stderr.write(`[main] WARNING: FastAPI not ready in ${timeoutMs}ms; worker may start before migration\n`);
  }

  getProcessState(key: "fastapi" | "worker" | "qdrant"): ProcessState {
    return this.procs[key].state;
  }

  stopAll(): void {
    for (const key of ["fastapi", "worker", "qdrant"] as const) {
      const child = this.procs[key].child;
      if (child && !child.killed) {
        child.kill(); // SIGTERM（优雅退出 drain 为 P2）
      }
    }
  }
}
