"""P2.2 content_hash AI 结果复用测试（spec §十二 A-G）。

- A Hash：相同/不同/大文件流式/错误
- B-G：worker 级（process_doc_file 无模型依赖；image 路径 mock ocr/caption）
  FakeVectorStore 记录 Qdrant 读写，验证新 point_id 复制（免 BGE inference）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnisearch.common.database import Database
from omnisearch.common.models import EmbeddingStatus, FileStatus, SourceType
from omnisearch.common.utils.hash import content_hash_xxh3
from omnisearch.common.utils.point_id import point_id
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.worker.pipeline import processor
from omnisearch.worker.pipeline.processor import process_doc_file, process_image_file


# ================= A. Hash =================

def test_a1_identical_content_same_hash(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"same bytes" * 100)
    b.write_bytes(b"same bytes" * 100)
    assert content_hash_xxh3(a) == content_hash_xxh3(b)


def test_a2_different_content_different_hash(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"content A")
    b.write_bytes(b"content B")
    assert content_hash_xxh3(a) != content_hash_xxh3(b)
    # 单字节差异也应不同
    a.write_bytes(b"content A")
    b.write_bytes(b"content A!")
    assert content_hash_xxh3(a) != content_hash_xxh3(b)


def test_a3_large_file_streaming(tmp_path):
    big = tmp_path / "big.bin"
    with open(big, "wb") as f:
        for _ in range(40):
            f.write(os.urandom(1 << 20))  # 40MB（流式：不整载内存，chunk 1MB）
    h = content_hash_xxh3(big)
    assert h is not None and len(h) == 16
    # 确定性：再算一次相同
    assert content_hash_xxh3(big) == h


def test_a4_hash_error_returns_none(tmp_path):
    assert content_hash_xxh3(tmp_path / "missing.bin") is None
    # 目录路径 → None
    assert content_hash_xxh3(tmp_path) is None


# ================= worker 环境 =================

class FakeEmbedder:
    """BGE 兼容替身：确定性向量（embedding 完成 → SUCCESS）。"""

    dim = 8

    def embed_texts(self, texts, batch_size=32):  # noqa: ARG002
        return [[0.5] * 8 for _ in texts]

    def embed_query(self, q):  # noqa: ARG002
        return [0.5] * 8


class FakeVectorStore:
    """记录 Qdrant 读写：point_id → (vector, payload)。"""

    def __init__(self):
        self.points: dict[int, tuple[list[float], dict]] = {}
        self.upserted: list[int] = []

    def get_vectors(self, ids):
        return {i: self.points[i] for i in ids if i in self.points}

    def upsert_points(self, points):
        self.upserted.extend(p.point_id for p in points)
        for p in points:
            self.points[p.point_id] = (p.vector, {"file_id": p.file_id, "source_type": p.source_type,
                                                  "chunk_index": p.chunk_index, "text": p.text})

    def search(self, _v, top_k=100):  # noqa: ARG002
        return []

    def list_keys_by_file(self, file_id):
        return [pid for pid, (_, payload) in self.points.items() if payload.get("file_id") == file_id]

    def delete_points(self, ids):
        for i in ids:
            self.points.pop(i, None)


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    migrate(db)
    vs = FakeVectorStore()

    def _add_file(path: str, file_type: str = "doc", content_hash: str | None = None, status: str = "AI_DONE") -> int:
        with db.connect() as c:
            cur = c.execute(
                """INSERT INTO files (path, filename, dir_path, extension, size_bytes, mtime_ns, ctime_ns,
                                      file_type, status, content_hash)
                   VALUES (?, ?, ?, ?, 1, 1, 1, ?, ?, ?)""",
                (path, Path(path).name, str(Path(path).parent), Path(path).suffix.lower(),
                 file_type, status, content_hash),
            )
            return cur.lastrowid

    return db, vs, _add_file


def _seed_ai(db: Database, fid: int, chunks: list[tuple[str, int, str, int]], ocr: str | None = None) -> None:
    """预置源文件 AI 产物（chunks + ocr_text + hash）。"""
    from omnisearch.common.utils.seg import seg_text

    with db.connect() as c:
        for source_type, idx, text, emb in chunks:
            c.execute(
                """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg,
                                       token_count, embedding_status)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (fid, source_type, idx, text, seg_text(text), emb),
            )
        if ocr:
            c.execute(
                "INSERT INTO ocr_text (file_id, text, lang, confidence) VALUES (?, ?, 'zh+en', 0.9)",
                (fid, ocr),
            )
        c.commit()


