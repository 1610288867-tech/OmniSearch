# ADR-001：AI Worker 独立进程 + index_file 单任务模型（无 DAG）

- 状态：Accepted
- 日期：2026-08-15

## Context

文件 AI 处理（Caption / OCR / 文档提取 / Embedding）是 CPU 与内存密集操作。需要决定：在 FastAPI 进程内用线程池执行，还是独立进程执行；以及任务如何切分。

## Decision

1. **AI Worker 为独立 Python 进程**（单 Worker 单实例），与 FastAPI 完全隔离：
   - GIL/延迟隔离：ONNX 推理、OCR、大文档解析不拉高 API 延迟，保证「搜索永远轻载」
   - 内存与故障隔离：模型常驻数百 MB~1GB，崩溃只重启 Worker
   - 可独立降级：CPU 紧张时可单独降低 Worker 优先级，索引停但搜索不停
2. **任务模型：`task_type='index_file'` 单任务制，不做任务 DAG**。一个文件由**一个任务**负责完整处理流程，Worker 内部按 file_type 分支：
   - image: decode → Caption → OCR → chunks/FTS → Embedding → Qdrant
   - document: extract → chunk → FTS → Embedding → Qdrant
3. **MVP 仅 4 态**：PENDING → RUNNING → SUCCESS | FAILED。无 DEAD/CANCELLED/worker_id/复杂退避。
4. **去重入队（数据库级保证）**：partial unique index `ON ai_tasks(file_id) WHERE status IN ('PENDING','RUNNING')`，同一 file_id 最多一个活跃任务。
5. **retry 与 reindex 区分**：
   - retry：复用 FAILED 任务，FAILED → PENDING（attempt 累计；attempt ≥ max_attempts 时 API 拒绝并返回 `MAX_ATTEMPTS_EXCEEDED`）
   - reindex：创建**新的** index_file task；SUCCESS/FAILED 历史任务保留
6. **FAILED 语义**：task FAILED 表示 index_file **未完整完成，不代表文件完全不可搜索**——已成功生成的 OCR/chunks/FTS/embedding 结果默认保留，搜索按文件现有能力降级执行。

## Consequences

- 放弃 FastAPI 内线程池方案（无法隔离延迟/内存/故障）
- 放弃任务 DAG（MVP 复杂度不匹配；单任务 + 内部分支已覆盖全部需求）
- P2 再引入：自动重试（退避）、任务取消、启动时 RUNNING 任务恢复
- 状态机保持简单，UI（Task Dashboard）直接读 ai_tasks 表即可展示队列全貌
