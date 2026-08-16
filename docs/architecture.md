# OmniSearch 软件架构设计方案（v4 Final — 实现级技术修正）

> 本版本仅做实现级技术修正，核心架构保持不变。本文档是后续 AI 编码的唯一技术依据。

## Context

桌面端本地 AI 文件搜索软件 **OmniSearch**（需求：[request.txt](../request.txt)）。修复了 FTS5 contentless DELETE 语义、时间过滤 unknown 语义、重新索引旧数据生命周期、QueryParser MVP 边界、端口分配、时区规则等实现级问题。

定位：**本地 AI Hybrid Retrieval Engine**。三大核心亮点：① Hybrid Search（Metadata Filter + FTS5 + Vector → RRF）；② AI Worker 解耦（API 轻载 / AI 任务进程级隔离）；③ Windows Incremental Index（Watchdog 增量，USN Journal 为 P2）。

已确认决策：本地优先（云端 P3）；BGE Embedding 本地 ONNX；**Qdrant 本地 Sidecar 进程**（Electron Main spawn/监控/退出，FastAPI 经 localhost REST/gRPC 通信）；语义搜索覆盖图片+文档；Windows 11。三阶段：Phase 1（MVP）/ Phase 2（工程增强）/ Phase 3（Future Extensions）。

**冻结架构决策（不再修改）**：
- 进程链：Electron Main → FastAPI → SQLite + FTS5 → Qdrant（Sidecar）；AI Worker 独立进程
- 索引链：FastAPI/Index pipeline → SQLite ai_tasks → AI Worker → chunks / FTS5 / Embedding / Qdrant
- 搜索链：Query → QueryParser → UnifiedFilter + semantic_text → Metadata Filter → FTS5 → Vector → candidate union → SQLite canonical filter → RRF(FTS + Vector) → Match Reasons → Results
- 明确：Metadata 不参与 RRF；SQLite 是事实数据源；Qdrant 是可重建索引；ai_tasks 是单机单 Worker 持久化轻量队列
- MVP 不引入：Redis/Celery/Kafka/PostgreSQL/微服务、任务 DAG、CLIP image vector、LLM Query Parser、Provider Registry

---

# 1. 项目定位【MVP】

**一句话**：OmniSearch 是一个**本地 AI Hybrid Retrieval Engine**——Everything 级文件名检索 + 自然语言查询理解 + 图片/文档内容语义理解，一句话搜到本地任何文件，数据与推理留在本机。

**核心亮点（其余能力均为支撑）**：

| # | 亮点 | 构成 |
|---|---|---|
| 1 | **Hybrid Search** | Metadata Filter（过滤）+ SQLite FTS5（关键词检索）+ Qdrant（语义检索）→ RRF 融合 → 匹配原因 |
| 2 | **AI Worker 解耦** | FastAPI（搜索，永远轻载）与 AI 密集任务（Caption/OCR/Embedding）进程级隔离 |
| 3 | **Windows Incremental Index** | Watchdog 实时增量（MVP）→ USN Journal 启动恢复（P2） |

**能力矩阵**：

| 能力 | OmniSearch | 阶段 |
|---|---|---|
| 文件名毫秒检索 | FTS5 contentless | MVP |
| 文档全文检索 | FTS5 external content（doc_chunk） | MVP |
| OCR 文字搜索 | PaddleOCR(本地) → FTS5 + 向量 | MVP |
| 图片内容语义搜索 | Caption → BGE → Qdrant（统一文本语义空间） | MVP |
| 自然语言查询 | 规则解析 + 可选 LLM 增强（离线可用） | MVP |
| 时间/类型过滤 | EXIF exact / mtime fallback / unknown，逐条标注可信度 | MVP |
| USN Journal 启动恢复 / 断点续扫 / hash 复用 / 自动重试 / 优雅退出 / 打包 | — | P2 |
| 云端 VLM / 以图搜图 / 人脸 / 语音 / 视频 / 插件 / 同步 | — | P3 |

---

# 2. 整体架构设计【MVP，标注 P2】

## 2.1 进程拓扑

```
┌──────────────────────────────────────────────────────────────┐
│                     Electron 桌面应用                          │
│  ┌──────────────┐   IPC    ┌───────────────────────────────┐  │
│  │ Renderer     │◄────────►│ Main Process                  │  │
│  │ Vue3/Pinia   │contextBrdg│ · spawn 子进程 + 健康检查(MVP) │  │
│  └──────────────┘          │ · 崩溃自愈/优雅退出/托盘(P2)   │  │
│                            │ · 文件预览协议                  │  │
│                            └──────────┬────────────────────┘  │
└───────────────────────────────────────┼───────────────────────┘
                           spawn(stdio)  端口探测/顺延
        ┌────────────────────────────────┼──────────────────────┐
        │                                ▼                       │
        │  ┌──────────────────┐   ┌──────────────────────────┐  │
        │  │  FastAPI 进程     │──►│  Qdrant Sidecar 进程     │  │
        │  │  Router→Service   │   │  (qdrant.exe 本机模式)   │  │
        │  │  →Repository→DB   │   │  HNSW(初始参数待调优)    │  │
        │  └────┬──────────────┘   └──────────────────────────┘  │
        │       │ SQLite 主库 (WAL) —— 事实数据源                 │
        │       │ files/exif/chunks/ocr_text/ai_tasks/... + FTS5 │
        │  ┌────▼───────────────────────────────────────────┐    │
        │  │  AI Worker 独立 Python 进程（单 Worker）         │    │
        │  │  index_file 任务: Caption / OCR / 提取 / 切分 /  │    │
        │  │  FTS 同步 / BGE Embedding / Qdrant upsert       │    │
        │  └─────────────────────────────────────────────────┘    │
        └─────────────────────────────────────────────────────────┘
```

**进程清单**：Electron Main/Renderer（用户启动）｜FastAPI（Main spawn）｜AI Worker（Main spawn，单实例）｜Qdrant（本地 Sidecar 进程：Electron Main spawn/监控/退出，FastAPI 经 localhost REST/gRPC 通信）｜SQLite（无独立进程，FastAPI/Worker 各自连接，WAL 多进程）。

## 2.2 进程间通信

| 通信对 | 方式 | 说明 |
|---|---|---|
| Renderer ↔ Main | Electron IPC（`contextBridge`） | 唯一入口，类型化 channel |
| Main ↔ FastAPI | HTTP (localhost) | 健康检查/状态转发；搜索走 HTTP |
| FastAPI ↔ AI Worker | **共享 SQLite（ai_tasks 表轮询）** | 单机单 Worker 轻量持久化队列（边界见 §10.2） |
| FastAPI ↔ Qdrant | Qdrant REST/gRPC (localhost) | Repository 层封装 |
| Main ↔ 文件系统 | 自定义协议 `omnisearch://preview/<file_id>` | 白名单校验后读盘 |

## 2.3 生命周期管理

- **MVP**：启动 Main → 数据目录 `%LOCALAPPDATA%/OmniSearch/{db,qdrant,models,logs}` → Qdrant（就绪探针）→ FastAPI（/health）→ Worker（心跳表）→ 通知 Renderer；失败 UI 故障面板+重试；退出按序 kill 子进程 + WAL checkpoint。
- **P2**：崩溃自愈（指数退避重启）、优雅退出 drain、托盘+全局快捷键。

---

# 3. 系统数据流【MVP】

## 3.1 查询链路（最终 Pipeline 语义）

```
Query
  → QueryParser（规则解析 + 可选 LLM 增强）
      → UnifiedFilter {time_range, file_types, extensions, include_deleted}
      + semantic_text（规则无法结构化的残句）
  → SQLite Metadata Filter（过滤正确性事实来源，canonical WHERE）
  → FTS5 Retrieval（关键词：文件名 + 文档正文 + OCR）
  → BGE Query Embedding（semantic_text）
  → Qdrant Semantic Retrieval（image_caption + doc_chunk + ocr）
  → FTS + Vector Candidate Fusion（候选并集，回 SQLite 统一过滤）
  → RRF（仅 FTS + Vector 两通道参与）
  → Match Reasons（含 time_basis / time_confidence）
  → Results
```