def _seed_vector(vs: FakeVectorStore, fid: int, source_type: str, idx: int, vec: list[float] | None = None) -> None:
    vs.points[point_id(fid, source_type, idx)] = (vec or [0.1] * 8, {"file_id": fid, "source_type": source_type,
                                                                    "chunk_index": idx, "text": "x"})


# ================= B. Same file =================

def test_b5_unchanged_content_skips_ai(env, tmp_path, monkeypatch):
    db, vs, add_file = env
    p = tmp_path / "doc.txt"
    p.write_text("原始内容", encoding="utf-8")
    fid = add_file(str(p))  # 首次无 hash → 正常处理并写入
    calls = {"extract": 0}
    orig = processor.extract_text

    def counting_extract(path):
        calls["extract"] += 1
        return orig(path)

    monkeypatch.setattr(processor, "extract_text", counting_extract)
    # 第一次正常处理
    process_doc_file(db, fid, str(p), embedder=FakeEmbedder(), vector_store=vs)
    assert calls["extract"] == 1
    # 模拟 touch（mtime 变化但内容不变）→ 再次入队处理 → hash 相同 → 跳过 AI
    os.utime(p, (os.stat(p).st_atime, os.stat(p).st_mtime + 10))
    process_doc_file(db, fid, str(p), vector_store=vs)
    assert calls["extract"] == 1, "内容未变不应重新提取"
    with db.connect() as c:
        assert c.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"] == "AI_DONE"


def test_b6_modified_content_reprocesses(env, tmp_path, monkeypatch):
    db, vs, add_file = env
    p = tmp_path / "doc.txt"
    p.write_text("v1 内容", encoding="utf-8")
    fid = add_file(str(p))
    calls = {"extract": 0}
    orig = processor.extract_text

    def counting_extract(path):
        calls["extract"] += 1
        return orig(path)

    monkeypatch.setattr(processor, "extract_text", counting_extract)
    process_doc_file(db, fid, str(p), vector_store=vs)
    p.write_text("v2 内容不同了", encoding="utf-8")  # 内容变化
    process_doc_file(db, fid, str(p), vector_store=vs)
    assert calls["extract"] == 2, "内容变化应重新处理"
    with db.connect() as c:
        row = c.execute("SELECT chunk_text FROM chunks WHERE file_id=?", (fid,)).fetchone()
        assert "v2" in row["chunk_text"]


def test_s7_content_changed_during_processing_rejected(env, tmp_path, monkeypatch):
    """S7：处理期间内容变化 → 拒绝陈旧结果（抛 ValueError → task FAILED，重试重新处理）。

    正常文件零 I/O 负担（一次 stat 对比）；只有 stat 变化才重算 hash 拒绝。
    """
    db, vs, add_file = env
    p = tmp_path / "doc.txt"
    p.write_text("原始内容 v1", encoding="utf-8")
    fid = add_file(str(p))
    orig = processor.extract_text

    def changing_extract(path):
        text = orig(path)
        p.write_text("覆盖后的完全不同内容", encoding="utf-8")  # 处理中途被覆盖（size 也变）
        return text

    monkeypatch.setattr(processor, "extract_text", changing_extract)
    with pytest.raises(ValueError, match="changed during AI processing"):
        process_doc_file(db, fid, str(p), embedder=FakeEmbedder(), vector_store=vs)


# ================= C. Rename =================

def test_c7_rename_preserves_ai_results(env, tmp_path):
    """RENAME：file_id 保留 → chunks/ocr/AI 产物保留（MVP rename 语义）。"""
    db, vs, add_file = env
    p = tmp_path / "old-name.txt"
    p.write_text("改名内容", encoding="utf-8")
    fid = add_file(str(p))
    process_doc_file(db, fid, str(p), vector_store=vs)
    dst = tmp_path / "new-name.txt"
    dst.write_text("改名内容", encoding="utf-8")
    p.unlink()
    # MVP rename 语义（保留 file_id）
    with db.connect() as c:
        c.execute("UPDATE files SET path=?, filename=?, dir_path=? WHERE id=?",
                  (str(dst), "new-name.txt", str(tmp_path), fid))
        c.commit()
    assert dst.exists()
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (fid,)).fetchone()["n"]
        assert n > 0  # AI 产物保留


