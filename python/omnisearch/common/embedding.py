"""BGEEmbeddingProvider（M4：architecture.md §10.7）。

- ONNX Runtime CPU 推理，模型从用户数据目录加载（manifest 驱动 + sha256 校验）
- 默认 BGE-small-zh v1.5（512 维）；不做 CLS 之外的 pooling 切换
- embed_texts：batch 推理（初始 batch_size=32，benchmark 后可调）
- embed_query：BGE 官方中文 query instruction 前缀（v1.5 检索质量建议）
- 返回 L2 归一化向量（Qdrant Cosine 空间）
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort

logger = logging.getLogger("omnisearch.embedding")

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingProvider(Protocol):
    @property
    def dim(self) -> int: ...

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> list[list[float]]: ...


class BGEEmbeddingProvider:
    """BGE-small-zh ONNX（Xenova 导出）：CLS pooling + L2 normalize。"""

    def __init__(self, model_dir: Path):
        self._model_dir = model_dir
        self._dim: int | None = None
        self._session: ort.InferenceSession | None = None
        self._lock = threading.Lock()

    # ---- 惰性加载（首次 embed 时初始化；模型常驻进程） ----
    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            model_path = self._model_dir / "model.onnx"
            if not model_path.exists():
                raise RuntimeError(f"BGE model not found: {model_path}（请先运行 download_models.py）")
            self._session = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )
            # 维度取模型输出（last_hidden_state: [1, seq, hidden]）或 config.json
            config = self._model_dir / "config.json"
            if config.exists():
                self._dim = int(json.loads(config.read_text(encoding="utf-8")).get("hidden_size", 512))
            else:
                out = self._session.get_outputs()[0].shape[-1]
                self._dim = int(out)
            logger.info("BGE loaded: dim=%d (model_dir=%s)", self._dim, self._model_dir)

    @property
    def dim(self) -> int:
        self._ensure_loaded()
        assert self._dim is not None
        return self._dim

    # ---- tokenize（transformers tokenizer.json） ----
    def _tokenize(self, texts: list[str], max_len: int = 512):
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(str(self._model_dir / "tokenizer.json"))
        encoded = tok.encode_batch(texts)
        ids = [e.ids[:max_len] for e in encoded]
        masks = [[1] * len(i) for i in ids]
        max_seq = max(len(i) for i in ids) if ids else 1
        input_ids = np.zeros((len(texts), max_seq), dtype=np.int64)
        attention_mask = np.zeros((len(texts), max_seq), dtype=np.int64)
        token_type_ids = np.zeros((len(texts), max_seq), dtype=np.int64)
        for r, (i, m) in enumerate(zip(ids, masks)):
            input_ids[r, : len(i)] = i
            attention_mask[r, : len(m)] = m
        return input_ids, attention_mask, token_type_ids

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """批量 embedding（CLS pooling + L2 normalize）。"""
        if not texts:
            return []
        self._ensure_loaded()
        assert self._session is not None and self._dim is not None
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            input_ids, attention_mask, token_type_ids = self._tokenize(batch)
            outputs = self._session.run(
                None,
                {"input_ids": input_ids, "attention_mask": attention_mask, "token_type_ids": token_type_ids},
            )
            hidden = outputs[0]  # [B, seq, hidden]
            cls = hidden[:, 0, :]  # CLS pooling（BGE 官方）
            norm = np.linalg.norm(cls, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            vectors.extend((cls / norm).astype(np.float32).tolist())
        return vectors

    def embed_query(self, query: str) -> list[float]:
        """查询 embedding（BGE v1.5 中文 instruction 前缀）。"""
        return self.embed_texts([QUERY_INSTRUCTION + query], batch_size=1)[0]
