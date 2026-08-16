"""域 B/D/E/F/G/H：Hybrid Search（M5 §18）—— Filter / Vector / Hybrid / RRF / Degradation / Match Reasons / Time。

使用真实 BGE + Qdrant（conftest qdrant_server/bge）验证语义通道；降级用注入故障对象。
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import pytest

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.common.models import FileType, SourceType
from omnisearch.common.utils.point_id import point_id
from omnisearch.common.utils.seg import seg_text
from omnisearch.common.utils.time import day_start, now_local
from omnisearch.common.vector import VectorPoint, VectorStore
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.service.filter_builder import FilterBuilderService
from omnisearch.server.service.query_parser import QueryParser
from omnisearch.server.service.search import SearchError, SearchService
from omnisearch.server.service.semantic_search import SemanticSearchService
from omnisearch.server.service.time_range import TimeRangeService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _link_models(tmp_path: Path) -> None:
    """把 dev-data/models junction 到 tmp_path/models（Windows mklink /J）。"""
    import subprocess

    src = REPO_ROOT / "dev-data" / "models"
    dest = tmp_path / "models"
    if not src.exists():
        return
    subprocess.run(["cmd", "/c", "mklink", "/J", str(dest), str(src)], check=False, capture_output=True)


def _days_ago_epoch(days: int) -> int:
    """本地当日零点往前 days 天的零点 epoch（测试与实现同用本地时区）。"""
    return int(day_start(now_local()).timestamp()) - days * 86400


def _seed_file(
    db: Database,
    files: FileRepository,
    fts: FtsRepository,
    path: str,
    body: str | None = None,
    ocr: str | None = None,
    caption: str | None = None,
    mtime_days_ago: int = 0,
    ctime_days_ago: int | None = None,
    exif_epoch: int | None = None,
    file_type: FileType = FileType.DOC,
) -> int:
    mtime = _days_ago_epoch(mtime_days_ago)
    ctime = _days_ago_epoch(ctime_days_ago if ctime_days_ago is not None else mtime_days_ago)
    ops = files.upsert_batch(
        [
            FileMeta(
                path=path, filename=Path(path).name, dir_path=str(Path(path).parent),
                extension=Path(path).suffix.lower(), size_bytes=10,
                mtime_ns=mtime * 10**9, ctime_ns=ctime * 10**9,
                file_type=file_type, mime_type=None,
            )
        ]
    )
    fid = ops[0].file_id
    fts.insert(fid, ops[0].filename, ops[0].filename_seg, ops[0].dir_tokens)
    with db.connect() as c:
        for src, text in ((SourceType.DOC_CHUNK.value, body), (SourceType.OCR.value, ocr),
                          (SourceType.IMAGE_CAPTION.value, caption)):
            if text:
                c.execute(
                    """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
                       VALUES (?, ?, 0, ?, ?)""",
                    (fid, src, text, seg_text(text)),
                )
        if exif_epoch is not None:
            c.execute(
                """INSERT INTO exif (file_id, datetime_original, datetime_original_epoch)
                   VALUES (?, ?, ?)""",
                (fid, "2026:08:15 10:00:00", exif_epoch),
            )
        c.execute("UPDATE files SET status='AI_DONE' WHERE id=?", (fid,))
        c.commit()
    return fid


def _seed_vector(vs: VectorStore, bge, fid: int, source_type: str, text: str) -> None:
    """写入 Qdrant 点（chunk 三元组必须已存在——由 _seed_file 的 body/ocr/caption 保证）。"""
    vec = bge.embed_texts([text])[0]
    vs.upsert_points([VectorPoint(point_id(fid, source_type, 0), vec, fid, source_type, 0, text)])


def _make_svc(db: Database, semantic=None, fts=None, weights=None) -> SearchService:
    return SearchService(
        db, FileRepository(db), fts or FtsRepository(db),
        QueryParser(TimeRangeService()), FilterBuilderService(), semantic, weights,
    )


@pytest.fixture()
def hybrid(tmp_path, qdrant_server, bge):
    """真实语义通道可用的服务环境（session 级 Qdrant：跨测试防污染靠三元组校验）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    os.environ["OMNISEARCH_QDRANT_HTTP_PORT"] = qdrant_server.rsplit(":", 1)[-1]
    _link_models(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    vs = VectorStore(qdrant_server, bge.dim)
    vs.ensure_collection()
    semantic = SemanticSearchService(db, bge, vs)
    svc = _make_svc(db, semantic=semantic)
    yield db, vs, svc
    # 清理本测试产生的 points（按本 db 的 file_id；collection 为 session 级共享）
    with db.connect() as c:
        fids = [r["id"] for r in c.execute("SELECT id FROM files").fetchall()]
    for fid in fids:
        vs.delete_points(vs.list_keys_by_file(fid))
    os.environ.pop("OMNISEARCH_DEV_DATA", None)
    os.environ.pop("OMNISEARCH_QDRANT_HTTP_PORT", None)


@pytest.fixture()
def plain(tmp_path):
    """无语义通道的服务环境（keyword-only 降级场景）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    yield db, _make_svc(db)
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


# ================= 域 B：Filter =================

def test_time_exact_exif(hybrid):
    """EXIF exact（hard filter）：exif 在范围内 → 命中；exif 不在范围但 mtime 在 → 排除（§12.7）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/photo-a.jpg", caption="自由女神像",
                   exif_epoch=_days_ago_epoch(1), file_type=FileType.IMAGE)
    b = _seed_file(db, files, fts, "/x/photo-b.jpg", caption="自由女神像",
                   exif_epoch=_days_ago_epoch(10), mtime_days_ago=1, file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, a, "image_caption", "自由女神像")
    _seed_vector(vs, svc._semantic._embedder, b, "image_caption", "自由女神像")
    out = svc.search("昨天拍的自由女神照片")
    ids = {r["file_id"] for r in out.results}
    assert a in ids and b not in ids  # b 的 EXIF 不在范围 → 即使 mtime 昨天也排除


