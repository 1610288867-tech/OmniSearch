"""Semantic Search API 测试（M4：POST /api/v1/search/semantic）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.database import Database
from omnisearch.common.utils.point_id import point_id
from omnisearch.common.vector import VectorPoint, VectorStore
from omnisearch.server.main import create_app


def _seed(db: Database, vs: VectorStore, bge, fid: int, source_type: str, text: str) -> None:
    from omnisearch.common.utils.seg import seg_text

    with db.connect() as c:
        c.execute(
            """INSERT INTO files (id, path, filename, dir_path, mtime_ns, ctime_ns, file_type)
               VALUES (?, ?, ?, '/x', 1, 1, 'doc')""",
            (fid, f"/x/doc{fid}.txt", f"doc{fid}.txt"),
        )
        c.execute(
            """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg, embedding_status)
               VALUES (?, ?, 0, ?, ?, 1)""",
            (fid, source_type, text, seg_text(text)),
        )
        c.commit()
    vec = bge.embed_texts([text])[0]
    vs.upsert_points(
        [VectorPoint(point_id(fid, source_type, 0), vec, fid, source_type, 0, text)]
    )


@pytest.fixture()
def client(tmp_path, qdrant_server, bge):
    """语义通道可用的 TestClient（env 指向 fixture Qdrant 端口 + 预置数据）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    os.environ["OMNISEARCH_QDRANT_HTTP_PORT"] = qdrant_server.rsplit(":", 1)[-1]
    # 模型目录 junction 链接（lifespan 加载 BGE 需要；避免复制 90MB）
    _link_models(tmp_path)
    from omnisearch.common.config import db_path
    from omnisearch.server.database.migrations.migrate import migrate

    db = Database(db_path(tmp_path))
    migrate(db)
    vs = VectorStore(qdrant_server, bge.dim)
    vs.ensure_collection()
    _seed(db, vs, bge, 1, "doc_chunk", "机器学习是人工智能的重要分支")
    _seed(db, vs, bge, 2, "ocr", "New York 2026 自由女神像")
    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("OMNISEARCH_DEV_DATA", None)
    os.environ.pop("OMNISEARCH_QDRANT_HTTP_PORT", None)


def _link_models(tmp_path: Path) -> None:
    """把 dev-data/models junction 到 tmp_path/models（Windows mklink /J）。"""
    import subprocess

    src = Path(__file__).resolve().parents[2] / "dev-data" / "models"
    dest = tmp_path / "models"
    if not src.exists():
        return
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
        check=False, capture_output=True,
    )


def test_semantic_search_normal(client):
    resp = client.post("/api/v1/search/semantic", json={"query": "深度学习与神经网络", "topK": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    item = body["results"][0]
    assert set(item) >= {"file_id", "path", "filename", "source_type", "chunk_index", "text", "semantic_score"}


def test_semantic_search_topk(client):
    resp = client.post("/api/v1/search/semantic", json={"query": "自由女神", "topK": 1})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_semantic_search_empty_query(client):
    resp = client.post("/api/v1/search/semantic", json={"query": "", "topK": 10})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