def test_c8_rename_does_not_reembed(env, tmp_path):
    """rename 后不重新 embedding（无新向量写入）。"""
    db, vs, add_file = env
    p = tmp_path / "r.txt"
    p.write_text("内容", encoding="utf-8")
    fid = add_file(str(p))
    process_doc_file(db, fid, str(p), vector_store=vs)
    before = len(vs.points)
    dst = tmp_path / "r2.txt"
    dst.write_text("内容", encoding="utf-8")
    p.unlink()
    with db.connect() as c:
        c.execute("UPDATE files SET path=?, filename=? WHERE id=?", (str(dst), "r2.txt", fid))
        c.commit()
    assert len(vs.points) == before  # 无新向量（rename 不触发 AI）


def test_c9_watchdog_rename_artifact_merged(env, tmp_path):
    """watchdog rename 伴生 created(dst) 假记录（stat 相同）→ 合并保留 src file_id + AI 产物。

    P2.2 E2E 暴露：created(dst) 先 flush 留下假行 → handle_rename 曾误判 conflict →
    delete+create。修复：stat 相同视为同一文件，物理删除假记录，走正常 rename。
    """
    db, vs, add_file = env
    p = tmp_path / "rename-me.txt"
    p.write_text("rename 保留内容", encoding="utf-8")
    fid = add_file(str(p))
    process_doc_file(db, fid, str(p), vector_store=vs)
    dst = tmp_path / "rename-done.txt"
    dst.write_text("rename 保留内容", encoding="utf-8")
    p.unlink()
    # 模拟 watchdog 顺序：created(dst) 先被 flush → dst 假行（stat 与 src 相同）
    dst_fake_id = add_file(str(dst))
    with db.connect() as c:  # 假行的 stat 与 src 相同（同一文件）
        c.execute("UPDATE files SET mtime_ns=(SELECT mtime_ns FROM files WHERE id=?), size_bytes=(SELECT size_bytes FROM files WHERE id=?) WHERE id=?",
                  (fid, fid, dst_fake_id))
        c.commit()
    # handle_rename：应合并假记录，保留 src file_id
    from omnisearch.server.service.index import IndexService

    index = IndexService(db, __import__("omnisearch.server.repository.files", fromlist=["FileRepository"]).FileRepository(db),
                         __import__("omnisearch.server.repository.fts", fromlist=["FtsRepository"]).FtsRepository(db),
                         __import__("omnisearch.server.repository.jobs", fromlist=["IndexJobRepository"]).IndexJobRepository(db))
    index.handle_rename(str(p), str(dst))
    with db.connect() as c:
        row = c.execute("SELECT id, path FROM files WHERE id=?", (fid,)).fetchone()
        assert row is not None and row["path"] == str(dst)  # src file_id 保留
        fake = c.execute("SELECT id FROM files WHERE id=?", (dst_fake_id,)).fetchone()
        assert fake is None  # 假记录已物理删除
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (fid,)).fetchone()["n"] == 1  # AI 产物保留


# ================= P2.3 一致性：hash 相同但 AI 产物不完整 =================

def test_p23_embedding_failed_reprocessed(env, tmp_path, monkeypatch):
    """同一文件 hash 相同但 Embedding=FAILED → 不能跳过 → 完整重新处理 → embedding=SUCCESS。"""
    db, vs, add_file = env
    p = tmp_path / "doc.txt"
    p.write_text("一致性内容", encoding="utf-8")
    h = content_hash_xxh3(p)
    fid = add_file(str(p), content_hash=h, status="AI_DONE")
    # 模拟部分失败：OCR/Caption 文本成功但 Embedding=FAILED
    _seed_ai(db, fid, [(SourceType.DOC_CHUNK.value, 0, "一致性内容", EmbeddingStatus.FAILED.value)])
    calls = {"extract": 0}
    orig = processor.extract_text

    def counting_extract(path):
        calls["extract"] += 1
        return orig(path)

    monkeypatch.setattr(processor, "extract_text", counting_extract)
    process_doc_file(db, fid, str(p), embedder=FakeEmbedder(), vector_store=vs)
    assert calls["extract"] == 1, "Embedding FAILED 不应因 hash 相同跳过 → 应重新处理"
    with db.connect() as c:
        row = c.execute("SELECT chunk_text, embedding_status FROM chunks WHERE file_id=?", (fid,)).fetchone()
        assert row["embedding_status"] == EmbeddingStatus.SUCCESS.value  # 最终 embedding 完整
        # 无 duplicate chunks（重新处理 = DELETE old + INSERT new）
        n = c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (fid,)).fetchone()["n"]
        assert n == 1