通道语义（贯穿全文）：**Metadata = Filter，FTS5 = Keyword Retrieval，Qdrant = Semantic Retrieval，RRF = FTS + Vector**。Metadata 不参与 RRF。

## 3.2 入库链路（文件落盘→可搜索）

```
Watchdog 事件(防抖2s合并) / 全量扫描
  → mtime_ns+size 变化检测
  → SQLite 批量 upsert files 元数据（1000 行/事务）
  → 写入 ai_tasks（task_type=index_file，PENDING；file_type 为 image/doc 才入队）
  → Worker claim（poll_interval_ms=500 默认，可调）→ RUNNING + files.status=PROCESSING
     image:   decode → Caption(chunks image_caption) → OCR(ocr_text + chunks ocr)
              → FTS 同步(doc/ocr) → BGE Embedding(全部 chunks) → Qdrant upsert
     document: 提取 → 切分(chunks doc_chunk) → FTS 同步 → BGE Embedding → Qdrant upsert
  → 任务 SUCCESS + files.status=AI_DONE → 三通道均可命中
```

---

# 4. Electron 架构【MVP 基础，P2 增强】

## 4.1 职责划分

| 职责 | Main | Renderer |
|---|---|---|
| 进程编排（spawn/健康检查/退出清理） | ✅ MVP | — |
| 崩溃自愈、优雅退出、托盘、全局快捷键 | ✅ P2 | — |
| 文件系统（唯一访问方）：预览读取、目录选择 | ✅ MVP | — |
| UI 全部界面与交互 | 窗口/主题管理 | ✅ MVP |

安全基线：`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`；Renderer 仅经 preload 的 `window.omnisearch` 类型化 API。

## 4.2 IPC Channel 设计

| Channel | 方向 | 载荷 → 响应 | 阶段 |
|---|---|---|---|
| `search:query` | R→M | `{query, topK}` → SearchResponse | MVP |
| `index:add-roots` / `index:start-scan` / `index:status` | R→M | 目录管理 / jobId / 进度 | MVP |
| `index:events` | M→R | 扫描/AI 进度事件（轮询兜底） | MVP |
| `task:status` | R→M | AI 队列汇总 | MVP |
| `settings:get/set` | R→M | 设置 JSON | MVP |
| `fs:open-file` / `fs:reveal` / `fs:read-preview` | R→M | 打开/定位/预览 | MVP |
| `provider:test` | R→M | 连通性测试 | P3 |
| `app:quit` | R→M | 优雅退出 | P2 |

契约以 `desktop/src/shared/contracts.ts` 为单一事实源（与后端 OpenAPI 对齐）。

## 4.3 子进程管理与文件系统

- 端口：FastAPI 8734；Qdrant HTTP 6333 + gRPC 6334。**Qdrant 的 HTTP/gRPC 必须作为端口组成对顺延**：6333/6334 被占用 → 6335/6336 → 6337/6338（禁止只顺延 HTTP 而 gRPC 固定）；实际端口由 Electron Main 探测发现并**注入 FastAPI 配置**（避免 Qdrant 启动成功但 FastAPI gRPC 连接错端口）。
- spawn：开发态 `python -m omnisearch.server` + `python -m omnisearch.worker`；生产态 PyInstaller exe（P2）。stdio 重定向日志文件。
- 预览：文本走 `fs:read-preview`；图片走 `omnisearch://` 协议（白名单校验，缩略图缓存 P2）。

---

# 5. Vue3 前端架构【MVP 基础，P2 增强】

## 5.1 页面与组件树

```
App.vue
├── SearchPage.vue
│   ├── SearchBar.vue（输入 + 历史联想）
│   ├── FilterChips.vue（解析出的时间/类型结构化标签）
│   ├── ResultList.vue（vue-virtual-scroller 虚拟滚动）
│   │   └── ResultCard.vue（缩略图/文件名/匹配原因/时间可信度标注）
│   └── PreviewPane.vue（右侧预览）
├── DetailDrawer.vue（FileMeta / AiDescription(chunks image_caption) / MatchReasons / OcrPanel(ocr_text 原始结果)）
├── SettingsPage.vue（IndexRootsPanel / ModelSettings(模型下载进度) / StoragePanel）
├── TaskDashboard.vue（基础版 M5：队列统计 + 失败列表 + 手动重试；增强 P2）
├── StatusBar.vue（索引覆盖率/队列长度）
└── OnboardingWizard.vue（首次启动：选目录→索引进度→模型下载）
```

## 5.2 Pinia Store

`searchStore` / `settingsStore` / `indexStore`（扫描进度）/ `taskStore` / `uiStore`。后端调用经 `api/ipc.ts` 薄封装，错误统一 toast。

---

# 6. FastAPI 后端架构【MVP】

## 6.1 分层职责

```
Router      ：HTTP 边界。Pydantic 校验、本机 token 鉴权、错误映射、OpenAPI
Service     ：业务编排。无 HTTP 概念、无 SQL 直写
Repository  ：数据访问。SQL 与 Qdrant 操作全部封装，返回领域对象
Database    ：Schema 迁移（连接门面位于 common/database.py，见 §14 注）
```

- Repository 拆分：`FileRepository / ChunkRepository / FtsRepository / VectorRepository / TaskRepository / HistoryRepository`。
- Service 模块：`QueryParserService`（→ UnifiedFilter + semantic_text）、`FilterBuilderService`（UnifiedFilter → canonical SQL WHERE）、`SearchService`（双通道调度 + RRF）、`IndexService`（扫描/增量/删除同步）、`AiTaskService`（入队/状态查询/手动重试）、`SettingsService`、`StatService`。
- DI：Composition Root（`core/container.py` 单例 + `Depends` 注入）；Repository 接收**连接工厂**而非连接实例。
- 异步策略：全链路 async；SQLite 同步调用包 `asyncio.to_thread`，Qdrant 异步客户端。

---

# 7. SQLite 数据库设计【MVP，P2 字段标注】

主库 `data/omnisearch.db`。PRAGMA：`WAL` / `synchronous=NORMAL` / `foreign_keys=ON` / `cache_size=-64000` / `busy_timeout=5000`。**SQLite 是事实数据源**。

## 7.1 表结构（要点）

**files（元数据主表）**：`id`(PK=rowid，FTS/Qdrant 关联锚点), `path`(UNIQUE), `filename`, `dir_path`, `extension`, `size_bytes`, `mtime_ns`, `ctime_ns`, `content_hash`(xxh3_64，**P2**), `file_type`(image|doc|video|audio|archive|other), `mime_type`, `status`(**METADATA_ONLY|PROCESSING|AI_DONE|FAILED**), `is_deleted`(软删), `scanned_at`, `created_at`, `updated_at`
索引：`UNIQUE(path)`、`(dir_path)`、`(file_type,status)`、`(mtime_ns)`、部分索引 `(content_hash) WHERE NOT NULL`（P2）、`(is_deleted,status)`

**exif（1:1）**：`file_id`(PK/FK CASCADE), `datetime_original`, `datetime_digitized`, `gps_lat/gps_lon/gps_altitude`, `camera_make/model`, `width/height`, `orientation`, `iso/exposure_time/f_number`
索引：`(datetime_original)`、`(gps_lat,gps_lon)`

**chunks（1:N，FTS5 + Embedding 的统一入口；source_type 枚举与 Qdrant payload 一致）**：

```sql
CREATE TABLE chunks (
    id               INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source_type      TEXT NOT NULL,   -- 'doc_chunk' | 'ocr' | 'image_caption'
    chunk_index      INTEGER NOT NULL DEFAULT 0,   -- doc_chunk: 0..N；ocr/image_caption: 恒 0
    chunk_text       TEXT NOT NULL,        -- 原文（image_caption 为描述文本）
    chunk_text_seg   TEXT NOT NULL,        -- jieba 分词文本（FTS 用）
    token_count      INTEGER,
    embedding_status INTEGER NOT NULL DEFAULT 0,  -- 0=PENDING 1=SUCCESS 2=FAILED
    created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at       INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(file_id, source_type, chunk_index)
);
CREATE INDEX idx_chunks_file ON chunks(file_id, source_type, chunk_index);
CREATE INDEX idx_chunks_emb  ON chunks(embedding_status, id);
```

