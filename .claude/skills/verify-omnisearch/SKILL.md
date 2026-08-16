---
name: verify-omnisearch
description: OmniSearch 项目统一验证流程 —— pytest / vitest / tsc（renderer+electron）/ 四进程真实启动（FastAPI / AI Worker / Qdrant Sidecar / Electron）/ 可选 E2E / 退出清理，输出 PASS/FAIL 报告。用于任何里程碑（M0/M1/...）后的回归验证。只验证和报告，不修复代码。
---

# Verify OmniSearch

对当前 OmniSearch 项目执行统一验证流程并输出报告。
**只验证与报告，不修改业务代码；失败不自动修复，仅给出排查方向。**

## 项目环境（必须知道的事实）

- Windows 11 + Git Bash shell（命令用 bash 语法）
- 四进程体系：Electron Main → spawn FastAPI / AI Worker / Qdrant（Sidecar 二进制 `qdrant/bin/qdrant.exe`）
- Python venv：`python/.venv/Scripts/python.exe`（自动发现；缺失则回退 `OMNISEARCH_PYTHON`）
- desktop：`node_modules/` 已安装；Electron 受系统级 `ELECTRON_RUN_AS_NODE=1` 影响（npm script 已内置 `set ELECTRON_RUN_AS_NODE=` 处理，**不要在外部 shell 覆盖**）
- 开发数据目录：`dev-data/`（`--dev-data dev-data` 注入；脚本内相对路径一律 `.resolve()` 为绝对路径）

## 执行步骤

按顺序执行；每步记录结果标记：**PASS / SKIPPED / FAIL**（环境原因无法执行 → SKIPPED，不判整体失败）。

### 步骤 1：后端测试（pytest）

```bash
cd python && ./.venv/Scripts/python -m pytest -q
```

通过标准：全部 passed，无 FAILED。输出 `pytest: <N> passed`。

### 步骤 2：前端测试（vitest）

```bash
cd desktop && npx vitest run
```

通过标准：Test Files / Tests 全部 passed。

### 步骤 3：TypeScript 类型检查（双 tsconfig）

```bash
cd desktop && npx tsc --noEmit -p tsconfig.json && npx tsc --noEmit -p tsconfig.electron.json
```

通过标准：两条命令均无输出错误（exit 0）。注意：类型检查不产出构建产物；Electron main/preload 编译用 `npx tsc -p tsconfig.electron.json`（dist-electron）。

### 步骤 4：真实开发环境启动（FastAPI + Worker + Qdrant）

后台启动（`run_in_background`，不要用 `&` 否则子进程随 shell 退出被回收）：

```bash
cd <repo> && python python/scripts/dev.py --dev-data dev-data
```

等待约 10-12s 后验证：

| 检查项 | 命令 | 通过标准 |
|---|---|---|
| FastAPI /health | `curl -s -m 5 http://127.0.0.1:8734/health` | JSON `status=ok` 或 `degraded`（qdrant 缺失时 degraded 仍算环境原因 SKIPPED）；`components.sqlite.ok=true` 必须 |
| Qdrant | 上述 /health 的 `components.qdrant.ok=true`；或 `curl http://127.0.0.1:6333/healthz` | 200 |
| Worker heartbeat | 从 dev.py 后台输出/日志 grep `heartbeat: alive` | 5s 周期出现 |

**注意**：端口可能顺延（8734/6333 被占用时）——从 dev.py 输出读取实际端口与 token（`grep -oP 'token=\K[0-9a-f]+'`）。

### 步骤 5：Electron 全链路（环境支持时）

确保步骤 4 已停止（Electron 自己会 spawn 三进程，端口冲突）后。

**前置检查（实测必做）**：前次 Electron 会话可能残留 vite dev server 占用 5173 导致启动失败：

```bash
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"
```

有输出 → 先 `taskkill //PID <pid> //F` 清理（该进程为残留 vite，命令行含 `vite/bin/vite.js`，确认后再杀）。另外确认 8734 空闲（`curl http://127.0.0.1:8734/health` 无响应）。

```bash
cd desktop && OMNISEARCH_PYTHON="<repo>/python/.venv/Scripts/python.exe" OMNISEARCH_DEV_DATA="<repo>/dev-data" npm run dev
```

等待约 30-35s，验证：

| 检查项 | 命令 | 通过标准 |
|---|---|---|
| 进程 | `tasklist` grep `electron.exe`/`python.exe`/`qdrant.exe` | electron ≥1、python ≥1、qdrant=1 |
| FastAPI（Electron spawn） | `curl -s -m 5 http://127.0.0.1:8734/health` | sqlite.ok=true |
| HealthMonitor | 后台输出 grep `GET /health` | 5s 周期 200 |
| Worker heartbeat | 后台输出 grep `heartbeat: alive` | 周期出现 |

**GUI 自动验证信号（从 Electron 输出日志 grep，防「假通过」）**：

| 信号 | 判定 |
|---|---|
| `[main] renderer loaded OK` | 出现 → Renderer load PASS；出现 `renderer load FAILED` → **FAIL** |
| `Unable to load preload script` | 一旦出现 → **FAIL**（preload 加载失败） |
| `[main] renderer-console-error:` | 一旦出现 → **FAIL**（uncaught TypeError/Error/Vue error/preload runtime error） |
| `[main] gui-smoke: {json}` | JSON 校验：`hasApp=true`、`hasSearchInput=true`、`bridge.omnisearch="object"`、`bridge.onHealthStatus="function"`、`bridge.search="function"`、`smoke.cards ≥ 1`（GUI 搜索 smoke 命中结果卡）；任一不满足 → **FAIL** |

