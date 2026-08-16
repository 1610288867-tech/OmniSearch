# CLAUDE.md

OmniSearch —— 本地 AI Hybrid Retrieval Engine（Windows 桌面本地文件搜索：Everything 级文件名检索 + 自然语言查询 + 图片/文档语义理解，全部本地离线）。

**架构权威文档：[docs/architecture.md](docs/architecture.md)（v4 Final）**。本文档的规则均提取自它；冲突时以 architecture.md 为准。实现前先读对应章节。

## 常用命令

- 一键拉起开发环境（Electron + FastAPI + Worker + Qdrant 四进程）：`python python/scripts/dev.py`（M0 落地）
- 后端测试：`cd python && pytest`
- 模型下载（manifest 驱动 + sha256 校验）：`python python/scripts/download_models.py`

## 不可违反的工程规则

### 1. 进程与目录（冻结）
- 四进程体系：Electron Main（宿主）→ spawn FastAPI / AI Worker / Qdrant（Sidecar，非 Docker）。Qdrant HTTP/gRPC 端口**成对顺延**（6333/6334 → 6335/6336 → …），由 Main 探测并注入 FastAPI 配置。
- Python 目录分层：`common/`（唯一共享层）/ `server/`（FastAPI）/ `worker/`（AI Worker）。SQLite 连接门面在 `common/database.py`（两进程共用）；`server/database/` 仅存 migrations（只由 server 启动时执行迁移）。
- **依赖方向**：只允许 `server → common`、`worker → common`。禁止 `worker → server.repository`、禁止 `server → worker.pipeline`。

### 2. 禁止引入（MVP 红线）
- 基础设施：Redis / Celery / Kafka / PostgreSQL / Elasticsearch / 微服务 / 消息总线
- 抽象：任务 DAG、Provider Registry、通用 LLM Agent、trigram 独立索引
- MVP 禁止：CLIP image vector、LLM Query Parser（P2）、异步 Qdrant upsert（wait=false）

### 3. SQLite = 事实数据源；Qdrant = 可重建索引
- 所有搜索过滤、文件元数据、状态的事实来源都是 SQLite；Qdrant 只是可从 chunks 重建的向量索引。
- **删除顺序**：`files.is_deleted=1` 先行（立即从搜索结果排除，不依赖 Qdrant 清理）→ 异步清理 chunks/FTS/Qdrant/ai_tasks。
- **同路径复活**：写入 path 已存在且 is_deleted=1 → 复活原记录（is_deleted=0 + 状态重置 + 入队），不新建 file_id。
- **reindex 一致性**（§10.3/§11.5/§12.8 三处一致）：不先删旧 chunks；解析/OCR/Embedding 全部在 SQLite 事务外；完整成功后单个短事务替换（DELETE old + INSERT new + FTS 触发器）；失败则旧数据全保留、task=FAILED。**「重新索引失败时，旧搜索能力必须保持可用。」**
- `chunks.source_type` 创建后不可变（变更 = DELETE + INSERT）。`UNIQUE(file_id, source_type, chunk_index)`。
- 状态枚举：`files.status` = METADATA_ONLY | PROCESSING | AI_DONE | FAILED（文件级 pipeline）；`chunks.embedding_status` = 0 PENDING | 1 SUCCESS | 2 FAILED（chunk 级向量状态）。embedding 失败不影响该 chunk 的 FTS 可搜索性。

### 4. Qdrant 规则
- point_id 算法**冻结**：`logical_key = "{file_id}:{source_type}:{chunk_index}"`，`point_id = xxh3_64(logical_key)`。算法变更必须触发全量重建。三元组与 chunks UNIQUE 一一对应，幂等 upsert。
- **同步 upsert（wait=true）**：成功 → embedding_status=SUCCESS；失败 → FAILED。
- 重索引顺序：先 upsert new_points → 再算 stale（logical_key 差集）异步删除；stale 清理失败不影响 new_points。
- **旧 point 防污染**：向量候选回 SQLite 时必须校验 chunks 三元组存在，再 join files 应用 canonical WHERE——不能只回查 files.file_id。

### 5. 任务队列（ai_tasks，单机单 Worker 轻量持久化队列）
- `task_type='index_file'` 唯一任务类型（一个文件一个任务，无 DAG），4 态：PENDING → RUNNING → SUCCESS | FAILED。
- partial unique index：`ON ai_tasks(file_id) WHERE status IN ('PENDING','RUNNING')` —— 数据库级防重复入队。
- retry = 复用 FAILED 任务置回 PENDING；attempt ≥ max_attempts → 拒绝并返回 `MAX_ATTEMPTS_EXCEEDED`。reindex = 新建任务（历史保留）。
- 所有事务必须短事务；claim/回写短事务内完成；推理/OCR/Embedding 一律在事务外。

