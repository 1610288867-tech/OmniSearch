"""图片语义链路测试（M4.4：image_caption → BGE → Qdrant；不进 FTS）。

真实 Chinese-CLIP 标签 provider + BGE + Qdrant。
关键验证：image_caption 永不进入 FTS 关键词通道（fts_chunks_source VIEW 语义不变）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from omnisearch.common.models import SourceType
from omnisearch.common.utils.seg import fts_query_for
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.service.semantic_search import SemanticSearchService
from omnisearch.worker.pipeline.processor import process_image_file

_FONT = "C:/Windows/Fonts/msyh.ttc"


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def caption_provider():
    """Chinese-CLIP 标签 provider（dev-data/models/chinese-clip）。"""
    from omnisearch.common.utils.models import models_dir
    from omnisearch.worker.providers.caption import LocalImageCaptionProvider

    md = models_dir(REPO_ROOT / "dev-data") / "chinese-clip"
    if not (md / "vision_model.onnx").exists():
        pytest.skip("chinese-clip ONNX 未导出（需先运行导出脚本）")
    prov = LocalImageCaptionProvider(md)
    prov._ensure_loaded()
    return prov


def _make_image(path: Path, label_text: str) -> None:
    img = Image.new("RGB", (480, 320), (180, 200, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 50, 420, 290], outline=(40, 40, 60), width=4)
    try:
        font = ImageFont.truetype(_FONT, 22)
    except OSError:
        font = ImageFont.load_default()
    d.text((120, 150), label_text, fill=(20, 20, 20), font=font)
    img.save(str(path))


def _insert_image(db, path: str) -> int:
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, ?, ?, '.jpg', 1, 1, 'image')""",
            (path, Path(path).name, str(Path(path).parent)),
        )
        return cur.lastrowid


def test_image_caption_never_in_fts(db, qdrant_server, bge, caption_provider, tmp_path):
    """image_caption 只进 Vector；FTS 关键词通道与 rebuild 均不可见（architecture.md §8.1）。"""
    img = tmp_path / "cap.jpg"
    _make_image(img, "自由女神像")
    fid = _insert_image(db, str(img))
    from omnisearch.common.vector import VectorStore

    vs = VectorStore(qdrant_server, bge.dim)
    vs.ensure_collection()
    process_image_file(db, fid, str(img), bge, vs, caption_provider)

    conn = db.connect()
    rows = conn.execute(
        "SELECT source_type, chunk_text FROM chunks WHERE file_id=?", (fid,)
    ).fetchall()
    types = {r["source_type"] for r in rows}
    assert SourceType.IMAGE_CAPTION.value in types
    caption_text = next(r["chunk_text"] for r in rows if r["source_type"] == SourceType.IMAGE_CAPTION.value)
    conn.close()

    # FTS 关键词通道：caption 标签词不可见（触发器 WHEN 排除）
    probe = caption_text.split("，")[0]  # 取第一个标签词
    assert FtsRepository(db).body_match(fts_query_for(probe)) == []
    # FTS rebuild 后仍不可见（VIEW 语义不变）
    with db.connect() as c:
        c.execute("INSERT INTO fts_body(fts_body) VALUES('rebuild')")
        c.commit()
    assert FtsRepository(db).body_match(fts_query_for(probe)) == []
    # 清理（session 级 Qdrant 共享）
    vs._client.delete_collection("omnisearch")


def test_image_semantic_search(db, qdrant_server, bge, caption_provider, tmp_path):
    """图片语义检索：标签文本 → BGE → Qdrant → 语义 query 召回。"""
    img = tmp_path / "cap2.jpg"
    _make_image(img, "城市天际线")
    fid = _insert_image(db, str(img))
    from omnisearch.common.vector import VectorStore

    vs = VectorStore(qdrant_server, bge.dim)
    vs.ensure_collection()
    process_image_file(db, fid, str(img), bge, vs, caption_provider)

    svc = SemanticSearchService(db, bge, vs)
    results = svc.search("高楼大厦的天际线", top_k=5)
    # 图片经 OCR（含文字）+ Caption 双通道进入向量空间；该图片应被语义召回
    # （OCR 文本可能比标签更贴近 query，source_type 不限定）
    assert results and any(r["file_id"] == fid for r in results)
    # 图片语义通道确实存在（image_caption chunk 已向量化）
    with db.connect() as c:
        row = c.execute(
            "SELECT embedding_status FROM chunks WHERE file_id=? AND source_type=?",
            (fid, SourceType.IMAGE_CAPTION.value),
        ).fetchone()
    assert row and row["embedding_status"] == 1
    vs._client.delete_collection("omnisearch")  # 清理（session 级 Qdrant 共享）
