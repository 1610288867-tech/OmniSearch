# ADR-005：fts_files 的 contentless-delete 选择 + point_id hash 算法冻结

- 状态：Accepted
- 日期：2026-08-15

## Context

`fts_files`（文件名/路径索引）采用 contentless 模式省 2~3 倍索引膨胀，但普通 contentless 表**不支持普通 UPDATE/DELETE**（官方文档明确要求使用 FTS5 special delete command）；contentless-delete 表（SQLite ≥ 3.43）支持普通 UPDATE/DELETE 与 integrity-check。同时 Qdrant point_id 的 hash 算法需要冻结，避免实现阶段随意变更导致索引失效。

## Decision

1. **fts_files 模式选择（运行时检测，实现阶段不得自行改变）**：
   - SQLite ≥ 3.43 → `contentless-delete` 表：
     ```sql
     CREATE VIRTUAL TABLE fts_files USING fts5(
         filename, filename_seg, dir_tokens,
         content='', contentless_delete=1, tokenize='unicode61');
     ```
   - SQLite < 3.43 → 普通 contentless 表 + **FTS5 special delete command**：
     `INSERT INTO fts_files(fts_files, rowid, ...) VALUES('delete', ?, ...)`
2. **FtsRepository 统一封装（强制）**：`insert / delete / replace(update) / integrity_check('integrity-check')` 四个方法；**业务层禁止直接操作 fts_files**。
3. **fts_body（external content）指向过滤 VIEW**：
   ```sql
   CREATE VIEW fts_chunks_source AS
       SELECT id, chunk_text, chunk_text_seg
       FROM chunks WHERE source_type IN ('doc_chunk', 'ocr');
   ```
   保证 **FTS rebuild 也只包含 doc_chunk + ocr**，image_caption 永不进入关键词通道。
4. **Qdrant point_id 算法冻结**：
   ```
   logical_key = f"{file_id}:{source_type}:{chunk_index}"
   point_id    = xxh3_64(logical_key)   # 64 位无符号整数
   ```
   **算法变更必须触发 Qdrant 全量重建**。三元组与 chunks 表 `UNIQUE(file_id, source_type, chunk_index)` 一一对应，幂等 upsert 覆盖。

## Consequences

- rename/重新扫描的 fts_files 更新语义正确（contentless-delete 用 UPDATE；普通 contentless 用 special delete + INSERT）
- image_caption 与关键词通道彻底隔离（含 rebuild 场景），语义通道（Vector）不受影响
- 集成测试 12 项覆盖：含 image_caption 的 rebuild、caption 独有关键词不命中、doc/ocr 正常命中等
- point_id 稳定：重跑任务、重索引、重启均幂等