def test_time_fallback_mtime(hybrid):
    """无 EXIF → mtime fallback 参与过滤，结果标注 fallback（§12.7）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/note-a.txt", body="机器学习", mtime_days_ago=1)
    _seed_file(db, files, fts, "/x/note-b.txt", body="机器学习", mtime_days_ago=30)
    out = svc.search("昨天机器学习")
    assert [r["file_id"] for r in out.results] == [a]
    assert out.results[0]["time_info"]["basis"] == "mtime"
    assert out.results[0]["time_info"]["confidence"] == "fallback"


def test_time_ctime_hint(hybrid):
    """query 含「创建」→ ctime 过滤（§12.7）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/created-a.txt", body="机器学习", ctime_days_ago=1, mtime_days_ago=30)
    _seed_file(db, files, fts, "/x/created-b.txt", body="机器学习", ctime_days_ago=30, mtime_days_ago=1)
    out = svc.search("昨天创建的机器学习")
    assert [r["file_id"] for r in out.results] == [a]
    assert out.results[0]["time_info"]["basis"] == "ctime"


def test_file_type_filter(hybrid):
    """file_types=[image]：doc 排除（§12.2 canonical）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/img.jpg", body="机器学习", file_type=FileType.IMAGE)
    _seed_file(db, files, fts, "/x/doc.txt", body="机器学习")
    out = svc.search("机器学习图片")
    assert out.parsed.file_types == ["image"]
    assert out.results and all(r["file_type"] == "image" for r in out.results)


def test_extension_filter(hybrid):
    """extensions=[pdf]：非 pdf 排除。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/a.pdf", body="机器学习")
    _seed_file(db, files, fts, "/x/b.txt", body="机器学习")
    out = svc.search("机器学习pdf")
    assert out.parsed.extensions == ["pdf"]
    assert [r["file_id"] for r in out.results] == [a]


def test_deleted_excluded(hybrid):
    """is_deleted=1 → keyword + semantic 双通道均排除（canonical，§12.2/§12.3 三处一致）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/gone.txt", body="机器学习")
    _seed_vector(vs, svc._semantic._embedder, fid, "doc_chunk", "机器学习")
    with db.connect() as c:
        c.execute("UPDATE files SET is_deleted=1 WHERE id=?", (fid,))
        c.commit()
    assert fid not in {r["file_id"] for r in svc.search("机器学习").results}
    assert fid not in {r["file_id"] for r in svc.search("机器学习", mode="semantic").results}


# ================= 域 D：Vector（语义通道正确性） =================

def test_semantic_only_mode(hybrid):
    """mode=semantic：仅向量通道，keyword_score=null（§12.5）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/img.jpg", caption="纽约城市风景照片", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "纽约城市风景照片")
    out = svc.search("纽约城市夜景", mode="semantic")
    assert out.results and out.results[0]["file_id"] == fid
    assert out.results[0]["keyword_score"] is None
    assert out.results[0]["semantic_score"] is not None
    assert any(r["channel"] == "semantic" for r in out.results[0]["match_reasons"])


