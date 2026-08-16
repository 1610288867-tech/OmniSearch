# OmniSearch

**本地 AI Hybrid Retrieval Engine** —— Everything 级文件名检索 + 自然语言查询理解 + 图片/文档内容语义理解。用一句话搜到本地任何文件。

> 「昨天的自由女神照片」 → QueryParser 解析出时间条件（昨天 → EXIF exact）、类型条件（照片 → image）、语义条件（自由女神 → 图片内容理解）→ Hybrid 双通道召回 → RRF 融合 → 逐条匹配原因。

## 三大核心亮点

| # | 亮点 | 构成 |
|---|---|---|
| 1 | **Hybrid Search** | Metadata Filter（过滤）+ SQLite FTS5（关键词检索）+ Qdrant（语义检索）→ 加权 RRF 融合（k=60，仅 FTS+Vector 双通道）→ 逐条匹配原因 + 时间可信度标注 |
| 2 | **AI Worker 解耦** | FastAPI（搜索，永远轻载）与 AI 密集任务（Caption / OCR / Embedding）进程级隔离，模型缺失自动降级 |
| 3 | **Windows Incremental Index** | Watchdog 实时增量监听（防抖 2s / 事件合并 / RENAME 身份保留）→ NTFS USN Journal 启动恢复（P2） |

## 技术栈

- **桌面端**：Electron + Vue3 + Pinia + TypeScript + Vite（contextIsolation / sandbox 安全基线）
- **后端**：Python 3.11 + FastAPI（Router → Service → Repository 分层）
- **存储**：SQLite（WAL，**事实数据源**）+ FTS5（jieba 预分词中文检索）+ Qdrant Sidecar（HNSW 向量语义检索，**可重建索引**）
- **AI**：独立 Python Worker（ai_tasks 单机轻量持久化队列）｜PaddleOCR（zh+en）+ Chinese-CLIP 图片标签 + BGE-small-zh 语义嵌入（全部本地 ONNX CPU）

## MVP 能力（M0–M5 全部完成 ✅）

| 能力 | 说明 |
|---|---|
| 全量/增量扫描 | 8 线程扫描、Watchdog 实时监听（增/改/删/重命名）、删除软标记先行 |
| 文件名毫秒检索 | FTS5 contentless-delete，中文 jieba 前缀查询 |
| 文档全文检索 | TXT / MD / PDF / DOCX → 分块 → 正文命中（含高亮片段证据） |
| OCR 文字搜索 | 图片内文字（zh+en）→ 关键词通道（「识别到文字：New York 2026」） |
| 图片语义搜索 | 图片 → 中文标签（Chinese-CLIP）→ 统一 BGE 语义空间（「AI 描述：…」） |
| 自然语言查询 | 规则 QueryParser：今天/昨天/本周/本月/日期区间/类型/扩展名（LLM 解析为 P2） |
| Hybrid 融合 | 双通道并行（FTS 1s / Vector 3s 超时降级）→ 候选并集 → canonical 过滤 → RRF → 匹配原因 |
| 纯过滤查询 | 「昨天的照片」「pdf」→ Metadata-only（时间 basis 排序，不参与 RRF） |
| 时间可信度 | EXIF=exact / mtime·ctime=fallback，逐条标注；unknown 默认排除 |
| 运维 | Settings（权重/topK/模型状态）、Task Dashboard（队列统计/重试）、健康检查（sqlite/qdrant/worker/semantic readiness） |

## 快速开始

### 环境要求

- Windows 10/11（NTFS）
- Python 3.11+（建议 3.12）
- Node.js 18+ / npm

### 1. 安装

```bash
# 后端（虚拟环境 + 依赖，含 paddle 版本冻结）
cd python
python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"

# 桌面端
cd ../desktop
npm install
```

### 2. 下载模型

```bash
# manifest 驱动 + sha256 校验（BGE-small-zh + Chinese-CLIP + PaddleOCR，~800MB，下载到 dev-data/models）
python python/scripts/download_models.py
```

### 3. 启动开发环境

```bash
# 方式 A：纯后端四进程（FastAPI + Worker + Qdrant Sidecar）
python python/scripts/dev.py --dev-data dev-data

# 方式 B：完整桌面应用（Electron 自动拉起四进程；需指定 venv）
cd desktop
OMNISEARCH_PYTHON="d:/OmniSearch/python/.venv/Scripts/python.exe" OMNISEARCH_DEV_DATA="d:/OmniSearch/dev-data" npm run dev
```

