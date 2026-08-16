"""MVP 最终收口测试（M5 收口 1-5，最终验收矩阵 A-F）。

A Timeout：vector/fts/both 超时 + 超时后后续请求正常（真时限断言）
B QueryParser：类型词误判回归
C Metadata-only：纯过滤查询返回文件
D Health：semantic_ready true/false + semantic 降级
E FTS deadline：phrase/AND/OR 整体不突破 FTS 1s
F Cleanup：由 verify-omnisearch Skill 覆盖（步骤 0 前置清理）
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.common.models import FileType
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.service.filter_builder import FilterBuilderService
from omnisearch.server.service.query_parser import QueryParser
from omnisearch.server.service.search import SearchError, SearchService
from omnisearch.server.service.semantic_search import SemanticSearchService
from omnisearch.server.service.time_range import TimeRangeService
from omnisearch.common.utils.time import day_start, now_local


def _days_ago_epoch(days: int) -> int:
    return int(day_start(now_local()).timestamp()) - days * 86400


def _seed_file(
    db: Database, files: FileRepository, fts: FtsRepository,
    path: str, body: str | None = None, file_type: FileType = FileType.DOC,
    mtime_days_ago: int = 0, ctime_days_ago: int | None = None,
) -> int:
    from omnisearch.common.utils.seg import seg_text

    mtime = _days_ago_epoch(mtime_days_ago)
    ctime = _days_ago_epoch(ctime_days_ago if ctime_days_ago is not None else mtime_days_ago)
    ops = files.upsert_batch(
        [FileMeta(path=path, filename=Path(path).name, dir_path=str(Path(path).parent),
                  extension=Path(path).suffix.lower(), size_bytes=10,
                  mtime_ns=mtime * 10**9, ctime_ns=ctime * 10**9, file_type=file_type, mime_type=None)]
    )
    fid = ops[0].file_id
    fts.insert(fid, ops[0].filename, ops[0].filename_seg, ops[0].dir_tokens)
    if body:
        with db.connect() as c:
            c.execute(
                """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
                   VALUES (?, 'doc_chunk', 0, ?, ?)""",
                (fid, body, seg_text(body)),
            )
            c.commit()
    return fid


def _make_svc(db: Database, semantic=None, fts=None) -> SearchService:
    return SearchService(
        db, FileRepository(db), fts or FtsRepository(db),
        QueryParser(TimeRangeService()), FilterBuilderService(), semantic,
    )


@pytest.fixture()
def plain(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    yield db, _make_svc(db)
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


# ================= A：Timeout 注入（真实时限断言） =================

class SlowVector:
    """VectorStore 兼容替身：search 阻塞指定时长。"""

    def __init__(self, sleep_s: float, hit: dict | None = None):
        self._sleep = sleep_s
        self._hit = hit or {"file_id": 1, "source_type": "doc_chunk", "chunk_index": 0, "text": "机器学习"}

    def search(self, _vector, top_k=100):  # noqa: ARG002
        time.sleep(self._sleep)
        return [(self._hit, 0.9)]


class SlowFts:
    """FtsRepository 兼容替身：每次 MATCH 阻塞指定时长，返回空候选。"""

    def __init__(self, sleep_s: float):
        self._sleep = sleep_s

    def match(self, query, top_k=100):  # noqa: ARG002
        time.sleep(self._sleep)
        return []

    def body_match(self, query, top_k=100):  # noqa: ARG002
        time.sleep(self._sleep)
        return []


class OkEmbedder:
    dim = 512

    def embed_query(self, _q):
        return [0.0] * 512


def _env(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    db = Database(db_path(tmp_path))
    migrate(db)
    return db


@pytest.fixture(autouse=True)
def _warm_jieba():
    """预热 jieba 词典（parser 首次调用加载 ~0.4s，避免计入 timeout 断言）。"""
    from omnisearch.common.utils.seg import seg_text

    seg_text("预热")
    yield


def test_timeout_vector_returns_within_deadline(tmp_path):
    """Vector 阻塞 > 3s：请求约 3s 内返回（不等待完成），FTS 结果正常，degraded=["semantic"]。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/a.txt", body="机器学习")
    svc = _make_svc(db, semantic=SemanticSearchService(db, OkEmbedder(), SlowVector(5.0)))
    started = time.perf_counter()
    out = svc.search("机器学习")
    elapsed = time.perf_counter() - started
    assert elapsed < 4.0, f"vector timeout 未生效：请求阻塞 {elapsed:.1f}s"
    assert out.degraded == ["semantic"]
    assert out.results and out.results[0]["file_id"] == fid
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_timeout_fts_returns_within_deadline(tmp_path):
    """FTS 阻塞 > 1s：请求约 1s 内返回，Vector 结果正常，degraded=["keyword"]。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/b.txt", body="机器学习")
    fake_sem = SemanticSearchService(db, OkEmbedder(), SlowVector(0.01, {"file_id": fid, "source_type": "doc_chunk", "chunk_index": 0, "text": "机器学习"}))
    svc = _make_svc(db, semantic=fake_sem, fts=SlowFts(2.0))
    started = time.perf_counter()
    out = svc.search("机器学习")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"fts timeout 未生效：请求阻塞 {elapsed:.1f}s"
    assert out.degraded == ["keyword"]
    assert out.results and out.results[0]["file_id"] == fid  # 语义通道未受影响
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_timeout_both_raises_within_deadline(tmp_path):
    """双通道同时阻塞：约 3s 内明确 BOTH_CHANNELS_FAILED（SQLite 可用）。"""
    db = _env(tmp_path)
    svc = _make_svc(db, semantic=SemanticSearchService(db, OkEmbedder(), SlowVector(5.0)), fts=SlowFts(2.0))
    started = time.perf_counter()
    with pytest.raises(SearchError) as e:
        svc.search("机器学习")
    elapsed = time.perf_counter() - started
    assert "BOTH_CHANNELS_FAILED" in str(e.value)
    # 期望墙钟 ≈ kw 超时(1s) + vector 超时(3s) = 4s（顺次等待两个 future 各自 deadline）
    assert elapsed < 4.5, f"both timeout 未生效：请求阻塞 {elapsed:.1f}s"
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_timeout_then_subsequent_requests_ok(tmp_path):
    """超时后后续请求必须可用（executor 不泄漏、无未处理异常）。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/c.txt", body="机器学习")
    svc = _make_svc(db, semantic=SemanticSearchService(db, OkEmbedder(), SlowVector(5.0)), fts=SlowFts(2.0))
    for _ in range(2):
        with pytest.raises(SearchError):
            svc.search("机器学习")  # 双通道超时
    # 恢复正常通道（注入慢件 → 换回真件）后仍可用
    svc_ok = _make_svc(db, fts=FtsRepository(db))
    out = svc_ok.search("机器学习")
    assert out.results and out.results[0]["file_id"] == fid
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