**source_type 创建后不可变**：变更 source_type 必须 DELETE + INSERT（FTS 触发器随之 delete+insert 同步）。

**ocr_text（1:1 原始 OCR 存档）**：`file_id`(PK/FK), `text`, `lang`, `confidence`, `created_at`
职责：保存**原始 OCR 结果**，用于详情展示、调试、重新处理；与 chunks(ocr)（标准化搜索输入）职责不同，非重复业务数据。

**search_history**：`id`, `query_text`, `parsed_filters`(JSON), `result_count`, `top_results`(JSON), `latency_ms`, `created_at`

**ai_tasks（单机轻量持久化任务队列——一个文件一个任务）**：

```sql
CREATE TABLE ai_tasks (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    task_type     TEXT NOT NULL DEFAULT 'index_file',  -- MVP 只有这一种任务类型
    priority      INTEGER NOT NULL DEFAULT 0,   -- 0 高(监听触发) 1 低(批量扫描)
    status        TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|RUNNING|SUCCESS|FAILED
    attempt       INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    next_attempt_at INTEGER,          -- P2 自动重试；MVP 恒 NULL
    last_error    TEXT,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at    INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX idx_tasks_status ON ai_tasks(status, priority);
CREATE INDEX idx_tasks_file  ON ai_tasks(file_id);
-- 数据库级保证：同一 file_id 最多存在一个活跃任务（防重复入队）
CREATE UNIQUE INDEX idx_tasks_active ON ai_tasks(file_id) WHERE status IN ('PENDING','RUNNING');
```

**不做任务 DAG**：一个文件由**一个 `index_file` 任务**负责完整处理流程，Worker 内部按 file_type 分支（image / document 流水线）。**去重入队**：partial unique index 保证同一 file_id 最多存在一个 PENDING/RUNNING 任务，入队冲突即跳过。

**retry 与 reindex 区分（明确）**：
- `retry`：复用 FAILED 任务，`FAILED → PENDING`（attempt 累计，不新建记录）；**attempt ≥ max_attempts 时 retry API 拒绝，返回 `MAX_ATTEMPTS_EXCEEDED`**（此时只能通过 reindex 新建任务）
- `reindex`：创建**新的** index_file task（旧任务保留在历史中）
- PENDING/RUNNING 期间不得重复创建任务（由 partial unique index 兜底）
- SUCCESS/FAILED 历史任务保留（供统计与审计）

P2 再考虑自动重试、任务取消、任务恢复。

**index_jobs（扫描作业+进度）**：`id`, `root_path`, `scan_type`(full|incremental), `status`, `cursor_path`(**P2** 断点), `total_files/scanned_files/error_count`, `started_at/finished_at`

**settings（KV）**：`key`(PK), `value`(JSON)

**状态职责区分（明确）**：`files.status` = **文件级** AI pipeline 状态（METADATA_ONLY/PROCESSING/AI_DONE/FAILED）；`chunks.embedding_status` = **单个 chunk** 的向量索引状态（0=PENDING 1=SUCCESS 2=FAILED）。**embedding 失败不影响该 chunk 的 FTS 可搜索能力**——FTS 触发器在 chunks INSERT 时即生效，与 embedding_status 无关。

## 7.2 表关系

```
files 1—1 exif | files 1—N chunks | files 1—1 ocr_text
files 1—N ai_tasks | index_jobs/settings/search_history 独立
```

**时间存储与时区规则（冻结）**：① SQLite 内部统一存 **UTC unixepoch seconds**（整数）；② 用户输入的今天/昨天/日期区间基于 **Windows OS 当前时区**解释；③ EXIF datetime_original 通常无时区信息，MVP 解释为「拍摄设备本地时间」，按本地时区换算为 epoch；④ API 内部比较用 epoch，API/UI 展示用本地时间，`time_basis/confidence` 必须反映来源；⑤ 日期范围解析统一归 **QueryParser + TimeRangeService**，**禁止 server/worker/frontend 各自实现日期计算逻辑**。

---

# 8. FTS5 全文搜索设计【MVP】

## 8.1 两张 FTS 表

| 表 | 模式 | 列 | 关联 |
|---|---|---|---|
| `fts_files` | **contentless-delete**（首选）或 **contentless + special delete**（兜底，见冻结规则） | `filename, filename_seg, dir_tokens` | rowid = files.id |
| `fts_body` | **external content**（指向过滤 VIEW） | `chunk_text, chunk_text_seg` | rowid = chunks.id |

```sql
-- 首选：contentless-delete 表（SQLite ≥ 3.43；支持普通 UPDATE/DELETE 与 integrity-check）
CREATE VIRTUAL TABLE fts_files USING fts5(
    filename, filename_seg, dir_tokens,
    content='', contentless_delete=1,
    tokenize='unicode61');

-- 兜底：普通 contentless 表（SQLite < 3.43；官方文档明确不支持普通 UPDATE/DELETE，
--       必须使用 FTS5 special delete command，见 §8.2）
CREATE VIRTUAL TABLE fts_files USING fts5(
    filename, filename_seg, dir_tokens, contentless, tokenize='unicode61');

-- FTS 正文数据源：VIEW 过滤 source_type，保证 rebuild 也只包含 doc_chunk + ocr
CREATE VIEW fts_chunks_source AS
    SELECT id, chunk_text, chunk_text_seg
    FROM chunks
    WHERE source_type IN ('doc_chunk', 'ocr');

CREATE VIRTUAL TABLE fts_body USING fts5(
    chunk_text, chunk_text_seg,
    content='fts_chunks_source', content_rowid='id',
    tokenize='unicode61');
-- 触发器（仅 doc_chunk / ocr 进入关键词通道；image_caption 仅走语义通道）
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks WHEN NEW.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(rowid, chunk_text, chunk_text_seg)
    VALUES (NEW.id, NEW.chunk_text, NEW.chunk_text_seg); END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks WHEN OLD.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(fts_body, rowid, chunk_text, chunk_text_seg)
    VALUES ('delete', OLD.id, OLD.chunk_text, OLD.chunk_text_seg); END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks WHEN NEW.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(fts_body, rowid, chunk_text, chunk_text_seg)
    VALUES ('delete', OLD.id, OLD.chunk_text, OLD.chunk_text_seg);
    INSERT INTO fts_body(rowid, chunk_text, chunk_text_seg)
    VALUES (NEW.id, NEW.chunk_text, NEW.chunk_text_seg); END;
-- 全量重建: INSERT INTO fts_body(fts_body) VALUES('rebuild')
```

**通道保证（明确）**：doc_chunk → FTS ✅；ocr → FTS ✅；image_caption → FTS ❌（仅 Vector ✅）。**FTS rebuild 也只能包含 doc_chunk + ocr**——因为 content 指向过滤后的 `fts_chunks_source` VIEW（而非 chunks 全表），rebuild 不会把 image_caption 索引进关键词通道。

**模式冻结（ADR-005）**：contentless-delete（SQLite ≥ 3.43）优先；否则普通 contentless + FTS5 special delete command。以运行时 `sqlite3.sqlite_version` 检测决定，**选择写入 ADR，实现阶段不得自行改变**。

## 8.2 同步策略与 FtsRepository 封装

| 变化 | 同步方式 |
|---|---|
| files INSERT（新文件） | `FtsRepository.insert`：`INSERT INTO fts_files(rowid, ...)` |
| files UPDATE（**rename/重新扫描**） | `FtsRepository.replace`：contentless-delete 表用普通 UPDATE；普通 contentless 用 **FTS5 special delete command**（`INSERT INTO fts_files(fts_files, rowid, ...) VALUES('delete', ?, ...)`）+ INSERT |
| files DELETE（文件删除） | `FtsRepository.delete`：contentless-delete 表用普通 DELETE；普通 contentless 用 special delete command |
| chunks INSERT / UPDATE / DELETE | 触发器 → FTS INSERT / delete+insert / DELETE（WHEN source_type ∈ doc_chunk,ocr） |

