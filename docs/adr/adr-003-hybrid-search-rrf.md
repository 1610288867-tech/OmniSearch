# ADR-003：Hybrid Search = Metadata Filter + FTS/Vector 双通道加权 RRF

- 状态：Accepted
- 日期：2026-08-15

## Context

三个候选来源（Metadata / FTS5 / Qdrant）如何组织成最终结果。备选：三通道都参与分数融合（加权求和）、仅关键词+向量融合、RRF。

## Decision

1. **通道语义（最终）**：
   - Metadata = **Filter**（非检索通道）：time / file_type / extension / is_deleted，**不参与 RRF**
   - FTS5 = Keyword Retrieval（filename + 文档正文 + OCR）
   - Qdrant = Semantic Retrieval（image_caption / doc_chunk / ocr）
   - **RRF = FTS + Vector**
2. **统一 Filter Model**：QueryParser 输出 `{time_range, file_types, extensions, include_deleted}`，SQLite 与 Qdrant 各自 Filter Builder 消费**同一结构**。**SQLite 是过滤正确性的事实来源**——canonical WHERE 应用于 files 查询 / FTS join / 向量回表 join 三处；Qdrant payload filter 仅作性能优化（P2），不得出现两套过滤语义。
3. **融合算法：加权 RRF**（跨通道分数不可直接相加：bm25 与 cosine 分布不同；RRF 只依赖位次、对分数分布畸变免疫、无需校准参数）：
   ```
   rrf_score(d) = w_kw / (k + rank_kw(d)) + w_sem / (k + rank_sem(d))
   k=60（初始值，benchmark 调优）；默认 w_kw = w_sem = 1.0（Settings 可调）
   ```
4. **Score 语义**：API 返回 `rrf_score`（融合分）、`keyword_score`（BM25 原始分）、`semantic_score`（cosine），不产生歧义。
5. **向量候选防污染**：Qdrant point 回 SQLite 时校验 **chunks 三元组（file_id, source_type, chunk_index）**存在，再应用 canonical WHERE——旧 point 即使 file_id 存在也无法进入最终结果。

## Consequences

- 排除「三通道加权求和」：Metadata 分数与检索分数不可比，且硬过滤语义必须 100% 满足
- 排除分数归一化加权：需要维护每通道校准参数，RRF 更简单
- 时间/类型条件 100% 由 canonical WHERE 保证；语义只影响排序
- 通道异常降级行为见 architecture.md §12.8（degraded_channels 如实标注）
