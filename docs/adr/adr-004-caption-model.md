# ADR-004：Caption 模型选型（M4.3 benchmark 后冻结）

- 状态：Accepted（M4.3 冻结）
- 日期：2026-08-16

## Context

图片语义链路需要「图片 → 中文描述文本 → BGE-small-zh → Qdrant」。候选按 architecture.md §10.6：
Florence-2-base-ft / Qwen2-VL-2B-Instruct / InternVL2-1B。约束：ONNX Runtime + CPU + Windows 桌面、中文输出、模型不进入 Git。

## 候选评估（M4.3 实测）

| 候选 | 官方 ONNX | 中文 caption | 磁盘（估） | CPU 推理（估） | 结论 |
|---|---|---|---|---|---|
| Florence-2-base-ft | ❌ 无官方导出（需 torch + optimum 自导出） | 弱（英文为主） | ~700MB | 30-90s/图（生成式） | **受限**：导出工具链重、中文不达标 |
| Qwen2-VL-2B-Instruct | ❌ 需自导出（vision+LLM decoder 复杂） | 强 | ~2GB | 分钟级/图 | **受限**：桌面 CPU 不可现实运行 |
| InternVL2-1B | ❌ 需自导出 | 中 | ~1GB | 分钟级/图 | **受限**：同 Qwen |

**结论**：三个 VLM 候选在「ONNX + CPU + 桌面」约束下均不可现实落地（无官方导出、生成式推理过慢、中文质量与体积冲突）。

## Decision：启用 MVP fallback（architecture.md §10.6 既定方案）

**Zero-shot Visual Tagging（中文标签文本生成）**——冻结 Chinese-CLIP（OFA-Sys/chinese-clip-vit-base-patch16）：

```
Image → Chinese-CLIP 视觉端(ONNX) → 中文标签词表零样本分类 → 中文标签文本 → BGE → Qdrant
```

- 只生成文本标签；**不保存 CLIP image embedding；不建立 image-vector collection**（以图搜图为 Phase 3）
- 中文标签词表（66 标签：场景/物体/属性），top-3 标签 + 置信度阈值 0.20
- 模型：自导出 ONNX（vision 512 维 + text 512 维，opset 14，传统导出 dynamo=False）

**实测指标（M4.3 benchmark，固定测试集：风景/人物/建筑/文档/含文字/中文场景）**：

| 指标 | 值 |
|---|---|
| 模型磁盘占用 | 718 MB（vision 327MB + text 388MB + tokenizer） |
| 首次推理（含模型加载） | 8.8 s/图（CPU） |
| 稳态推理 | 0.19 s/图（CPU） |
| 中文标签输出 | ✅（如「海报，截图，彩色」；置信度 0.39 与官方 API 一致） |
| 与官方 API 对齐验证 | ✅（相似度同量级 0.31-0.39） |

**Benchmark 说明**：VLM 候选未逐一下载（下载/导出成本数 GB + 数小时且 CPU 推理不可现实落地）；如实记录限制而非伪装完成。若未来需要高质量中文描述，P3 云端 VLM Provider 覆盖。

## 边界

- Phase 3 以图搜图（CLIP image embedding + 独立 collection）与本 fallback 完全分离
- 图片语义 = image_caption chunk（仅 Vector 通道，**永不进 FTS**——fts_chunks_source VIEW 语义不变）
- 若未来 benchmark 证明 VLM 可行（如 ONNX 生态成熟），重新评估（本 ADR 更新）