**FtsRepository 统一封装（强制）**：`insert / delete / replace(update) / integrity_check('integrity-check')` 四个方法；**业务层禁止直接操作 fts_files**（FTS 读写只经 FtsRepository）。

**明确保证**：文件 rename（改 filename/dir_tokens/filename_seg）、文档重新解析（chunks 重建）、OCR 重新识别（ocr chunk 重建）都必须正确同步 FTS。

**集成测试（12 项）**：新文件 INSERT / 文件名修改 / 路径修改 / rename / 文件删除 / 同一 rowid 重建 / 重启数据库后搜索 / FTS integrity-check / **chunks 存在 image_caption 时执行 FTS rebuild（确认 rebuild 后 image_caption 仍不进 FTS）** / **搜索 image_caption 独有关键词（确认 FTS 不返回 image_caption）** / **搜索 doc_chunk 关键词（正常返回）** / **搜索 ocr 关键词（正常返回）**。

## 8.3 中文分词与查询

**jieba 预分词 + unicode61**：写入时 jieba 分词以空格拼接存 `*_seg` 列。**MVP 不引入 trigram 独立索引**；如后续 benchmark 证明中文子串召回不足，再作为 P2 单独增加（届时需定义独立 FTS 表、索引策略、查询切换条件与空间成本）。

**查询策略（phrase → AND → OR 逐级降级）**：优先短语查询（`"词1 词2"`）；其次 AND（`词1 AND 词2`）；**AND 召回不足时自动降级 OR**（召回数低于阈值或含单字词）。最终仍由 RRF 与 Vector 通道融合，不因关键词召回少而整体失败。

查询流程：`match + bm25() ORDER BY LIMIT 200` → join files（应用 UnifiedFilter WHERE）→ 得分归一化进入融合器。路径按分隔符拆目录片段入 `dir_tokens`；文件名支持 `resume*` 前缀查询。

---

# 9. Qdrant 向量搜索设计【MVP】

## 9.1 Collection 配置（单 collection："omnisearch"）

| 配置 | 值 | 说明 |
|---|---|---|
| 维度 | 512（BGE-small-zh） | 由当前模型决定；换模型触发全量重建（P2） |
| distance | Cosine | 语义标准度量 |
| HNSW | m=16, ef_construct=128, 查询 ef=64 | **初始参数，最终以本机 benchmark 调优为准** |
| 量化 | int8 scalar（可选） | 百万点规模时启用，benchmark 验证召回损失 |
| payload 索引 | file_id, source_type | 支持 delete-by-filter |

**单 collection**：image_caption / doc_chunk / ocr 同属 BGE 文本语义空间，用 `source_type` payload 区分，不拆 collection。

## 9.2 Point 设计（修复 ID 冲突）

```
logical_key = f"{file_id}:{source_type}:{chunk_index}"
  例: file_id=100 → 100:image_caption:0 / 100:ocr:0 / 100:doc_chunk:0
point_id = xxh3_64(logical_key)   # hash 算法冻结（写入 ADR）；算法变更必须触发 Qdrant 全量重建
```

`{file_id, source_type, chunk_index}` 三元组与 chunks 表 UNIQUE 约束一一对应，幂等 upsert 覆盖。

**payload（MVP 最小集）**：`file_id`, `chunk_index`, `source_type`, `text`（返回时免回表）。`embed_model` 字段为 **P2** 模型版本追踪预留。**path/filename/mtime/file_type 等 metadata 的事实来源是 SQLite**；若未来为性能在 payload 冗余 mtime/type 字段，仅作**性能优化冗余**，不作为事实来源，过滤正确性仍以 SQLite 为准。

## 9.3 一致性（修正表述）

- **SQLite 是事实数据源；Qdrant 是可重建向量索引**（数据 = chunks.embedding_status=SUCCESS 的行 + BGE 重算，任何时刻可全量重建）
- Qdrant 使用**同步 upsert**（`wait=true`，批量提交）：**upsert 成功 → embedding_status=SUCCESS；失败 → FAILED**——`wait=true` 保证状态语义一致，不引入额外状态；批量 upsert 的异步化优化留待 P2 benchmark 验证收益后再定
- `embedding_status`（0=PENDING 1=SUCCESS 2=FAILED）+ 对账任务（**P2** 每日：scroll 全量 point 的 file_id 与 chunks 对比，修复孤儿点/缺失点）用于发现和修复不一致
- 删除/重索引遗留的旧 point 不污染搜索结果：向量候选必须经 **chunks 三元组校验（file_id, source_type, chunk_index）+ files canonical WHERE**（见 §12.3），不能只回查 files.file_id；异步清理仅为容量回收

---

# 10. AI Worker 设计【MVP，标注 P2】

## 10.1 进程模型：独立 Python 进程

选**独立进程**：① GIL/延迟隔离——ONNX 推理、OCR、文档解析是 CPU/内存密集操作，混在 FastAPI 内拉高 API 延迟；② 内存与故障隔离（模型常驻数百 MB，崩溃只重启 Worker）；③ 可独立降级（CPU 紧张时降低 Worker 优先级，索引停但搜索不停）。

## 10.2 任务队列：共享 SQLite 轻量持久化队列

**适用边界（诚实声明）**：ai_tasks 是「**单机、单 Worker、桌面应用场景下的持久化轻量任务队列**」，**不能替代通用消息队列**（无多消费者竞争协议、无流式/广播语义）。本项目恰好只需：单生产者（FastAPI/索引管道）+ 单消费者（单 Worker 进程）+ 持久化 + 状态可查询，SQLite 全部满足且零额外依赖。

**核心设计**：`SQLite persistent queue + single worker`；轮询间隔为**可调参数** `poll_interval_ms = 500`（默认值，非架构约束）。

**并发与写入竞争（风险与对策）**：

| 风险点 | 对策 |
|---|---|
| FastAPI（生产任务）与 Worker（状态回写）并发写 | WAL：写写串行；`busy_timeout=5000` 等待而非报错 |
| 长事务持锁阻塞对端 | 所有事务**短事务**：claim 一次 8 条、元数据 1000 行/批、状态逐任务回写；**推理/OCR/Embedding 一律在事务外执行** |
| claim 竞争 | 单 Worker 无消费者竞争；claim 事务边界：`BEGIN IMMEDIATE → SELECT ... WHERE status='PENDING' LIMIT 8 → UPDATE ... SET status='RUNNING', attempt=attempt+1 → COMMIT`（原子且短）；同一事务内将对应 files.status 置 PROCESSING |
| 进程崩溃 | 中断的 RUNNING 任务：MVP 由 UI 手动重试兜底；**P2** Worker 启动时 `UPDATE ... SET status='PENDING' WHERE status='RUNNING'` 自动恢复 |

**通信方式**：Worker 轮询 claim（本地磁盘 <1ms，开销可忽略）。不用 HTTP 回调（进程死亡丢回调、环形依赖）。SQLite 既是队列、也是状态存储、也是 UI 数据源。

## 10.3 任务模型（MVP，无 DAG）

```
task_type = 'index_file'   —— 一个文件一个任务，负责完整处理流程
Worker 内部按 file_type 分支：
  image:    decode → Caption → OCR → chunks/FTS → Embedding → Qdrant
  document: extract → chunk → FTS → Embedding → Qdrant

状态机（MVP 仅 4 态）：
PENDING ──claim──► RUNNING ──成功──► SUCCESS（files.status=AI_DONE）
                      │失败
                      ▼
                    FAILED（files.status=FAILED；UI 可见，可手动重试置回 PENDING）
```

**FAILED 语义（明确）**：task FAILED 表示 `index_file` **未完整完成，不代表文件完全不可搜索**——已成功生成的 OCR/chunks/FTS/embedding 结果默认保留；搜索按该文件现有能力降级执行（如已 OCR 未 embedding 的文件仅关键词通道可命中）；请求级通道失败由响应 `degraded_channels` 反映（见 §12.8）。

