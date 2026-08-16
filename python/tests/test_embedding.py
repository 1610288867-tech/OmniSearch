"""Embedding + 模型管理测试（M4 A/F 域：architecture.md §10.8）。

真实 BGE-small-zh ONNX（dev-data/models）；模型管理用 manifest + sha256 校验。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from omnisearch.common.embedding import BGEEmbeddingProvider
from omnisearch.common.utils.models import download_model, load_manifest, sha256_file


def test_model_load_and_dim(bge):
    """A1. model load + A4. dimension check（512）。"""
    assert bge.dim == 512


def test_batch_embedding(bge):
    """A3. batch embedding：5 条文本 batch=2，输出维度一致。"""
    texts = [f"测试文本内容第{i}号" for i in range(5)]
    vecs = bge.embed_texts(texts, batch_size=2)
    assert len(vecs) == 5
    assert all(len(v) == 512 for v in vecs)


def test_embedding_normalized(bge):
    """向量 L2 归一化（Cosine 空间）。"""
    import math

    v = bge.embed_texts(["测试"])[0]
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-3


def test_query_instruction(bge):
    """embed_query 带 BGE v1.5 中文 instruction 前缀（与文档向量同空间）。"""
    q = bge.embed_query("自由女神")
    d = bge.embed_texts(["关于自由女神像的历史"])[0]
    import math

    dot = sum(a * b for a, b in zip(q, d))
    assert dot > 0.5  # 语义相近


def test_manifest_hash_validation():
    """F1/F3/F4. manifest + sha256 校验 + missing model。"""
    manifest = load_manifest()
    onnx = next(m for m in manifest["models"] if m["id"] == "bge-small-zh-v1.5-onnx")
    assert onnx["sha256"]  # 已回填
    with pytest.raises(RuntimeError):
        download_model("nonexistent-model")


def test_corrupt_model_rejected(tmp_path):
    """F5. corrupt model：本地文件 sha256 不匹配 → 拒绝（不重新下载）。"""
    manifest = load_manifest()
    onnx = next(m for m in manifest["models"] if m["id"] == "bge-small-zh-v1.5-onnx")
    fake = tmp_path / "models" / "model.onnx"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"corrupt data")
    with pytest.raises(RuntimeError, match="sha256"):
        download_model("bge-small-zh-v1.5-onnx", data_dir=tmp_path)


def test_embedding_failure_missing_model(tmp_path):
    """A6. embedding failure：模型缺失 → embed 抛异常（不破坏调用方）。"""
    p = BGEEmbeddingProvider(tmp_path / "no_model")
    with pytest.raises(RuntimeError, match="not found"):
        p.embed_texts(["x"])
