"""正文搜索 API 测试（M2：fts_body 通道，matched_in 标记）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.server.main import create_app
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.common.models import FileType, SourceType


def _meta(path: str, file_type: FileType = FileType.DOC) -> FileMeta:
    return FileMeta(
        path=path, filename=Path(path).name, dir_path=str(Path(path).parent),
        extension=Path(path).suffix.lower(), size_bytes=10, mtime_ns=2, ctime_ns=1,
        file_type=file_type, mime_type=None,
    )


def _seed_doc(db: Database, path: str, body: str) -> None:
    """插入文件 + 正文 chunk（模拟 Worker 已处理，AI_DONE）。"""
    from omnisearch.common.utils.seg import seg_text

    files, fts = FileRepository(db), FtsRepository(db)
    ops = files.upsert_batch([_meta(path)])
    fts.insert(ops[0].file_id, ops[0].filename, ops[0].filename_seg, ops[0].dir_tokens)
    with db.connect() as c:
        c.execute(
            """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
               VALUES (?, ?, 0, ?, ?)""",
            (ops[0].file_id, SourceType.DOC_CHUNK.value, body, seg_text(body)),
        )
        c.execute("UPDATE files SET status='AI_DONE' WHERE id=?", (ops[0].file_id,))


@pytest.fixture()
def client(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_search_body_keyword(tmp_path, client):
    """正文关键词命中：match_reasons 含 body 通道（M5 §12.6）。"""
    db = Database(db_path(tmp_path))
    _seed_doc(db, "/x/notes.txt", "本文档讨论机器学习与深度学习的关系。")
    resp = client.post("/api/v1/search", json={"query": "机器学习", "topK": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["results"][0]
    assert item["filename"] == "notes.txt"
    assert any(r["channel"] == "body" for r in item["match_reasons"])
    assert item["keyword_score"] is not None


def test_search_filename_and_body(tmp_path, client):
    """文件名 + 正文命中：match_reasons 分别含 keyword / body 通道。"""
    db = Database(db_path(tmp_path))
    _seed_doc(db, "/x/resume.txt", "简历正文包含技能描述。")
    resp = client.post("/api/v1/search", json={"query": "resume", "topK": 10})
    assert any(r["channel"] == "keyword" for r in resp.json()["results"][0]["match_reasons"])
    resp2 = client.post("/api/v1/search", json={"query": "简历", "topK": 10})
    assert any(r["channel"] == "body" for r in resp2.json()["results"][0]["match_reasons"])


def test_search_body_excludes_deleted(tmp_path, client):
    """canonical：正文候选回表应用 is_deleted=0（删除文件不因正文命中返回）。"""
    db = Database(db_path(tmp_path))
    _seed_doc(db, "/x/secret.txt", "机密内容关键词")
    with db.connect() as c:
        c.execute("UPDATE files SET is_deleted=1 WHERE filename='secret.txt'")
        c.commit()
    resp = client.post("/api/v1/search", json={"query": "机密", "topK": 10})
    assert resp.json()["total"] == 0


def test_search_body_chinese_seg(tmp_path, client):
    """中文正文：jieba 预分词一致（seg 列 + 查询同分词器，前缀 AND）。

    注：'自由女神' 这类 jieba 上下文分词不一致（句中切为 '自由 女神像'）属 M5
    QueryParser/phrase 降级边界；此处用分词一致的 '机器学习' 验证。
    """
    db = Database(db_path(tmp_path))
    _seed_doc(db, "/x/report.md", "本文档讨论机器学习与深度学习的关系。")
    resp = client.post("/api/v1/search", json={"query": "机器学习", "topK": 10})
    assert resp.json()["total"] == 1
    assert any(r["channel"] == "body" for r in resp.json()["results"][0]["match_reasons"])