**旧数据保护（与 §11.5、§12.8 一致）**：index_file 执行过程中**不得先删除旧 chunks**；只有新解析结果完整成功后（短事务提交）才替换旧数据；任一阶段失败 → 旧 chunks/FTS/Qdrant points 全部保留、task=FAILED。核心目标：**「重新索引失败时，旧搜索能力必须保持可用。」**

保留字段：`attempt / last_error / created_at / updated_at`；`next_attempt_at` 预留（P2 自动重试）。无 DEAD/CANCELLED/worker_id/复杂退避。**P2**：自动重试（退避）、任务取消（文件删除时置 FAILED 并标注原因）、启动恢复。

## 10.4 三条处理流水线（MVP）

- **image**：decode → Caption 模型生成中文描述 → chunks(source_type=image_caption, chunk_index=0)；**并行** PaddleOCR(zh+en) → ocr_text（原始存档）+ chunks(source_type=ocr)；FTS 同步（触发器，仅 ocr 进入关键词通道）→ 全部 chunks BGE embedding → Qdrant upsert
- **document（TXT/MD/PDF/DOCX）**：内置/PyMuPDF/python-docx 提取 → 切分（256 token、重叠 32、按段落边界回退，表格/代码块不切）→ chunks(source_type=doc_chunk) → FTS（触发器）→ embedding → Qdrant
- **OCR 独立触发**：图片 → PaddleOCR → 更新 ocr_text + 重建 ocr chunk（触发器 FTS delete+insert）→ 重新 embedding

**入队范围**：仅 file_type ∈ {image, doc} 入队 index_file；video/audio/archive/other 保持 METADATA_ONLY（UI 视为「无需 AI 处理」）。

## 10.5 图片语义链路（统一文本语义空间）

```
MVP 唯一链路：
图片 → LocalImageCaptionProvider 生成 Caption（中文文本）
     → BGE-small-zh Embedding（与文档/Query 同一模型）
     → Qdrant（source_type=image_caption）

Query（中文）→ BGE-small-zh Embedding → Qdrant
```

**语言约束（明确）**：MVP 默认 **中文 Query → 中文 Caption → BGE-small-zh**。**不承诺** BGE-small-zh 自动可靠处理中英跨语言 Caption；英文 Caption 或多语言 embedding（如 BGE-M3）的匹配质量放到后续 benchmark/扩展验证。

**CLIP 明确后置**：MVP 不引入 CLIP image vector；「以图搜图」Phase 3 单独引入图像向量并独立 collection，不与 BGE 文本空间混用。

## 10.6 Caption 模型选型（M4 前冻结）

**候选模型（ONNX Runtime 推理）**：

| 候选 | 中文 caption 能力 | 大小（约） | CPU 推理 | 备注 |
|---|---|---|---|---|
| Florence-2-base-ft（ONNX 官方导出） | 弱（英文为主） | ~250MB(int8) | ~2-5s/图 | 生态最成熟、导出完善 |
| Qwen2-VL-2B-Instruct（量化） | **强** | ~2GB(int4) | ~3-8s/图 | 中文质量最好，体积大 |
| InternVL2-1B | 中 | ~1GB(int8) | ~3-6s/图 | 折中选择 |

**当前不伪装成已确定**：**M4 开始前完成 Caption 模型 benchmark（中文描述质量 / 单图推理耗时 / 内存占用）后冻结选型**，结果写入 `docs/adr/`。

**MVP fallback（明确命名与边界）**：若中文 caption 质量均不达标，MVP fallback = **视觉标签文本生成（Zero-shot Visual Tagging）**：

```
Image → Zero-shot Visual Tagging → 中文标签文本 → BGE embedding → Qdrant
```

明确：**只生成文本标签；不保存 CLIP image embedding；不建立 image-vector collection；不新增图像向量搜索通道；不改变 Hybrid Search 的 FTS + Vector 双通道结构**。Phase 3 的以图搜图（Image → CLIP image embedding → 独立 image-vector collection → Query by Example）与 MVP 边界完全分离。

## 10.7 Provider 抽象（MVP 极简）

```python
class ImageCaptionProvider(Protocol):
    def caption(self, image_path: Path) -> CaptionResult   # {text, model, confidence}

class EmbeddingProvider(Protocol):
    @property
    def dim(self) -> int
    def embed_texts(self, texts: list[str]) -> list[list[float]]
```

MVP 仅两个实现：`LocalImageCaptionProvider`（ONNX，M4 前冻结选型）、`BGEEmbeddingProvider`（ONNX Runtime）。不做 Provider 注册表/路由框架；云端 Provider 为 P3 且同时只启用一个。

## 10.8 模型管理

`models/manifest.json` 登记 `{id, name, source(HF 镜像), sha256, size_mb, format}`；Onboarding 触发下载（`.part` 断点续传 + 校验）MVP；版本升级检测与引导重建 **P2**。体积预估（下载前展示）：BGE-small ~100MB、PaddleOCR ~30MB、Caption 模型见选型表。

---

# 11. 文件索引 Pipeline【MVP，P2 标注】

## 11.1 初次全量扫描（MVP）

用户添加目录 → `index_jobs(full)` → 有序 DFS（显式栈）→ 8 线程生产者-消费者、有界队列 10k → 元数据提取（stat 不跟随符号链接，mtime_ns/ctime_ns/size）→ 过滤系统目录/黑名单扩展名 → `executemany` 每 1000 行一事务 → 每 500 文件更新进度 → 扫完按 file_type ∈ {image,doc} 批量入队 index_file（priority=1）。

## 11.2 增量扫描与变化检测（MVP）

- **mtime_ns+size 为主**：与库中记录比对 → 不同则更新元数据 + status 回退 METADATA_ONLY + 入队新 index_file
- **hash 复用 AI 结果（P2）**：xxh3 指纹相同（移动/复制）→ 克隆既有 AI 产物跳过推理
- **断点续扫（P2）**：`cursor_path` 续扫

## 11.3 删除同步（顺序明确）

```
发现删除（Watchdog 事件 / 扫描比对）
  → ① SQLite files.is_deleted = 1（立即，短事务）
  → ② 立即从正常搜索结果排除：所有搜索 SQL 的 canonical WHERE 恒含 is_deleted=0；
      向量通道候选回 SQLite join 时同样被过滤 —— 不依赖 Qdrant 清理完成
  → ③ 异步清理：chunks（触发器级联 FTS）/ fts_files 行 / Qdrant 点（delete-by-filter file_id）
      / ai_tasks（级联）；批处理每批 5000，低优先级
```

**同路径文件重新出现（复活规则）**：写入 files 时若 `path` 已存在且 `is_deleted=1` → **复活原记录**（is_deleted=0 + 元数据刷新 + status=METADATA_ONLY + 入队新 index_file），**不 INSERT 新 file_id**——解决 `path` UNIQUE 约束与软删除并存的冲突。

## 11.4 文件监听（双通道）

| 通道 | 阶段 | 技术 |
|---|---|---|
| 实时监听 | **MVP** | Watchdog（ReadDirectoryChangesW），运行期间增/改/删/重命名 |
| USN Journal 兜底 | **P2** | NTFS USN Journal 光标增量枚举，恢复应用关闭期间的变更 |

**USN 定位**：Windows/NTFS **增强能力**，不是核心搜索功能的前置依赖；MVP 用 Watchdog + 启动时增量扫描（mtime+size 对比）已正确工作，USN 只是把启动恢复从分钟级重扫加速到秒级。非 NTFS 卷降级为对比重扫。

**事件处理（MVP）**：环形缓冲 10k + 防抖 2s → 合并器（CREATE+MODIFY 合并、CREATE+DELETE 忽略）→ 批量调度增量处理；缓冲溢出/句柄失效 → 标记子树重扫。