### 6. FTS5 规则
- `fts_files`（文件名/路径）：contentless-delete（SQLite ≥ 3.43）优先；否则普通 contentless + **FTS5 special delete command**（禁止普通 UPDATE/DELETE）。运行时按 `sqlite_version` 检测。
- `fts_body`（正文）：external content，`content` 指向过滤 VIEW `fts_chunks_source`（`WHERE source_type IN ('doc_chunk','ocr')`）——**rebuild 也只含 doc_chunk+ocr；image_caption 永不进 FTS 关键词通道（仅 Vector）**。
- **FTS 读写只经 FtsRepository**（insert/delete/replace/integrity_check 四方法），业务层禁止直接操作 FTS 表。
- 中文：jieba 预分词存 `*_seg` 列 + unicode61。查询降级：phrase → AND → OR（召回不足自动降级）。

### 7. 搜索规则（Hybrid）
- 通道语义：**Metadata = Filter（不参与 RRF）；FTS5 = Keyword Retrieval；Qdrant = Semantic Retrieval；RRF = FTS + Vector 双通道**。
- `rrf_score(d) = w_kw/(k+rank_kw) + w_sem/(k+rank_sem)`，k=60 初始、权重默认 1.0。
- **canonical WHERE 三处一致**：files 查询 / FTS join / 向量回表 join 必须应用同一 `FilterBuilderService` 生成的 WHERE（is_deleted=0 + 类型 + 扩展名 + 时间条件）。Qdrant payload filter 仅 P2 性能优化，不得出现第二套过滤语义。
- API score 字段：`rrf_score` / `keyword_score`（BM25）/ `semantic_score`（cosine），不得用裸 `score`。
- 通道异常必须降级并如实填 `degraded_channels`（语义/关键词任一可用即返回结果；SQLite 不可用 → 明确报错）。
- 时间可信度：exif=exact（hard filter）；mtime/ctime=fallback（结果必须标注）；**unknown 默认排除**（Include unknown 为 P2）。响应必须含 `time_info: {basis, confidence, value}`。

### 8. 时间与时区（单一实现）
- 存储：SQLite 内统一 **UTC unixepoch seconds**。用户输入按 Windows 当前时区解释；EXIF 视为设备本地时间再换算。
- API/DTO 的 time_range 必须是 **offset-aware RFC3339**（如 `2026-08-14T00:00:00+08:00`），**禁止 timezone-naive datetime**。
- 日期逻辑统一在 `QueryParser + TimeRangeService`；禁止 server/worker/frontend 各自实现日期计算。

### 9. QueryParser MVP 边界
- 只做：时间表达 / 文件类型 / 扩展名 / 简单关键词提取 / 剩余文本 → semantic_text。
- 禁止：意图识别、实体抽取、多轮理解、LLM Agent、自由 JSON schema 扩展。
- 解析失败兜底：`semantic_text = 原始 query`，Parser 失败不得导致搜索失败。

### 10. 文件身份与 RENAME
- 运行期间 RENAME 且目标不存在：保留 file_id/chunks/OCR/Caption/Embedding/Qdrant points，仅更新 path/filename/dir_path。
- 目标 path 已存在（conflict）：**不自动覆盖、不删除目标记录**——标记 conflict，对 source+target 重新扫描比对后再决定。
- 应用关闭期间的移动：MVP 不保证识别为同一文件（按删除+新增处理；P2 由 content_hash 复用）。

### 11. MVP / P2 / P3 边界（不得越界提前实现）
- P2 才做：USN Journal、断点续扫、hash 复用、Worker 自动重试/任务取消/启动恢复、优雅退出 drain、崩溃自愈、托盘/快捷键、Local LLM Query Parser、模型版本升级重建、Query 缓存（默认关闭+主动失效）、缩略图缓存、打包发布。
- P3 才做：云端 VLM Provider、以图搜图（CLIP 独立 collection）、人脸、语音、视频、插件、跨设备同步。
- AI 语言约束：MVP 默认 **中文 Query → 中文 Caption → BGE-small-zh**；不承诺 BGE 跨语言匹配。

### 12. 安全基线
- Renderer：`contextIsolation: true` / `nodeIntegration: false` / `sandbox: true`，仅经 preload 类型化 API。
- FastAPI：本机 token（`X-Omni-Token` 头）鉴权；SQL 全部参数化。
- `omnisearch://` 预览协议必须校验 file_id 白名单；模型文件 sha256 校验；路径遍历防护。

### 13. 关键测试不变量
- **「SQLite 中没有对应 chunks 三元组（file_id, source_type, chunk_index）的 Qdrant point 永远不能进入最终结果。」**
- 集成测试矩阵见 architecture.md §14.1（文件生命周期 / FTS / Qdrant / Hybrid / Filter / Task 六域，共 30+ 用例）。

### 14. 性能纪律
- 不写死性能承诺（无 benchmark 支撑的 <Xms / ≥Y条/s 表述）。benchmark 后记录 p50/p95（texts/s、ms/text、CPU、RSS 等）。
- 参数初始值可调优：HNSW m=16/ef=64、batch_size=32、poll_interval_ms=500、RRF k=60。

## 文档索引

- 架构设计（18 章 + Final Consistency Checklist）：[docs/architecture.md](docs/architecture.md)
- 决策记录：[docs/adr/](docs/adr/)（ADR-001 Worker 进程模型 / ADR-002 SQLite 队列 / ADR-003 Hybrid RRF / ADR-004 Caption 选型 / ADR-005 FTS 模式与 point_id）
- 需求原文：request.txt