def test_stale_point_excluded(hybrid):
    """stale point：chunks 已删但 Qdrant 点残留 → 排除（三元组校验，§12.3）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/stale.jpg", caption="自由女神像", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "自由女神像")
    with db.connect() as c:  # 模拟 reindex 后旧 caption chunk 已删除（异步清理未完成）
        c.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
        c.commit()
    assert fid not in {r["file_id"] for r in svc.search("自由女神", mode="semantic").results}


def test_orphan_point_excluded(hybrid):
    """orphan point：file_id 在 files 中不存在 → 排除。"""
    _vs, svc = hybrid[1], hybrid[2]
    _seed_vector(_vs, svc._semantic._embedder, 99999, "image_caption", "自由女神像")  # 无对应 files 行
    try:
        assert 99999 not in {r["file_id"] for r in svc.search("自由女神", mode="semantic").results}
    finally:
        # 清理（该 fid 不在本 db，fixture 不会清理；否则污染 session 级共享 collection）
        _vs.delete_points(_vs.list_keys_by_file(99999))


def test_missing_chunk_excluded(hybrid):
    """point 三元组在 chunks 中不存在（chunk 已删/从未写入）→ 排除。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/miss.jpg", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "ocr", "New York 2026")  # 未写 chunks(ocr) 行
    assert fid not in {r["file_id"] for r in svc.search("New York", mode="semantic").results}


def test_deleted_file_semantic_excluded(hybrid):
    """deleted file：语义候选回表 canonical 排除（is_deleted=0，§12.3 第三处一致）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/del.jpg", caption="纽约城市风景照片", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "纽约城市风景照片")
    with db.connect() as c:
        c.execute("UPDATE files SET is_deleted=1 WHERE id=?", (fid,))
        c.commit()
    assert fid not in {r["file_id"] for r in svc.search("纽约城市夜景", mode="semantic").results}


# ================= 域 E：Hybrid / RRF =================

def test_hybrid_both_channels(hybrid):
    """双通道命中同文件：rrf_score 含两项，keyword_score + semantic_score 均有值（§12.4/§12.5）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/机器学习架构.pdf", body="机器学习架构与系统设计")
    _seed_vector(vs, svc._semantic._embedder, fid, "doc_chunk", "机器学习架构与系统设计")
    out = svc.search("机器学习架构")
    hit = next(r for r in out.results if r["file_id"] == fid)
    assert hit["keyword_score"] is not None and hit["semantic_score"] is not None
    assert hit["rrf_score"] > 1 / 61  # 双通道两项求和 > 单通道一项


def test_hybrid_fts_only(hybrid):
    """仅 FTS 命中：semantic_score=null，degraded=[]（正常单通道，非降级）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/unique-token-xyz.pdf", body="机器学习")
    out = svc.search("unique-token-xyz")
    assert out.results and out.results[0]["file_id"] == fid
    assert out.results[0]["semantic_score"] is None
    assert out.degraded == []


def test_hybrid_vector_only(hybrid):
    """仅向量命中：keyword_score=null（文件名/正文无关键词候选）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/IMG_0001.jpg", caption="纽约城市夜景", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "纽约城市夜景")
    out = svc.search("纽约城市风景")
    assert out.results and out.results[0]["file_id"] == fid
    assert out.results[0]["keyword_score"] is None


def test_duplicate_file_dedup(hybrid):
    """同文件多 chunk：file_id 去重，取最高 semantic_score（§12.3）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/multi.txt", body="纽约城市风景", ocr="纽约城市风景照片")
    _seed_vector(vs, svc._semantic._embedder, fid, "doc_chunk", "纽约城市风景")
    _seed_vector(vs, svc._semantic._embedder, fid, "ocr", "纽约城市风景照片")
    out = svc.search("纽约城市风景", mode="semantic")
    hits = [r for r in out.results if r["file_id"] == fid]
    assert len(hits) == 1  # 去重


def test_rrf_ordering(hybrid):
    """RRF：双通道命中排在单通道之前（k=60 默认权重）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/机器学习架构.pdf", body="机器学习架构与系统设计")
    _seed_vector(vs, svc._semantic._embedder, a, "doc_chunk", "机器学习架构与系统设计")
    b = _seed_file(db, files, fts, "/x/zzz.pdf", caption="机器学习架构与系统设计")
    _seed_vector(vs, svc._semantic._embedder, b, "image_caption", "机器学习架构与系统设计")
    out = svc.search("机器学习架构")
    assert out.results[0]["file_id"] == a  # a 双通道；b 仅语义