**RENAME 语义（文件身份，明确）**：Watchdog 捕获的**运行期间 RENAME**：
- **目标 path 不存在**：正常 rename——保留 file_id、chunks、OCR、Caption、Embedding 与 Qdrant point，仅更新 path/filename/dir_path（filename/dir_tokens 变化时 fts_files 按 rowid delete+insert，rowid=file_id 不变）
- **目标 path 已存在（rename conflict）**：**不自动覆盖、不直接删除目标记录**——标记 conflict，对 source + target 两个路径**重新扫描**比对；仅当确认目标原文件已不存在（如目标记录已软删/扫描证实缺失）时，才允许源 file_id 接管该 path
- **应用关闭期间发生的移动，MVP 不保证识别为同一文件**（启动扫描按「删除+新增」处理）；**P2** 引入 content_hash 后实现跨路径移动/复制的 AI 结果复用

---

## 11.5 重新索引一致性规则（MVP）

**旧 chunks 生命周期（明确）**：
1. index_file 任务执行过程中，**不得先删除旧 chunks**
2. 新解析结果必须先在**内存/临时结构**中完成（extract/chunk/OCR/Caption/Embedding 全部在 SQLite 事务外执行）
3. 只有新解析结果**完整成功**后，才在**一个短 SQLite 事务**内原子提交：DELETE old chunks + INSERT new chunks + OCR/metadata 对应更新 + FTS 触发器同步
4. 解析/OCR/chunking 任一阶段失败：SQLite 旧 chunks、FTS、Qdrant 旧 points **全部保留**，task = FAILED
5. **核心目标：「重新索引失败时，旧搜索能力必须保持可用。」**（§10.3、§12.8 与此一致）

**Qdrant 顺序**：old points **不得提前删除**；先 upsert new_points（point_id 幂等覆盖）；成功后计算 `old_points`（按 file_id scroll 获取）与 `stale = old_points − new_points`（logical_key 差集）→ 异步批量 delete；**stale 清理失败仅记录（P2 对账任务兜底），不影响 new_points 生效**；upsert 失败则旧 points 保留。

---

# 12. Hybrid Search 算法设计【MVP 核心】

## 12.1 通道定义（最终语义）

| 通道 | 角色 | 内容 | 是否参与 RRF |
|---|---|---|---|
| Metadata | **Filter**（非检索通道） | time / file_type / extension / is_deleted | ❌ |
| FTS5 | Keyword Retrieval | filename（fts_files）+ 文档正文 + OCR（fts_body） | ✅ |
| Qdrant | Semantic Retrieval | image_caption / doc_chunk / ocr | ✅ |

**RRF = FTS + Vector**。Metadata 是候选过滤条件，不单独参与融合。

## 12.2 Query Parser → 统一 Filter Model

Parser 输出统一结构，SQLite 与 Qdrant **通过各自的 Filter Builder 消费同一个结构**：

```json
{
  "time_range": {"from": "2026-08-14T00:00:00+08:00", "to": "2026-08-14T23:59:59+08:00", "basis_hint": "exif_first"},
  "file_types": ["image"],
  "extensions": [],
  "include_deleted": false
}
```

（`+08:00` 仅为示例，实际使用 Windows 当前配置时区偏移。**UnifiedFilter.time_range 禁止 timezone-naive datetime**——统一 offset-aware RFC3339。转换链路：QueryParser → TimeRangeService → offset-aware RFC3339 → UTC unixepoch → SQLite canonical WHERE；禁止 server/worker/frontend 各自解释时间。）

- **规则解析（MVP 边界）只负责**：① 时间表达（今天/昨天/前天/本周/上周/本月/具体日期/日期区间）；② 文件类型（image/document）；③ 扩展名（pdf/docx/jpg/png 等）；④ 简单关键词提取；⑤ 剩余无法结构化的文本进入 semantic_text
- **MVP 明确禁止**：通用意图识别、复杂实体抽取、多轮语义理解、通用 LLM Agent、自动修改搜索意图、自由 JSON schema 扩展
- **解析失败兜底**：`semantic_text = 原始 query`，**Parser 失败不得导致整个搜索失败**
- **LLM Query Parser 为 P2**：Settings 配置本地 LLM 后才启用，输出固定 JSON schema，校验失败静默回退规则结果；MVP 仅规则 Parser，核心搜索完全离线可用
- **过滤正确性事实来源是 SQLite**：`FilterBuilderService` 生成唯一 canonical WHERE（`is_deleted=0 AND file_type IN (...) AND extension IN (...) AND 时间条件`），应用于 files 查询、FTS join、向量候选回表 join 三处。**Qdrant payload filter 仅作性能优化**（P2，冗余字段时启用），**不得出现两套不同过滤语义**

## 12.3 双通道并行执行（+ 降级）

```
FTS5 Retrieval:   fts_files + fts_body match + bm25 → top 200（join files 应用 canonical WHERE）
Qdrant Retrieval: BGE embed(semantic_text) → top 200 points（chunk 级）
                  → 回 SQLite 校验三元组：point 的 (file_id, source_type, chunk_index)
                    必须在 chunks 表中存在（防旧 point 污染召回）
                  → join files 应用 canonical WHERE（删除/类型过滤兜底）
                  → 按 file_id 去重（同文件多 chunk 取 max cosine，保留命中 chunk 证据）
超时：语义 3s / FTS 1s；慢通道降级不阻塞整体（响应 degraded_channels 标注）
```

## 12.4 排序融合：加权 RRF（仅 FTS + Vector）

```
rrf_score(d) = w_kw / (k + rank_kw(d)) + w_sem / (k + rank_sem(d))
k=60（初始值，benchmark 调优）；默认 w_kw = w_sem = 1.0（Settings 可调）
```

## 12.5 Score 语义（API 无歧义）

```json
{
  "rrf_score": 0.0371,           // 融合得分（仅 FTS + Vector）
  "keyword_score": 18.4,         // BM25 原始分（未命中为 null）
  "semantic_score": 0.81,        // cosine（未命中为 null）
  "match_reasons": [...]
}
```

## 12.6 匹配原因填充

每条结果带 `match_reasons[]`（前端按通道着色）：元数据「拍摄于 2026-08-14（EXIF，可信度 exact）」；文件名「包含 'resume'」；正文「第 3 段提到...（高亮片段）」；语义「AI 描述: 自由女神像夜景照片（相似度 0.81）」；OCR「识别到文字: New York 2026」。

## 12.7 时间过滤可信度（最终语义）

| time_basis | time_confidence | 过滤行为 |
|---|---|---|
| `exif` | `exact` | **hard filter**：EXIF datetime_original 在范围内 → 保留；不在范围 → 排除 |
| `mtime` / `ctime` | `fallback` | 无 EXIF 时以 mtime 参与过滤（在范围内保留、**结果必须标记 fallback**）；与 exact 不等价，时间证据在排序中降权（soft） |
| — | `unknown` | 没有任何可用时间 → **默认排除**；「Include unknown time」为 **P2 可选设置**，不属于 MVP |

时间字段选择：动词含「拍/摄/照」→ EXIF 优先；「创建/保存」→ ctime；「修改」→ mtime；默认 EXIF → mtime → ctime 逐级回退（回退即 fallback）。**接口对应**：/search 响应每条结果含 `time_info: {basis, confidence, value}`。

**单一语义（强制）**：时间过滤是 canonical WHERE 的一部分——SQLite / FTS / Qdrant 三处必须使用**同一套时间语义**（TimeRangeService 生成区间 → FilterBuilder 落 SQL），不允许各通道自行实现时间过滤。Match Reason 展示：`basis = exif / mtime / ctime / unknown`，`confidence = exact / fallback / unknown`。只有如此「Metadata Filter 是 correctness source」才真正成立。

## 12.8 Search Degradation Matrix（通道异常降级）

