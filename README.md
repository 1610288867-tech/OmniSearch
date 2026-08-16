# OmniSearch

**本地 AI Hybrid Retrieval Engine** —— Everything 级文件名检索 + 自然语言查询理解 + 图片/文档内容语义理解，全部数据与推理留在本机。

> 「昨天的自由女神照片」 → 解析出时间（昨天 → EXIF exact）、类型（照片 → image）、语义（自由女神 → 图片内容理解）→ FTS + Vector 双通道召回 → RRF 融合 → 逐条匹配原因。

## 核心能力

| 能力 | 说明 |
|---|---|
| **Hybrid Search** | Metadata 过滤 + SQLite FTS5 关键词检索 + Qdrant 语义检索 → RRF 融合（k=60）→ 匹配原因 + 时间可信度标注 |
| **AI Worker 解耦** | FastAPI（搜索，轻载）与 AI 密集任务（OCR / Caption / Embedding）进程级隔离，模型缺失自动降级 |
| **Windows 增量索引** | Watchdog 实时监听 + USN Journal 启动恢复（P2.1）+ content_hash AI 结果复用（P2.2） |
| 多扫描位置 | 任意盘符/文件夹多 Root，启用/禁用/移除（保留已索引数据）、顺序扫描进度 |
| 文件名毫秒检索 | FTS5 contentless-delete，中文 jieba 预分词 |
| 文档全文检索 | TXT / MD / PDF / DOCX → 正文命中（含高亮片段证据） |
| OCR 文字搜索 | 图片内文字（zh+en）→ 关键词通道（「识别到文字：New York 2026」） |
| 图片语义搜索 | 图片 → 中文标签（Chinese-CLIP）→ 统一 BGE 语义空间（「AI 描述：…」） |
| 自然语言查询 | 规则 QueryParser：今天/昨天/日期区间/类型/扩展名 → FilterChips 展示解析结果 |
| 纯过滤查询 | 「昨天的照片」「pdf」→ Metadata-only（时间 basis 排序，不参与 RRF） |
| 时间可信度 | EXIF=exact / mtime·ctime=fallback，逐条标注 |
| 运维 | Settings（权重/topK/模型状态）、Task Dashboard（队列统计/重试）、健康检查（sqlite/qdrant/worker/semantic） |

## 技术栈

- **桌面端**：Electron + Vue3 + Pinia + TypeScript + Vite（contextIsolation / sandbox 安全基线）
- **后端**：Python 3.11 + FastAPI（Router → Service → Repository 分层）
- **存储**：SQLite（WAL，**事实数据源**）+ FTS5 + Qdrant Sidecar（HNSW 语义检索，**可重建索引**）
- **AI**：独立 Python Worker（SQLite 轻量持久化队列）｜PaddleOCR + Chinese-CLIP + BGE-small-zh（全部本地 ONNX CPU）

## 快速开始

**环境**：Windows 10/11（NTFS）、Python 3.11+、Node.js 18+

```bash
# 1. 安装
cd python && python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"
cd ../desktop && npm install

# 2. 下载模型（manifest 驱动 + sha256 校验，~800MB → dev-data/models）
python python/scripts/download_models.py

# 3. 启动（A：纯后端四进程；B：完整桌面应用）
python python/scripts/dev.py --dev-data dev-data
cd desktop && OMNISEARCH_PYTHON="<repo>/python/.venv/Scripts/python.exe" OMNISEARCH_DEV_DATA="<repo>/dev-data" npm run dev
```

使用：桌面 UI 搜索框输入自然语言，或直接调用 API（token 见 dev.py 启动输出）：

```
POST /api/v1/index/scan  {"root_paths": ["D:/photos"], "scan_type": "full"}
POST /api/v1/search {"query": "昨天的自由女神照片", "topK": 50}
```

**模型缺失或 Qdrant 未启动时自动降级为关键词搜索**（degraded_channels 如实标注，服务不崩溃）。

## API 概览

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 就绪探针（sqlite / qdrant / worker / semantic） |
| POST | `/search` | Hybrid Search：`{query, topK, mode}` → `{parsed, results, latency_ms, degraded_channels}` |
| POST | `/index/scan` · GET `/index/status` | 扫描作业 / 进度 |
| GET/POST | `/index/roots` · add/remove/toggle | 扫描位置管理（多 Root） |
| GET/PUT | `/settings` | 搜索模式 / 权重 / topK / 模型状态 / 存储 |
| GET | `/task/status` · `/task/failed` · POST `/task/{id}/retry` | 任务队列 / 重试 |

详细字段见 [docs/api.md](docs/api.md)。

## 测试

```bash
cd python && ./.venv/Scripts/python -m pytest    # 259 项（扫描/FTS/OCR/语义/Hybrid/降级/USN/hash 复用…）
cd desktop && npx vitest run                     # 23 项（store / 页面渲染）
cd desktop && npm run typecheck                  # renderer + electron 双 tsconfig
```

## 文档

- 📐 [架构设计](docs/architecture.md) —— 唯一架构权威文档（18 章 + Final Consistency Checklist）
- 📝 [决策记录](docs/adr/) —— ADR-001~007（Worker 进程 / SQLite 队列 / Hybrid RRF / Caption 冻结 / FTS 与 point_id / USN 恢复 / hash 复用）
- 📡 [API 文档](docs/api.md) —— 端点与字段明细（与前后端契约对齐）

## 目录结构

```
OmniSearch/
├── desktop/          # Electron + Vue3（main / preload / renderer / shared 契约）
├── python/
│   ├── omnisearch/
│   │   ├── common/   # 唯一共享层（SQLite 门面 / 分词 / 时间 / 向量 / hash）
│   │   ├── server/   # FastAPI（api / service / repository / database migrations）
│   │   └── worker/   # AI Worker（pipeline / providers / task 队列）
│   ├── scripts/      # dev.py / download_models.py / benchmark.py / e2e_p21.py / e2e_p22.py
│   └── tests/        # pytest（259 项）
├── docs/             # architecture.md + adr/ + api.md
├── models/           # 模型 manifest.json（模型本体在 dev-data，不入库）
├── qdrant/           # Sidecar 配置（二进制按需放置，不入库）
└── dev-data/         # 开发期数据（.gitignore 排除，可重建）
```