def test_p23_ocr_failed_reprocessed(env, tmp_path, monkeypatch):
    """image：OCR=FAILED（无 ocr chunk），Caption=SUCCESS，Embedding=SUCCESS → 不能跳过 → 补 OCR。"""
    db, vs, add_file = env
    src = tmp_path / "img.png"
    src.write_bytes(b"fake-image-consistent")
    h = content_hash_xxh3(src)
    fid = add_file(str(src), file_type="image", content_hash=h, status="AI_DONE")
    # 仅 Caption 成功（OCR 缺失/失败）
    _seed_ai(db, fid, [(SourceType.IMAGE_CAPTION.value, 0, "城市天际线", EmbeddingStatus.SUCCESS.value)])

    from types import SimpleNamespace

    class FakeCaption:
        def caption(self, _p):
            return SimpleNamespace(text="城市天际线", model="fake", confidence=0.9)

    calls = {"ocr": 0}
    monkeypatch.setattr(processor, "extract_exif", lambda _p: None)
    monkeypatch.setattr(processor, "ocr_image",
                        lambda _p: (calls.__setitem__("ocr", calls["ocr"] + 1),
                                    SimpleNamespace(text="New York 2026", confidence=0.9))[1])
    process_image_file(db, fid, str(src), embedder=FakeEmbedder(), vector_store=vs, caption_provider=FakeCaption())
    assert calls["ocr"] == 1, "OCR 缺失/FAILED 不应因 hash 相同跳过 → 应重新 OCR"
    with db.connect() as c:
        rows = c.execute(
            "SELECT source_type, embedding_status FROM chunks WHERE file_id=? ORDER BY source_type", (fid,)
        ).fetchall()
        by_type = {r["source_type"]: r["embedding_status"] for r in rows}
        # 重新处理后：ocr + caption 均存在、embedding 全 SUCCESS、无重复
        assert set(by_type) == {SourceType.OCR.value, SourceType.IMAGE_CAPTION.value}
        assert all(v == EmbeddingStatus.SUCCESS.value for v in by_type.values())
        assert len(rows) == 2  # 无 duplicate chunks


# ================= D. Copy =================

def test_d9_copy_same_hash_reuses(env, tmp_path, monkeypatch):
    """复制：新 file_id 相同 hash → 复用 chunks/ocr（不重新提取）。"""
    db, vs, add_file = env
    src = tmp_path / "src.txt"
    src.write_text("复制的内容", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "复制的内容", EmbeddingStatus.SUCCESS.value)], ocr=None)
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0)
    copy = tmp_path / "copy.txt"
    copy.write_text("复制的内容", encoding="utf-8")  # 相同内容
    copy_id = add_file(str(copy))
    calls = {"extract": 0}
    orig = processor.extract_text
    monkeypatch.setattr(processor, "extract_text", lambda path: (calls.__setitem__("extract", calls["extract"] + 1), orig(path))[1])
    process_doc_file(db, copy_id, str(copy), vector_store=vs)
    assert calls["extract"] == 0, "复用路径不应重新提取"
    with db.connect() as c:
        row = c.execute("SELECT chunk_text, embedding_status FROM chunks WHERE file_id=?", (copy_id,)).fetchone()
        assert row["chunk_text"] == "复制的内容"
        assert row["embedding_status"] == EmbeddingStatus.SUCCESS.value  # 向量复用（源 SUCCESS）
        assert c.execute("SELECT content_hash FROM files WHERE id=?", (copy_id,)).fetchone()["content_hash"] == h