| 异常场景 | 降级行为 | degraded_channels |
|---|---|---|
| Qdrant 不可用/超时 | 跳过语义通道；结果 = Metadata Filter + FTS5 | `["semantic"]` |
| FTS5 异常（索引损坏等） | 跳过关键词通道；结果 = Metadata Filter + Vector | `["keyword"]` |
| BGE embedding 失败 | 跳过语义通道 | `["semantic"]` |
| SQLite 不可用 | 事实数据源不可用 → 搜索整体不可用，返回明确错误（不静默返回空结果） | 整体失败 |
| semantic_text 为空 | 正常跳过向量通道（非降级） | `[]` |
| 单文件 AI 任务 FAILED（部分索引） | 该文件仅在其已有数据的通道可命中（如已 OCR 未 embedding → 仅关键词通道），match_reasons 如实反映；**重新索引失败时旧索引数据保留，旧搜索能力不受影响**（§11.5） | `[]`（文件级，非请求级） |

**原则**：FTS 与 Vector 任一通道可用即返回结果；两通道均失败但 SQLite 可用时返回明确错误。

---

# 13. API 设计【MVP】

Base: `http://127.0.0.1:{port}/api/v1`；认证：Main 启动生成随机 token，`X-Omni-Token` 头校验。

| Method | Path | 说明 | 阶段 |
|---|---|---|---|
| GET | `/health` | 存活+就绪探针 | MVP |
| GET | `/stats` | 索引统计/队列/库大小 | MVP |
| POST | `/index/scan` | 创建扫描作业（full/incremental） | MVP |
| GET | `/index/status` · POST `/index/stop` | 进度 / 停止 | MVP |
| POST | `/search` | 核心混合搜索 | MVP |
| GET/DELETE | `/search/history` | 历史查询/清空 | MVP |
| GET | `/files/{file_id}` | 详情（EXIF/chunks/AI 描述/OCR 原始） | MVP |
| POST | `/files/{file_id}/reindex` | 单独重建（入队新 index_file） | MVP |
| GET | `/files/{file_id}/thumbnail` | 缩略图（缓存 P2） | MVP |
| GET | `/task/status` · GET `/task/queue` | AI 队列汇总/明细 | MVP |
| POST | `/task/{id}/retry` | 失败任务手动重试（attempt ≥ max_attempts 返回 `MAX_ATTEMPTS_EXCEEDED`） | MVP |
| GET/PUT | `/settings` | 设置 | MVP |
| POST | `/settings/test-connection` | Provider 测试 | P3 |
| POST | `/database/optimize` | VACUUM/FTS rebuild/对账 | P2 |
| POST | `/shutdown` | 优雅退出入口 | P2 |

**POST /search 响应示例**（字段与 §7/§12 一一对应）：

```json
{
  "parsed": {
    "filters": {
      "time_range": {"from": "2026-08-14T00:00:00+08:00", "to": "2026-08-14T23:59:59+08:00", "basis_hint": "exif_first"},
      "file_types": ["image"], "extensions": [], "include_deleted": false
    },
    "semantic_text": "自由女神",
    "parse_method": "rule"
  },
  "results": [{
    "file_id": 88421, "path": "D:\\Photos\\2026\\IMG_08421.jpg",
    "filename": "IMG_08421.jpg", "file_type": "image",
    "rrf_score": 0.0371,
    "keyword_score": null,
    "semantic_score": 0.81,
    "time_info": {"basis": "exif", "confidence": "exact", "value": "2026-08-14T19:23:11+08:00"},
    "match_reasons": [
      {"channel": "semantic", "text": "AI 描述: 一张自由女神像照片，背景为纽约天际线", "score": 0.81},
      {"channel": "metadata", "text": "拍摄于 2026-08-14（昨天），字段: EXIF 拍摄时间"}
    ]
  }],
  "total": 23, "latency_ms": 148,
  "degraded_channels": []
}
```

---

# 14. 项目目录结构【MVP 骨架，P2 标注】

```
OmniSearch/
├── desktop/                          # Electron + Vue3
│   ├── src/main/                     # ProcessManager.ts / HealthMonitor.ts / FileAccess.ts / ipc/
│   ├── src/preload/                  # index.ts + index.d.ts
│   ├── src/renderer/                 # views/ components/ stores/ api/ipc.ts
│   ├── src/shared/contracts.ts       # IPC+API 契约单一事实源
│   ├── electron-builder.yml          # P2 打包
│   └── vite.config.ts
├── python/                           # 一个 Python 包、两个进程入口
│   ├── pyproject.toml
│   ├── omnisearch/
│   │   ├── common/                   # ============ 唯一共享层 ============
│   │   │   ├── database.py           # SQLite 连接门面（FastAPI 与 Worker 共用，见下注）
│   │   │   ├── models/               # 领域对象（File/Chunk/Task 等）
│   │   │   ├── config/               # 配置、路径、poll_interval_ms 等参数
│   │   │   ├── contracts/            # Pydantic DTO（与 TS contracts 对齐）
│   │   │   └── utils/                # 文件类型判定、jieba 分词、hash、端口成对分配
│   │   ├── server/                   # ============ FastAPI 进程 ============
│   │   │   ├── main.py
│   │   │   ├── api/                  # search.py index.py files.py tasks.py settings.py stats.py schemas.py
│   │   │   ├── service/              # query_parser.py time_range.py filter_builder.py search.py index.py ai_task.py settings.py stats.py
│   │   │   ├── repository/           # files.py chunks.py fts.py vector.py tasks.py history.py
│   │   │   └── database/             # 仅 migrations/（版本化迁移；连接层在 common/database.py）
│   │   └── worker/                   # ============ AI Worker 进程 ============
│   │       ├── main.py               # 轮询循环、心跳
│   │       ├── pipeline/             # image.py doc.py ocr.py chunker.py
│   │       ├── providers/            # base.py local_caption.py bge_embedding.py
│   │       ├── task/                 # queue.py（claim/回写，短事务）
│   │       └── embedding.py          # BGE 加载、批处理（batch_size 初始 32）
│   ├── scripts/                      # dev.py（一键拉起 FastAPI/Worker/Qdrant 三子进程）/ download_models.py
│   └── tests/                        # pytest：单元 + 集成测试矩阵（见 §14.1）
├── qdrant/                           # bin/qdrant.exe + production.yaml
├── models/manifest.json
├── resources/                        # 图标/安装器资源
├── scripts/                          # 构建脚本（P2）
├── docs/                             # architecture.md + adr/ + api.md
├── dev-data/                         # 开发期数据目录（gitignore）
└── README.md
```

**依赖规则（强制）**：共享代码只能放 `common/`（`server → common` ✅、`worker → common` ✅）；**禁止** `worker → import server.repository`、`server → import worker.pipeline`。

**Database 层归属（明确）**：FastAPI 与 AI Worker 都需要 SQLite 连接门面（architecture.md §2.1「各自连接」），因此 **connection facade 位于 `common/database.py`**（唯一共享层，两进程共用）；`server/database/` 仅保留 **migrations**（版本化迁移只由 FastAPI Server 启动时执行，Worker 不执行迁移）。

## 14.1 一致性测试矩阵（集成测试）

| 域 | 用例 |
|---|---|
| A 文件生命周期 | CREATE / MODIFY / RENAME / DELETE / RECREATE |
| B FTS | filename insert / update / delete；document chunk replace；OCR replace；同一 rowid 重建；重启数据库后搜索；FTS integrity-check；**含 image_caption 的 FTS rebuild 后 image_caption 不进 FTS、doc/ocr 正常** |
| C Qdrant | first index / reindex success / reindex failed / stale point cleanup / orphan point / missing point |
| D Hybrid | FTS only / Vector only / FTS+Vector / semantic timeout / FTS timeout / SQLite failure |
| E Filter | time exact / time fallback / time unknown / file type / extension / deleted files |
| F Task | PENDING→RUNNING→SUCCESS / PENDING→RUNNING→FAILED / retry / reindex / duplicate enqueue / crash while RUNNING |

**必须验证的关键不变量**：**「SQLite 中没有对应 chunks 三元组（file_id, source_type, chunk_index）的 Qdrant point 永远不能进入最终结果。」**

---

# 15. MVP 开发计划【每里程碑独立可运行可演示】

## 15.1 Phase 1 — MVP（单人约 10~11 周）