def test_rrf_weights(hybrid):
    """权重（§12.4 Settings 可调）：w_kw 大 → 关键词单通道文件排名上升。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    k = _seed_file(db, files, fts, "/x/kw-only-word.pdf", body="机器学习")
    s = _seed_file(db, files, fts, "/x/sem.jpg", caption="机器学习", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, s, "image_caption", "机器学习")
    svc2 = _make_svc(db, semantic=svc._semantic, weights=lambda: (10.0, 1.0))
    out = svc2.search("机器学习")
    assert out.results[0]["file_id"] == k  # w_kw=10 主导


# ================= 域 F：Degradation（§12.8） =================

def test_degraded_semantic_unavailable(plain):
    """语义通道未配置（BGE/Qdrant 缺失）→ degraded=["semantic"]，关键词正常。"""
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/a.txt", body="机器学习")
    out = svc.search("机器学习")
    assert out.results and out.results[0]["filename"] == "a.txt"
    assert out.degraded == ["semantic"]


def test_degraded_qdrant_down(tmp_path, bge):
    """Qdrant 不可用 → 跳过语义通道，关键词结果返回（§12.8）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/b.txt", body="机器学习")
    dead = VectorStore("http://127.0.0.1:1", bge.dim)  # 无监听端口 → 连接拒绝
    svc = _make_svc(db, semantic=SemanticSearchService(db, bge, dead))
    out = svc.search("机器学习")
    assert out.results and out.results[0]["filename"] == "b.txt"
    assert out.degraded == ["semantic"]
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_degraded_bge_fail(tmp_path):
    """BGE embedding 失败 → 跳过语义通道（§12.8）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/c.txt", body="机器学习")

    class BoomEmbedder:
        dim = 512

        def embed_query(self, _q):
            raise RuntimeError("bge failed")

    svc = _make_svc(db, semantic=SemanticSearchService(db, BoomEmbedder(), None))  # type: ignore[arg-type]
    out = svc.search("机器学习")
    assert out.results and out.results[0]["filename"] == "c.txt"
    assert out.degraded == ["semantic"]
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def _fake_semantic_hit(db: Database, fid: int, source_type: str) -> SemanticSearchService:
    """构造「点命中 + chunk 三元组存在」的假语义通道（chunk 行由调用方 seed）。

    走真实 SemanticSearchService 的校验路径（三元组 + canonical），仅替换 embedding/vector。
    """

    class FakeEmbedder:
        dim = 512

        def embed_query(self, _q):
            return [0.0] * 512

    class FakeVector:
        def search(self, _v, top_k=100):  # noqa: ARG002
            return [({"file_id": fid, "source_type": source_type, "chunk_index": 0, "text": "机器学习"}, 0.9)]

    return SemanticSearchService(db, FakeEmbedder(), FakeVector())  # type: ignore[arg-type]


def test_degraded_fts_fail(tmp_path):
    """FTS5 异常 → 跳过关键词通道，语义结果返回（§12.8）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/d.jpg", ocr="机器学习", file_type=FileType.IMAGE)

    class BoomFts:
        def match(self, query, top_k=100):  # noqa: ARG002
            raise RuntimeError("fts corrupted")

        def body_match(self, query, top_k=100):  # noqa: ARG002
            raise RuntimeError("fts corrupted")

    svc = _make_svc(db, semantic=_fake_semantic_hit(db, fid, "ocr"), fts=BoomFts())
    out = svc.search("机器学习")
    assert out.results and out.results[0]["file_id"] == fid
    assert out.degraded == ["keyword"]
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_degraded_fts_timeout(tmp_path):
    """FTS 超时（1s）→ degraded=["keyword"]，语义通道继续（§12.3）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/e.jpg", ocr="机器学习", file_type=FileType.IMAGE)

    class SlowFts:
        def match(self, query, top_k=100):  # noqa: ARG002
            time.sleep(2.0)  # > FTS 1s 超时
            return []

        def body_match(self, query, top_k=100):  # noqa: ARG002
            return []

    svc = _make_svc(db, semantic=_fake_semantic_hit(db, fid, "ocr"), fts=SlowFts())
    out = svc.search("机器学习")
    assert out.degraded == ["keyword"]
    assert out.results[0]["file_id"] == fid  # 语义通道未受影响
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_both_channels_failed(tmp_path):
    """两通道均失败 → 明确错误（SQLite 可用时不得静默返回空，§12.8）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)

    class BoomEmbedder:
        dim = 512

        def embed_query(self, _q):
            raise RuntimeError("bge failed")

    class BoomFts:
        def match(self, query, top_k=100):  # noqa: ARG002
            raise RuntimeError("fts corrupted")

        def body_match(self, query, top_k=100):  # noqa: ARG002
            raise RuntimeError("fts corrupted")

    svc = _make_svc(db, semantic=SemanticSearchService(db, BoomEmbedder(), None), fts=BoomFts())  # type: ignore[arg-type]
    with pytest.raises(SearchError) as e:
        svc.search("机器学习")
    assert "BOTH_CHANNELS_FAILED" in str(e.value)
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_sqlite_fail(tmp_path):
    """SQLite 不可用 → 整体失败（sqlite3.Error 上抛，不静默返回空，§12.8）。"""
    bad_db = Database(tmp_path)  # 路径是目录 → connect 必然失败
    svc = _make_svc(bad_db)
    with pytest.raises(sqlite3.Error):
        svc.search("机器学习")