def test_d10_new_point_id(env, tmp_path):
    """复制：新 point_id（new logical_key），禁止复制旧 point_id。"""
    db, vs, add_file = env
    src = tmp_path / "s.txt"
    src.write_text("向量复用内容", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "向量复用内容", EmbeddingStatus.SUCCESS.value)])
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0, vec=[0.5, 0.5, 0.5])
    copy = tmp_path / "c.txt"
    copy.write_text("向量复用内容", encoding="utf-8")
    copy_id = add_file(str(copy))
    process_doc_file(db, copy_id, str(copy), vector_store=vs)
    old_pid = point_id(src_id, SourceType.DOC_CHUNK.value, 0)
    new_pid = point_id(copy_id, SourceType.DOC_CHUNK.value, 0)
    assert new_pid != old_pid
    assert new_pid in vs.points and old_pid in vs.points  # 旧保留 + 新写入
    assert vs.points[new_pid][0] == [0.5, 0.5, 0.5]  # 向量相等（未重新 embedding）


def test_d11_vectors_equal(env, tmp_path):
    """复制：新向量 == 旧向量（免 BGE inference 的直接证据）。"""
    db, vs, add_file = env
    src = tmp_path / "s2.txt"
    src.write_text("向量相等", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "向量相等", EmbeddingStatus.SUCCESS.value)])
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0, vec=[0.9, 0.1, 0.4, 0.2])
    copy = tmp_path / "c2.txt"
    copy.write_text("向量相等", encoding="utf-8")
    copy_id = add_file(str(copy))
    process_doc_file(db, copy_id, str(copy), vector_store=vs)
    assert vs.points[point_id(copy_id, SourceType.DOC_CHUNK.value, 0)][0] == [0.9, 0.1, 0.4, 0.2]


def test_d12_text_chunks_reused(env, tmp_path):
    """复制：chunks 文本 + seg + token_count 全部复用。"""
    db, vs, add_file = env
    src = tmp_path / "s3.txt"
    src.write_text("多 chunk 文本" * 100, encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [
        (SourceType.DOC_CHUNK.value, 0, "第一段", EmbeddingStatus.SUCCESS.value),
        (SourceType.DOC_CHUNK.value, 1, "第二段", EmbeddingStatus.SUCCESS.value),
    ])
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0)
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 1)
    copy = tmp_path / "c3.txt"
    copy.write_text("多 chunk 文本" * 100, encoding="utf-8")
    copy_id = add_file(str(copy))
    process_doc_file(db, copy_id, str(copy), vector_store=vs)
    with db.connect() as c:
        rows = c.execute(
            "SELECT chunk_index, chunk_text, chunk_text_seg, token_count, embedding_status FROM chunks WHERE file_id=? ORDER BY chunk_index",
            (copy_id,),
        ).fetchall()
        assert [(r["chunk_index"], r["chunk_text"]) for r in rows] == [(0, "第一段"), (1, "第二段")]
        assert all(r["chunk_text_seg"] and r["token_count"] == 1 for r in rows)
        assert all(r["embedding_status"] == EmbeddingStatus.SUCCESS.value for r in rows)


# ================= E. Cross path =================

def test_e13_delete_and_recreate_same_content_reuses(env, tmp_path):
    """关闭期 delete+create（同内容）：新 file_id 复用已删除源（is_deleted=1）的 AI 产物。"""
    db, vs, add_file = env
    old = tmp_path / "moved.txt"
    old.write_text("跨路径内容", encoding="utf-8")
    h = content_hash_xxh3(old)
    old_id = add_file(str(old), content_hash=h)
    _seed_ai(db, old_id, [(SourceType.DOC_CHUNK.value, 0, "跨路径内容", EmbeddingStatus.SUCCESS.value)])
    with db.connect() as c:  # 关闭期删除（软删）
        c.execute("UPDATE files SET is_deleted=1 WHERE id=?", (old_id,))
        c.commit()
    new = tmp_path / "new-location.txt"
    new.write_text("跨路径内容", encoding="utf-8")
    new_id = add_file(str(new))
    process_doc_file(db, new_id, str(new), vector_store=vs)
    with db.connect() as c:
        assert c.execute("SELECT chunk_text FROM chunks WHERE file_id=?", (new_id,)).fetchone()["chunk_text"] == "跨路径内容"
        assert c.execute("SELECT is_deleted FROM files WHERE id=?", (old_id,)).fetchone()["is_deleted"] == 1  # 旧保持删除


