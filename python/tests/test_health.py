"""GET /health 冒烟测试（architecture.md §13）。"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from omnisearch.common.config import dev_data_dir
from omnisearch.server.main import create_app


def test_health_ok(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as client:  # with 上下文触发 lifespan（migration + configure）
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")  # qdrant 未启动时为 degraded
    assert body["components"]["sqlite"]["ok"] is True
    assert "qdrant" in body["components"]
    os.environ.pop("OMNISEARCH_DEV_DATA", None)