def test_empty_semantic_text(hybrid):
    """semantic_text 为空（纯过滤查询）→ 只走 FTS，degraded=[]（§12.8）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/f.txt", body="机器学习")
    out = svc.search("昨天")
    assert out.parsed.semantic_text == ""
    assert out.results == []  # FTS 无关键词候选
    assert out.degraded == []


# ================= 域 G：Match Reasons（§12.6） =================

def test_match_reasons_all_channels(hybrid):
    """单条结果同时携带 keyword/body/ocr/semantic/metadata 原因。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(
        db, files, fts, "/x/机器学习照片.jpg",
        body="机器学习是人工智能的重要分支", ocr="机器学习相关会议记录",
        caption="机器学习相关文档", mtime_days_ago=1, file_type=FileType.IMAGE,
    )
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "机器学习相关文档")
    out = svc.search("昨天的机器学习")
    hit = next(r for r in out.results if r["file_id"] == fid)
    channels = {r["channel"] for r in hit["match_reasons"]}
    assert channels == {"keyword", "body", "ocr", "semantic", "metadata"}
    meta = next(r for r in hit["match_reasons"] if r["channel"] == "metadata")
    assert meta["basis"] == "mtime" and meta["confidence"] == "fallback"


def test_match_reasons_metadata_exact(hybrid):
    """EXIF 时间原因：basis=exif / confidence=exact（§12.7）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/g.jpg", caption="纽约城市风景照片",
                     exif_epoch=_days_ago_epoch(1), file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "纽约城市风景照片")
    out = svc.search("昨天拍的纽约照片")
    hit = out.results[0]
    meta = next(r for r in hit["match_reasons"] if r["channel"] == "metadata")
    assert meta["basis"] == "exif" and meta["confidence"] == "exact"
    assert hit["time_info"]["value"] == meta["text"].split("拍摄于 ")[1]


def test_match_reasons_semantic_caption_prefix(hybrid):
    """image_caption 语义原因前缀「AI 描述：」（§12.6 示例）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/h.jpg", caption="纽约城市风景照片", file_type=FileType.IMAGE)
    _seed_vector(vs, svc._semantic._embedder, fid, "image_caption", "纽约城市风景照片")
    out = svc.search("纽约城市夜景", mode="semantic")
    reason = next(r for r in out.results[0]["match_reasons"] if r["channel"] == "semantic")
    assert reason["text"].startswith("AI 描述：")
    assert reason["score"] is not None


# ================= 域 H：Time =================

def test_time_info_rfc3339(hybrid):
    """time_info.value 为 offset-aware RFC3339（禁止 timezone-naive，§8）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/i.txt", body="机器学习", mtime_days_ago=1)
    out = svc.search("昨天机器学习")
    value = out.results[0]["time_info"]["value"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", value)


def test_no_time_query_no_time_info(hybrid):
    """无时间表达 → time_info 三字段全 None（不做时间过滤）。"""
    db, _vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/j.txt", body="机器学习")
    out = svc.search("机器学习")
    assert out.results[0]["time_info"] == {"basis": None, "confidence": None, "value": None}


# ================= mode 语义 =================

def test_mode_keyword(hybrid):
    """mode=keyword：仅关键词通道，无语义降级标注（用户选择，非降级）。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/k.txt", body="机器学习")
    _seed_vector(vs, svc._semantic._embedder, fid, "doc_chunk", "机器学习")
    out = svc.search("机器学习", mode="keyword")
    assert out.results and out.results[0]["semantic_score"] is None
    assert out.degraded == []


def test_mode_semantic(hybrid):
    """mode=semantic：仅向量通道。"""
    db, vs, svc = hybrid
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/m.txt", body="机器学习")
    _seed_vector(vs, svc._semantic._embedder, fid, "doc_chunk", "机器学习")
    out = svc.search("机器学习", mode="semantic")
    assert out.results and out.results[0]["file_id"] == fid
    assert out.results[0]["keyword_score"] is None
    assert out.degraded == []