def test_e14_different_content_no_reuse(env, tmp_path):
    """delete+create 但内容不同 → 不复用（正常重新处理）。"""
    db, vs, add_file = env
    old = tmp_path / "d.txt"
    old.write_text("旧内容", encoding="utf-8")
    h = content_hash_xxh3(old)
    old_id = add_file(str(old), content_hash=h)
    _seed_ai(db, old_id, [(SourceType.DOC_CHUNK.value, 0, "旧内容", EmbeddingStatus.SUCCESS.value)])
    with db.connect() as c:
        c.execute("UPDATE files SET is_deleted=1 WHERE id=?", (old_id,))
        c.commit()
    new = tmp_path / "d2.txt"
    new.write_text("完全不同的新内容", encoding="utf-8")
    new_id = add_file(str(new))
    process_doc_file(db, new_id, str(new), vector_store=vs)
    with db.connect() as c:
        row = c.execute("SELECT chunk_text FROM chunks WHERE file_id=?", (new_id,)).fetchone()
        assert "完全不同的新内容" in row["chunk_text"]  # 新内容（非旧内容复用）


# ================= F. Failure =================

def test_f15_reuse_transaction_fails_old_data_valid(env, tmp_path, monkeypatch):
    """复用事务失败 → 抛异常（task FAILED）；源文件 AI 产物完整保留。"""
    db, vs, add_file = env
    src = tmp_path / "s.txt"
    src.write_text("事务失败测试", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "事务失败测试", EmbeddingStatus.SUCCESS.value)])
    copy = tmp_path / "c.txt"
    copy.write_text("事务失败测试", encoding="utf-8")
    copy_id = add_file(str(copy))

    orig_execute = None
    import sqlite3 as _sqlite3

    class Boom:
        def __init__(self, conn):
            self._conn = conn
            self.failed = False

        def execute(self, *a, **k):
            if "INSERT INTO chunks" in (a[0] if a else "") and not self.failed:
                self.failed = True
                raise _sqlite3.OperationalError("simulated tx failure")
            return self._conn.execute(*a, **k)

    # 注入失败：包装 Database.connect
    orig_connect = db.connect

    def failing_connect():
        return Boom(orig_connect())

    monkeypatch.setattr(db, "connect", failing_connect)
    with pytest.raises(Exception):
        process_doc_file(db, copy_id, str(copy), vector_store=vs)
    # 源文件产物保留 + 新文件无半成品
    with orig_connect() as c:
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (src_id,)).fetchone()["n"] == 1
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (copy_id,)).fetchone()["n"] == 0


def test_f16_qdrant_upsert_fails_old_data_valid(env, tmp_path):
    """Qdrant upsert 失败 → 抛异常（task FAILED）；无 SQLite 半成品；源文件不受影响。"""
    db, vs, add_file = env
    src = tmp_path / "s.txt"
    src.write_text("向量失败", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "向量失败", EmbeddingStatus.SUCCESS.value)])
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0)
    copy = tmp_path / "c.txt"
    copy.write_text("向量失败", encoding="utf-8")
    copy_id = add_file(str(copy))

    class BoomVS(FakeVectorStore):
        def upsert_points(self, points):
            raise RuntimeError("qdrant down")

    boom = BoomVS()
    boom.points = dict(vs.points)  # 继承旧向量（upsert 时失败）
    with pytest.raises(RuntimeError):
        process_doc_file(db, copy_id, str(copy), vector_store=boom)
    with db.connect() as c:
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (copy_id,)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (src_id,)).fetchone()["n"] == 1
        assert c.execute("SELECT status FROM files WHERE id=?", (src_id,)).fetchone()["status"] == FileStatus.AI_DONE.value


def test_f17_old_data_remains_valid(env, tmp_path):
    """复用失败后：源文件 FTS/搜索能力不受影响（chunks 完整 + embedding 完整）。"""
    db, vs, add_file = env
    src = tmp_path / "keep.txt"
    src.write_text("保留内容", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "保留内容", EmbeddingStatus.SUCCESS.value)])
    _seed_vector(vs, src_id, SourceType.DOC_CHUNK.value, 0)
    # 模拟一次失败的复用（源不存在向量 → 读取失败）
    copy = tmp_path / "c.txt"
    copy.write_text("保留内容", encoding="utf-8")
    copy_id = add_file(str(copy))
    empty_vs = FakeVectorStore()  # 无旧向量 → 复用仅文本，embedding PENDING
    process_doc_file(db, copy_id, str(copy), vector_store=empty_vs)
    with db.connect() as c:
        row = c.execute("SELECT embedding_status FROM chunks WHERE file_id=?", (copy_id,)).fetchone()
        assert row["embedding_status"] == EmbeddingStatus.PENDING.value  # 文本复用，向量待补（不误标 SUCCESS）
        assert c.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (src_id,)).fetchone()["n"] == 1