### 4. 使用

```
# 添加索引目录（带 X-Omni-Token，token 见 dev.py 启动输出）
POST /api/v1/index/scan  {"root_paths": ["D:/photos"], "scan_type": "full"}

# Hybrid 搜索（默认模式；mode: keyword | semantic | hybrid）
POST /api/v1/search {"query": "昨天的自由女神照片", "topK": 50}
```

或直接在桌面 UI：搜索框输入自然语言 → FilterChips 展示解析结果 → 结果卡展示 RRF 分数 / 匹配原因 / 时间可信度。**模型缺失或 Qdrant 未启动时自动降级为关键词搜索**（degraded_channels 如实标注，服务不崩溃）。

## API 概览

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 就绪探针：`sqlite / qdrant / worker / semantic` 四组件 |
| POST | `/search` | **Hybrid Search**：`{query, topK, mode, stages?}` → `{parsed, results, total, latency_ms, degraded_channels}` |
| POST | `/search/semantic` | 语义通道独立（兼容） |
| POST | `/index/scan` · GET `/index/status` | 扫描作业 / 进度 |
| GET/PUT | `/settings` | 搜索模式 / w_kw·w_sem / topK / 索引目录 / 模型状态 / 存储 |
| GET | `/task/status` · `/task/failed` | 任务队列统计 / 失败明细 |
| POST | `/task/{id}/retry` · `/task/{id}/reindex` | 重试（超限 → 409）/ 重建任务 |

详细请求/响应字段见 [docs/api.md](docs/api.md)。

## 测试

```bash
# 后端（192 项：扫描/FTS/OCR/语义/Hybrid/降级/时间/任务/Health…）
cd python && ./.venv/Scripts/python -m pytest

# 前端（18 项：store 状态机 / 页面渲染）
cd desktop && npx vitest run

# 类型检查（renderer + electron 双 tsconfig）
cd desktop && npm run typecheck

# 项目级统一验证（四进程 + E2E + Electron GUI + 清理，输出 PASS/FAIL）
# 在 Claude Code 中：/verify-omnisearch
```

## 文档

- 📐 [架构设计](docs/architecture.md) —— 唯一架构权威文档（18 章 + Final Consistency Checklist，MVP/P2/P3 边界）
- 📝 [决策记录](docs/adr/) —— ADR-001~005（Worker 进程模型 / SQLite 队列 / Hybrid RRF / Caption 选型冻结 / FTS 模式与 point_id 冻结）
- 📡 [API 文档](docs/api.md) —— 端点与字段明细（与前后端契约对齐）
- 🤖 [工程规则](CLAUDE.md) —— 面向 AI Coding Agent 的 14 条不可违反规则
- 📋 [需求原文](request.txt)

## 目录结构

```
OmniSearch/
├── desktop/          # Electron + Vue3（main / preload / renderer / shared 契约）
├── python/
│   ├── omnisearch/
│   │   ├── common/   # 唯一共享层（SQLite 门面 / 分词 / 时间 / 向量 / embedding）
│   │   ├── server/   # FastAPI（api / service / repository / database migrations）
│   │   └── worker/   # AI Worker（pipeline / providers / task 队列）
│   ├── scripts/      # dev.py（四进程拉起）/ download_models.py / benchmark.py / e2e_m5.py
│   └── tests/        # pytest（192 项）
├── docs/             # architecture.md + adr/ + api.md
├── models/           # 模型 manifest.json（sha256 校验驱动下载；模型本体在 dev-data，不入库）
├── qdrant/           # Sidecar 配置（qdrant.production.yaml；二进制按需放置，不入库）
└── dev-data/         # 开发期数据（db/qdrant/logs/models，.gitignore 排除，可重建）
```

## 阶段规划

- **Phase 1 — MVP（完成 ✅）**：M0 骨架 → M1 扫描+文件名搜索 → M2 文档全文 → M3 OCR → M4 语义搜索 → M5 Hybrid+产品化（+ 最终收口：timeout 语义 / QueryParser 边界 / Metadata-only / readiness / deadline / 验证清理）
- **Phase 2 — 工程增强**：USN Journal 启动恢复、断点续扫、content_hash 复用、Worker 自动重试/优雅退出、崩溃自愈、本地 LLM Query Parser、Query 缓存、缩略图缓存、托盘+快捷键、打包发布
- **Phase 3 — Future Extensions**：云端 VLM、以图搜图（CLIP）、人脸识别、语音搜索、视频理解、插件系统、跨设备同步