| 里程碑 | 内容 | 可演示效果 | 工期 |
|---|---|---|---|
| **M0 骨架** | monorepo（common/server/worker 分层）、Electron 拉起 FastAPI/Worker/Qdrant、健康检查、IPC、dev-all | 窗口打开即三子进程就绪（四进程体系：Electron + FastAPI + Worker + Qdrant） | 1 周 |
| **M1 扫描+文件名搜索** | 全量扫描→files 表、Watchdog 增量、删除同步（顺序语义）、文件名 FTS5、搜索 UI v1（raw query→FTS） | 「Everything 模式」：选目录后文件名毫秒检索，新增/删除即时反映 | 2 周 |
| **M2 文档全文搜索** | TXT/MD/PDF/DOCX 提取、切分、chunks+fts_body（index_file 任务贯通）、Detail 面板 | 搜正文关键词命中文档段落，含高亮片段 | 1.5~2 周 |
| **M3 OCR 搜索** | PaddleOCR 流水线、ocr_text + chunks(ocr)、FTS 同步、OcrPanel | 搜「New York」命中图片中的文字 | 1.5 周 |
| **M4 语义搜索** | **Caption 模型 benchmark 并冻结选型**（ADR）、BGE embedding、Qdrant 同步、语义通道单独可用 | 按「自由女神」向量召回图片与文档段落 | 2 周 |
| **M5 Hybrid + 产品化** | Query Parser（**规则实现**；Local LLM Parser 后置 P2）、UnifiedFilter、双通道并行、RRF、match_reasons、time_basis/confidence、Settings、基础 Task Dashboard、Benchmark harness 首次报告 | **核心演示：「昨天拍的自由女神照片」一次命中，理由逐条展示** | 2 周 |

关键路径 M1→M5；Caption 模型风险在 M4 首日以 benchmark 冻结（备选视觉标签文本生成兜底，见 §10.6）。

## 15.2 Phase 2 — 工程增强（约 4~5 周）

USN Journal 启动恢复｜断点续扫｜hash 复用 AI 结果｜Worker 自动重试/任务取消/启动恢复｜优雅退出 drain｜崩溃自愈｜托盘+全局快捷键｜**Local LLM Query Parser（可选增强）**｜模型版本升级与重建｜每日对账｜缩略图缓存｜Qdrant payload 冗余过滤优化｜Benchmark 调优（HNSW/batch_size/批量参数）｜PyInstaller + electron-builder 打包。

## 15.3 Phase 3 — Future Extensions（仅记录）

云端 VLM Provider（单 Provider 启用）、以图搜图（CLIP 独立图像向量 collection）、人脸识别、语音搜索、视频理解、插件系统、跨设备同步、文件组织助手。

---

# 16. 性能设计【Benchmark Framework + 目标区间】

## 16.1 Benchmark 框架（M5 建立 harness，P2 持续调优）

**数据集 tier**：10K files / 100K files / 1M metadata records / 100K vectors / 1M vectors（混合类型，含中文文档与照片）。

**指标（记录 p50 / p95）**：`scan_throughput`（files/min）、`sqlite_write_throughput`（rows/s）、`fts5_latency`、`vector_latency`、`hybrid_latency`（端到端 /search）、`embedding_throughput`（**texts/s、ms/text、CPU usage、RSS**）、`memory_usage`（各进程稳态 RSS）。

## 16.2 目标区间（待 benchmark 验证，非承诺）

| 环节 | 方案 | 目标（待实测） |
|---|---|---|
| 扫描 | 8 线程生产-消费、有界队列 10k | **分钟级**完成 10 万文件（以 SSD benchmark 实测为准） |
| SQLite | WAL + executemany 1000 行/事务 + 读写连接分离 | **不设硬性性能承诺**，以 benchmark 实测为准（记录 rows/s） |
| FTS5 | contentless + external content + jieba 预分词 | 10 万文件 p95 < 100ms |
| Qdrant | 单 collection + HNSW 初始参数 | 10 万点 p95 < 30ms；百万点 benchmark 后再定 |
| Hybrid | 双通道并行 + RRF top-200 | 端到端 p95 < 300ms |
| Embedding | **batch_size 初始 32，最终以 benchmark 确定** | 记录 texts/s、ms/text、CPU、RSS 后定目标 |
| 缓存 | Query 缓存为 **P2 optional、默认关闭**（启用时必须在索引变化后主动失效）；缩略图 LRU（P2） | 命中时端到端 < 50ms |
| 资源守护 | Worker BELOW_NORMAL 优先级；活跃时节流（P2） | UI 恒 60fps |

**内存预算目标**：稳态 < 2GB（待实测校准；低配可换小模型降档）。

---

# 17. 后续扩展方向【Phase 3 详细】

| 方向 | 技术路径 | 复用现状 |
|---|---|---|
| 云端 VLM 增强 | CloudImageCaptionProvider（单 Provider 启用） | ImageCaptionProvider 接口已预留 |
| 以图搜图 | CLIP 图像向量 + 独立 collection（Query by Example） | 不混用 BGE 文本空间 |
| 人脸识别 | InsightFace/ArcFace 人脸向量 + person 表 | 复用 Worker 流水线 |
| 语音搜索 | 本地 Whisper ASR → 既有 Query 解析 | 复用 query 管线 |
| 视频理解 | ffmpeg 关键帧抽帧 + ASR → 复用图片/文档流水线 | 扩展 pipeline |
| 跨设备同步 | 元数据+描述文本导出/导入合并 | 数据导出接口 |
| 插件系统 | 解析插件遵循 Provider 协议 | 接口已预留 |
| 文件组织助手 | 重复文件检测、聚类归类建议 | 复用 metadata |

---

# 18. Final Consistency Checklist（交付前逐项核验）

- [ ] ai_tasks → Worker → files.status 闭环（PENDING→RUNNING→SUCCESS/AI_DONE | FAILED；FAILED 保留部分结果）
- [ ] chunks UNIQUE(file_id, source_type, chunk_index) ↔ point_id（logical_key = file_id:source_type:chunk_index）一一对应
- [ ] FTS INSERT/UPDATE/DELETE 全覆盖（fts_files 经 FtsRepository 封装；fts_body 触发器；contentless-delete 选择冻结于 ADR-005）
- [ ] fts_body rebuild 只包含 doc_chunk / ocr（content 指向过滤 VIEW）
- [ ] image_caption 永远不进入 FTS keyword channel（仅 Vector）
- [ ] UnifiedFilter.time_range 使用 timezone-aware RFC3339；时间最终统一转换为 UTC epoch
- [ ] canonical WHERE 三处一致（files 查询 / FTS join / 向量回表 join；Qdrant 不做独立过滤语义）
- [ ] 删除顺序：SQLite is_deleted=1 先行 → 搜索排除 → 异步清理 chunks/FTS/Qdrant/ai_tasks
- [ ] reindex 失败不破坏旧搜索能力（§10.3 / §11.5 / §12.8 三处一致）
- [ ] RRF 只有 FTS + Vector（Metadata 不参与融合）
- [ ] Qdrant 旧 point 不会污染结果（chunks 三元组校验）
- [ ] time unknown 默认排除（Include unknown time 为 P2）
- [ ] retry / reindex 语义明确（attempt ≥ max_attempts → MAX_ATTEMPTS_EXCEEDED）
- [ ] Caption fallback 只产生文本标签，不产生 CLIP image vector
- [ ] Qdrant HTTP/gRPC 端口成对分配（6333/6334 → 6335/6336 → …），Main 注入 FastAPI 配置
- [ ] 时间与时区单一实现（UTC epoch 存储 / 本地时区解析 / QueryParser + TimeRangeService）
- [ ] 无新增基础设施（无 Redis/Celery/Kafka/PostgreSQL/ES/微服务）
- [ ] MVP/P2/P3 边界没有越界（LLM Parser、USN、hash 复用、自动重试、Query 缓存均未提前）
- [ ] 安全性检测：Renderer 沙箱（contextIsolation/sandbox/nodeIntegration:false）；本机 token 鉴权；omnisearch:// 协议 file_id 白名单；模型文件 sha256 校验；SQL 参数化防注入；路径遍历防护