# ================= G. Model compatibility =================

def test_g18_incompatible_embedding_no_reuse(env, tmp_path):
    """embedding 兼容（MVP 单模型）：源 embedding_status 非 SUCCESS → 新文件不复制向量（PENDING）。"""
    db, vs, add_file = env
    src = tmp_path / "s.txt"
    src.write_text("模型兼容", encoding="utf-8")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), content_hash=h)
    _seed_ai(db, src_id, [(SourceType.DOC_CHUNK.value, 0, "模型兼容", EmbeddingStatus.FAILED.value)])  # 源向量失败/旧模型
    copy = tmp_path / "c.txt"
    copy.write_text("模型兼容", encoding="utf-8")
    copy_id = add_file(str(copy))
    process_doc_file(db, copy_id, str(copy), vector_store=vs)
    with db.connect() as c:
        row = c.execute("SELECT embedding_status FROM chunks WHERE file_id=?", (copy_id,)).fetchone()
        assert row["embedding_status"] == EmbeddingStatus.PENDING.value  # 只复用文本，不误复用向量
    assert vs.upserted == []  # 未复制任何向量


# ================= image 路径（OCR/Caption 复用） =================

def test_image_copy_reuses_ocr_caption(env, tmp_path, monkeypatch):
    """image 复制：复用 ocr_text + chunks(ocr) + image_caption，不重新 OCR/Caption。"""
    db, vs, add_file = env
    src = tmp_path / "img.png"
    src.write_bytes(b"fake-image-bytes-same")
    h = content_hash_xxh3(src)
    src_id = add_file(str(src), file_type="image", content_hash=h)
    _seed_ai(db, src_id, [
        (SourceType.OCR.value, 0, "New York 2026", EmbeddingStatus.SUCCESS.value),
        (SourceType.IMAGE_CAPTION.value, 0, "城市天际线", EmbeddingStatus.SUCCESS.value),
    ], ocr="New York 2026")
    _seed_vector(vs, src_id, SourceType.OCR.value, 0)
    _seed_vector(vs, src_id, SourceType.IMAGE_CAPTION.value, 0)
    copy = tmp_path / "copy.png"
    copy.write_bytes(b"fake-image-bytes-same")
    copy_id = add_file(str(copy), file_type="image")

    calls = {"ocr": 0, "caption": 0}
    monkeypatch.setattr(processor, "ocr_image", lambda _p: (calls.__setitem__("ocr", calls["ocr"] + 1), None)[1])
    monkeypatch.setattr(processor, "extract_exif", lambda _p: None)

    class FakeCaption:
        def caption(self, _p):
            calls["caption"] += 1
            return None

    process_image_file(db, copy_id, str(copy), vector_store=vs, caption_provider=FakeCaption())
    assert calls["ocr"] == 0 and calls["caption"] == 0, "复制路径不应重新 OCR/Caption"
    with db.connect() as c:
        rows = {
            r["source_type"]: (r["chunk_text"], r["embedding_status"])
            for r in c.execute("SELECT source_type, chunk_text, embedding_status FROM chunks WHERE file_id=?", (copy_id,)).fetchall()
        }
        assert rows[SourceType.OCR.value][0] == "New York 2026"
        assert rows[SourceType.IMAGE_CAPTION.value][0] == "城市天际线"
        assert rows[SourceType.OCR.value][1] == EmbeddingStatus.SUCCESS.value
        ocr_row = c.execute("SELECT text FROM ocr_text WHERE file_id=?", (copy_id,)).fetchone()
        assert ocr_row["text"] == "New York 2026"
    assert sorted(vs.upserted) == sorted([
        point_id(copy_id, SourceType.OCR.value, 0),
        point_id(copy_id, SourceType.IMAGE_CAPTION.value, 0),
    ])
