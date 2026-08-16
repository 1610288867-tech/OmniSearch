# ADR-007：content_hash AI 结果复用（P2.2）

- 状态：Accepted（P2.2）
- 日期：2026-08-16

## Context

文件移动/复制/rename/重扫时，即使内容未变也会重新执行 OCR/Caption/Embedding，
造成不必要的 AI 计算（CPU 密集）。引入 content_hash：内容相同 → 复用已有 AI 产物。

## 决策

### 1. 为什么 hash / 为什么 xxh3

- 架构 §7.1 已预留 `files.content_hash` 列（P2 用途）——无需迁移
- **xxh3_64**：流式计算（1MB chunk，不整载内存）、~2.4GB/s（实测）、
  内容相同 → 相同 hash；内容不同 → 极高概率不同（128 位雪崩特性）
- 不自行改 SHA256：xxh3 是架构既定选择（与 point_id 同一算法族），防碰撞需求
  （恶意构造碰撞）不在本地搜索场景威胁模型内

### 2. 什么时候计算 hash

不在扫描（FastAPI）对所有文件算 hash；**只在 Worker 的 index_file pipeline 内**计算：
- 处理前：`content_hash_xxh3(path)`（流式，事务外）
- 判定复用（`_resolve_reuse`）→ 复用 / 正常处理
- 正常处理完成时（短事务内）写回 `files.content_hash`

模型文件不参与 hash reuse（独立资产，manifest 管理）。

### 3. 复用规则

| 场景 | 行为 |
|---|---|
| A 同 file_id 内容未变（touch/重扫触发） | 跳过全部 AI，旧 chunks/embedding 保留，status=AI_DONE |
| B/C rename / 移动（file_id 保留） | 直接复用（MVP rename 语义已保留 chunks——不额外处理） |
| D 复制（新 file_id，同 hash） | 复用 ocr_text + chunks（文本/seg/token_count）+ embedding |
| E 跨路径移动（关闭期 delete+create，同 hash） | 复用旧（含 is_deleted=1 源）AI 产物，**新 file_id**（不恢复旧 id） |

### 4. Qdrant point_id 处理（关键）

禁止直接复制旧 point_id：
```
旧: point_id = xxh3_64("100:ocr:0")
新: point_id = xxh3_64("200:ocr:0")   ← 必须按新 logical_key 重新计算
```
流程：读旧 point 的 vector+payload（VectorStore.get_vectors）→ 按新 logical_key 计算新 point_id
→ upsert（wait=true）→ 成功后短事务复制 chunks（embedding_status=SUCCESS）。免 BGE inference。

顺序：Qdrant 先 upsert → SQLite 复制事务。Qdrant 失败 → 抛异常（task FAILED，无 SQLite 半成品；
orphan point 由三元组校验防污染）；事务失败 → 回滚（旧文件不受影响）。

### 5. 复用范围与禁止

允许：ocr_text、chunks.chunk_text/seg/token_count、image_caption、embedding vectors、embedding_status。
禁止：file_id/path/filename/dir_path/mtime/ctime/exif 等 metadata（永远属于新 files 记录）。

### 6. Embedding 兼容性

MVP 单模型（BGE-small-zh）：所有 embedding_status=SUCCESS 的 chunk 均为当前模型产物，
可直接复用。若未来换模型（P2 模型升级重建）：只复用文本，重新 embedding
（当前实现不引入模型 registry——最小实现满足现状）。

### 7. 失败与一致性

- hash 计算失败（不可读）→ 抛异常 → task FAILED（不得误复用）
- 复用事务/Qdrant 失败 → task FAILED，旧文件产物完整保留
- 保持 reindex 一致性（§11.5）：新文件不得产生半成品复用结果

## 后果

- 正面：复制/移动/重扫场景免 OCR/Caption/Embedding（实测 copy 图片任务数不变、
  向量复制而非重算）；hash 计算成本 ~2.4GB/s 可忽略
- 限制：首次处理仍全量 AI（hash 建立后生效）；deleted 源复用依赖 chunks 保留
  （删除不删 chunks ✓）
- 不做：不做内容去重合并（同 hash 多文件各自保留独立 file_id/索引），不做 hash 缓存
