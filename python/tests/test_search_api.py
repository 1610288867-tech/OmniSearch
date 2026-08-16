"""Search API 测试（M1 + M5：POST /api/v1/search —— Hybrid Search）。

覆盖：正常搜索 / 空 query / topK / 无结果 / is_deleted=0 canonical / 中文路径 / token 鉴权
（响应结构为 M5 Hybrid：parsed / rrf_score / keyword_score / semantic_score / time_info / match_reasons）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.common.models import FileType
from omnisearch.server.main import create_app
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository


def _meta(path: str) -> FileMeta:
    return FileMeta(
        path=path, filename=path.rsplit("/", 1)[-1], dir_path=path.rsplit("/", 1)[0],
        extension=Path(path).suffix.lower(), size_bytes=10, mtime_ns=2, ctime_ns=1,
        file_type=FileType.DOC, mime_type=None,
    )


def _db(tmp_path) -> Database:
    return Database(db_path(tmp_path))


def _seed(db: Database, paths: list[str]) -> None:
    files, fts = FileRepository(db), FtsRepository(db)
    for p in paths:
        ops = files.upsert_batch([_meta(p)])
        fts.insert(ops[0].file_id, ops[0].filename, ops[0].filename_seg, ops[0].dir_tokens)


@pytest.fixture()
def client(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_search_normal(tmp_path, client):
    _seed(_db(tmp_path), ["/x/resume.pdf", "/x/report-final.docx", "/x/photo.jpg"])
    resp = client.post("/api/v1/search", json={"query": "resume", "topK": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["results"][0]
    assert item["filename"] == "resume.pdf"
    # M5 Hybrid 响应：parsed + 三分数 + time_info + match_reasons（§13/§12.5/§12.6）
    assert set(item) >= {
        "file_id", "path", "filename", "dir_path", "extension", "file_type", "size_bytes", "mtime_ns",
        "rrf_score", "keyword_score", "semantic_score", "time_info", "match_reasons",
    }
    assert set(body) >= {"query", "parsed", "total", "latency_ms", "results", "degraded_channels"}
    assert body["parsed"]["semantic_text"] == "resume"
    assert body["parsed"]["parse_method"] == "rule"
    # 关键词命中（语义通道未配置）：keyword_score 有值，semantic_score=null，degraded 标注语义
    assert item["keyword_score"] is not None
    assert item["semantic_score"] is None
    assert body["degraded_channels"] == ["semantic"]


def test_search_empty_query(tmp_path, client):
    _seed(_db(tmp_path), ["/x/a.txt"])
    resp = client.post("/api/v1/search", json={"query": "", "topK": 10})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_search_topk(tmp_path, client):
    _seed(_db(tmp_path), [f"/x/report{i}.txt" for i in range(5)])
    resp = client.post("/api/v1/search", json={"query": "report", "topK": 2})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


def test_search_no_results(tmp_path, client):
    _seed(_db(tmp_path), ["/x/a.txt"])
    resp = client.post("/api/v1/search", json={"query": "zzz_nonexistent", "topK": 10})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_search_excludes_deleted(tmp_path, client):
    """canonical is_deleted=0：软删除文件永不进入搜索结果（architecture.md §12.2/§11.3）。"""
    db = _db(tmp_path)
    _seed(db, ["/x/secret.txt"])
    # 软删除 + FTS 仍残留（模拟异步清理未完成）
    conn = db.connect()
    conn.execute("UPDATE files SET is_deleted=1 WHERE filename='secret.txt'")
    conn.commit()
    conn.close()
    resp = client.post("/api/v1/search", json={"query": "secret", "topK": 10})
    assert resp.json()["total"] == 0


def test_search_chinese_filename(tmp_path, client):
    """中文文件名：jieba 预分词一致（写入 seg 列 + 查询同分词器，architecture.md §8.3）。"""
    _seed(_db(tmp_path), ["/x/自由女神像照片.jpg"])
    resp = client.post("/api/v1/search", json={"query": "自由女神", "topK": 10})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_search_reserved_chars_no_error(tmp_path, client):
    """含 FTS5 保留字符的 raw query 不产生 500（M1 收尾 1：sanitizer 防 syntax error）。"""
    _seed(_db(tmp_path), ["/x/live-test.pdf"])
    for q in ["live-test", "a\"b", "(x)", "a:b", "a*b", "report and final"]:
        resp = client.post("/api/v1/search", json={"query": q, "topK": 10})
        assert resp.status_code == 200, f"query {q!r} → HTTP {resp.status_code}"
    # 语义保持：'live-test' 仍可命中 live-test.pdf（'-' 分词为 live AND test）
    resp = client.post("/api/v1/search", json={"query": "live-test", "topK": 10})
    assert resp.json()["total"] == 1


def test_search_requires_token(tmp_path):
    """token 鉴权：配置 token 后无 X-Omni-Token → 401（architecture.md §12）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    os.environ["OMNISEARCH_TOKEN"] = "secret"
    try:
        with TestClient(create_app()) as c:
            resp = c.post("/api/v1/search", json={"query": "x", "topK": 10})
            assert resp.status_code == 401
            ok = c.post("/api/v1/search", json={"query": "x", "topK": 10}, headers={"X-Omni-Token": "secret"})
            assert ok.status_code == 200
    finally:
        os.environ.pop("OMNISEARCH_TOKEN", None)
        os.environ.pop("OMNISEARCH_DEV_DATA", None)