# ================= E：FTS phrase/AND/OR 整体 deadline =================

def test_fts_forms_combined_within_deadline(tmp_path):
    """phrase+AND+OR 三次 MATCH 各自 0.4s（合计 1.2s）：请求仍约 1s 内返回并降级。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/d.txt", body="机器学习")
    fake_sem = SemanticSearchService(db, OkEmbedder(), SlowVector(0.01, {"file_id": fid, "source_type": "doc_chunk", "chunk_index": 0, "text": "机器学习"}))
    svc = _make_svc(db, semantic=fake_sem, fts=SlowFts(0.4))
    started = time.perf_counter()
    out = svc.search("机器学习")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.4, f"FTS 整体 deadline 未生效：请求阻塞 {elapsed:.1f}s（3 次 MATCH 1.2s）"
    # 1s future 超时 → keyword 降级；语义通道正常
    assert out.degraded == ["keyword"]
    assert out.results and out.results[0]["file_id"] == fid
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_fts_single_slow_form_within_deadline(tmp_path):
    """单个 form 阻塞 1.5s：请求仍约 1s 内返回（后续形式不执行）。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    fid = _seed_file(db, files, fts, "/x/e.txt", body="机器学习")
    fake_sem = SemanticSearchService(db, OkEmbedder(), SlowVector(0.01, {"file_id": fid, "source_type": "doc_chunk", "chunk_index": 0, "text": "机器学习"}))
    svc = _make_svc(db, semantic=fake_sem, fts=SlowFts(1.5))
    started = time.perf_counter()
    out = svc.search("机器学习")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"单个 form 突破 deadline：请求阻塞 {elapsed:.1f}s"
    assert out.degraded == ["keyword"]
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


# ================= B：QueryParser 类型误判回归 =================

def test_parser_no_false_image_type():
    from omnisearch.server.service.query_parser import QueryParser as QP

    p = QP(TimeRangeService())
    for q in ("机器学习图片搜索系统", "图片搜索", "图片识别", "图片文字", "图片内容", "图片管理", "图片系统", "图片相关"):
        x = p.parse(q)
        assert "image" not in x.file_types, f"{q!r} 误判为 image"


def test_parser_about_image_doc():
    """「关于图片的文档」：末尾 文档 → doc；图片是修饰语 → 不抽 image。"""
    p = QueryParser(TimeRangeService())
    x = p.parse("关于图片的文档")
    assert x.file_types == ["doc"]


def test_parser_last_word_type_still_works():
    """末尾类型词仍正常抽取（含 spec 示例）。"""
    p = QueryParser(TimeRangeService())
    assert p.parse("机器学习图片搜索系统").file_types == []
    x = p.parse("昨天的自由女神照片")
    assert x.file_types == ["image"]
    assert x.semantic_text == "自由女神"
    assert p.parse("图片").file_types == ["image"]
    assert p.parse("机器学习文档").file_types == ["doc"]


# ================= C：Metadata-only Query =================