**⚠️ gui-smoke 依赖**：搜索 smoke 使用 E2E 步骤（步骤 6）在 verify-root 扫描产生的 `verify-alpha.pdf` 数据——**步骤 6 必须先于本步骤执行，且 verify-root 的清理推迟到本步骤之后**。

**PASS 判定（Electron 步骤）**：Renderer + Preload + Bridge + 关键 DOM + Search smoke 全部通过。
**SKIPPED**：当前环境无法执行 GUI 自动检查（headless 等）——必须明确说明原因。
**FAIL**：preload / bridge / uncaught runtime error / 关键 DOM 缺失。

人工 GUI 检查（视觉布局/样式/缩放/体验）为独立项，Skill 不截图判断美观——由用户人工确认。

### 步骤 6：E2E（扫描 + 搜索，使用临时目录）——**必须先于步骤 5 执行**

仅当步骤 4 的 FastAPI 可用且持有 token 时执行（不依赖 M1/M2 业务数据）：

**0. 前置清理（M5 收口 6）**：每次验证开始前清理旧的 verify-root 与残留的失败任务，避免人为 FAILED 状态残留在默认 dev-data：
```bash
rm -rf <repo>/dev-data/verify-root
```
（若上次验证中断导致 dev-data/db 残留任务，可整体重置：`rm -rf dev-data/db dev-data/qdrant dev-data/logs dev-data/verify-root`——SQLite dev DB / Qdrant dev data 均为可重建开发数据）

1. 建临时目录 `<dev-data>/verify-root/`，放入带文件名特征的文件（**必须包含 `verify-alpha.pdf`**，GUI smoke 依赖）
2. `POST /api/v1/index/scan`（`X-Omni-Token` 头）→ 等 job DONE（轮询 `/api/v1/index/status`）
3. `POST /api/v1/search {"query":"verify-alpha"}` → total ≥ 1
4. **暂不清理** verify-root（步骤 5 的 GUI smoke 需要该数据仍在索引中）

通过标准：scan job DONE 且 search 命中。E2E 失败 → FAIL（但若仅因 token 无法获取，标 SKIPPED 并说明）。

### 步骤 7：退出清理（必须）

1. 停止后台任务（TaskStop 或向 dev.py/Electron 进程发 SIGTERM）
2. 残留清理：`powershell "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'electron|qdrant' -or ($_.Name -eq 'node.exe' -and $_.CommandLine -match 'vite') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"`（仅限本项目相关；先确认无其他用户 Electron 进程）
3. 清理 E2E 临时数据（**此阶段才清理**——步骤 6 的 verify-root 持久化了 index root；步骤 5 的 gui-smoke 已消费该数据）：
   ```bash
   rm -rf dev-data/verify-root dev-data/db dev-data/qdrant dev-data/logs   # 重置测试数据（仅 dev 数据，非业务代码）
   ```
   **明确规则（M5 收口 6）**：dev-data/{db,qdrant,logs,verify-root} 均为可重建开发数据——
   SQLite dev DB（db/）由扫描重建；Qdrant dev data（qdrant/，向量索引）由 Worker 重建；
   默认 dev 环境不允许残留人为 FAILED 任务（Task Dashboard 的 FAILED 状态由 pytest 覆盖，
   不依赖真实运行数据）。
4. 验证无残留：`tasklist` grep `electron.exe|qdrant.exe` → 0；5173 无监听；`curl /health` 无响应

残留 > 0 → FAIL（说明退出清理不完整）。

## 输出格式

```
Verification Summary
- pytest:            <N> passed | FAIL | SKIPPED
- vitest:            <N> passed | FAIL | SKIPPED
- tsc renderer:      OK | FAIL | SKIPPED
- tsc electron:      OK | FAIL | SKIPPED
- FastAPI health:    ok/degraded | FAIL | SKIPPED
- Qdrant:            ok | FAIL | SKIPPED
- Worker heartbeat:  ok | FAIL | SKIPPED
- Electron:
    Renderer load:   OK | FAIL | SKIPPED   (renderer loaded OK / load FAILED)
    Preload:         OK | FAIL | SKIPPED   (无 Unable to load preload)
    Bridge:          OK | FAIL | SKIPPED   (gui-smoke: omnisearch/onHealthStatus/search)
    DOM:             OK | FAIL | SKIPPED   (gui-smoke: hasApp/hasSearchInput)
    Search smoke:    OK | FAIL | SKIPPED   (gui-smoke: cards ≥ 1)
    Runtime errors:  none | FAIL           (renderer-console-error 出现即 FAIL)
- E2E:               ok | FAIL | SKIPPED
- Process cleanup:   clean | FAIL

Result: PASS | FAIL
```

- **PASS**：全部必选项通过（SKIPPED 不计为失败，但需说明原因）
- **FAIL**：任一 FAIL → 列出失败步骤、关键错误输出、建议排查方向（进程/日志/端口/依赖），**不修改业务代码**
- 步骤 4/5 中端口被占用等环境问题：记录实际端口并继续，不要误判
- Electron 步骤的 PASS 判定 = Renderer + Preload + Bridge + 关键 DOM + Search smoke 均通过；人工 GUI 检查（视觉/样式/缩放/体验）为独立项，由用户人工确认

## 边界（不要做）

- 不修复任何代码（即使测试失败）
- 不引入新基础设施、不改配置
- 不硬编码 M1/M2 业务断言（本 Skill 只验证项目通用能力：测试/类型/进程/健康/搜索 API 可用性）
- 不创建 Hook