def test_metadata_only_yesterday_photos(plain):
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/p1.jpg", mtime_days_ago=1, file_type=FileType.IMAGE)
    _seed_file(db, files, fts, "/x/p2.jpg", mtime_days_ago=30, file_type=FileType.IMAGE)
    _seed_file(db, files, fts, "/x/doc.txt", body="机器学习", mtime_days_ago=1)
    out = svc.search("昨天的照片")
    assert out.parsed.file_types == ["image"] and out.parsed.semantic_text == ""
    ids = [r["file_id"] for r in out.results]
    assert a in ids and len(ids) == 1  # 仅昨天图片
    r = out.results[0]
    assert r["rrf_score"] is None and r["keyword_score"] is None and r["semantic_score"] is None
    assert any(x["channel"] == "metadata" for x in r["match_reasons"])
    assert out.degraded == []


def test_metadata_only_this_month_docs(plain):
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/notes.txt", mtime_days_ago=3)
    _seed_file(db, files, fts, "/x/old.txt", mtime_days_ago=40)
    _seed_file(db, files, fts, "/x/img.jpg", mtime_days_ago=1, file_type=FileType.IMAGE)
    out = svc.search("本月的文档")
    ids = [r["file_id"] for r in out.results]
    assert a in ids and len(ids) == 1
    assert out.results[0]["time_info"]["basis"] == "mtime"  # fallback 标注


def test_metadata_only_extension(plain):
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/a.pdf")
    _seed_file(db, files, fts, "/x/b.txt")
    out = svc.search("pdf")
    assert out.parsed.extensions == ["pdf"] and out.parsed.semantic_text == ""
    assert [r["file_id"] for r in out.results] == [a]


def test_metadata_only_yesterday(plain):
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/y.txt", mtime_days_ago=1)
    _seed_file(db, files, fts, "/x/o.txt", mtime_days_ago=20)
    out = svc.search("昨天")
    assert [r["file_id"] for r in out.results] == [a]
    assert out.results[0]["rrf_score"] is None
    assert any(x["channel"] == "metadata" for x in out.results[0]["match_reasons"])


def test_metadata_only_image_type(plain):
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    a = _seed_file(db, files, fts, "/x/shot.jpg", file_type=FileType.IMAGE)
    _seed_file(db, files, fts, "/x/doc.txt")
    out = svc.search("image")
    assert out.parsed.file_types == ["image"]
    assert [r["file_id"] for r in out.results] == [a]


def test_metadata_only_sorted_by_time(plain):
    """有时间过滤 → 按 basis 时间倒序（最新在前）。"""
    db, svc = plain
    files, fts = FileRepository(db), FtsRepository(db)
    old = _seed_file(db, files, fts, "/x/old.txt", mtime_days_ago=10)
    new = _seed_file(db, files, fts, "/x/new.txt", mtime_days_ago=1)
    out = svc.search("本月")
    assert [r["file_id"] for r in out.results] == [new, old]


# ================= D：Health readiness =================

def test_health_components_readiness(tmp_path):
    """health 区分 sqlite/qdrant/worker/semantic；语义不可用 ≠ 服务崩溃。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    from fastapi.testclient import TestClient

    from omnisearch.server.main import create_app

    with TestClient(create_app()) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["components"]) == {"sqlite", "qdrant", "worker", "semantic"}
    assert body["components"]["sqlite"]["ok"] is True
    # 无模型环境：semantic_ready=false（BGE 缺失），但 sqlite 正常
    assert body["components"]["semantic"]["ok"] is False
    assert body["components"]["worker"]["ok"] is False  # 测试环境无 Worker 心跳
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_health_semantic_ready_ok_and_degraded(tmp_path):
    """semantic_ready 可被 lifespan/配置翻转；true 时 semantic 组件 ok。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    from fastapi.testclient import TestClient

    from omnisearch.server.api import health as health_api
    from omnisearch.server.main import create_app

    with TestClient(create_app()) as client:
        health_api.configure_semantic_ready(True)
        body = client.get("/health").json()
        assert body["components"]["semantic"]["ok"] is True
        health_api.configure_semantic_ready(False)
        body = client.get("/health").json()
        assert body["components"]["semantic"]["ok"] is False
        assert body["components"]["sqlite"]["ok"] is True  # 语义失败不影响 sqlite
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_semantic_unavailable_degraded_channels(tmp_path):
    """semantic_ready=false（semantic 未配置）→ hybrid 请求 degraded=["semantic"]，关键词正常。"""
    db = _env(tmp_path)
    files, fts = FileRepository(db), FtsRepository(db)
    _seed_file(db, files, fts, "/x/f.txt", body="机器学习")
    svc = _make_svc(db)  # 无 semantic
    out = svc.search("机器学习")
    assert out.degraded == ["semantic"]
    assert out.results and out.results[0]["filename"] == "f.txt"
    os.environ.pop("OMNISEARCH_DEV_DATA", None)
