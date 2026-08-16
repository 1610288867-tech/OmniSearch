"""Settings API 测试（M5 §16：search mode / weights / topK / index roots / model status / storage）。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from omnisearch.server.main import create_app


@pytest.fixture()
def client(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_settings_defaults(client):
    resp = client.get("/api/v1/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["search_mode"] == "hybrid"  # 默认 Hybrid（M5 §14）
    assert body["w_kw"] == 1.0 and body["w_sem"] == 1.0
    assert body["topK"] == 50
    assert body["index_roots"] == []
    assert set(body["models"]) == {"bge", "caption"}
    assert set(body["storage"]) == {"db_bytes", "models_bytes"}


def test_settings_update_and_persist(client):
    resp = client.put("/api/v1/settings", json={"search_mode": "semantic", "w_kw": 2.0, "topK": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert body["search_mode"] == "semantic" and body["w_kw"] == 2.0 and body["topK"] == 20
    # 持久化（重启后仍在，settings KV 表）
    again = client.get("/api/v1/settings")
    assert again.json()["search_mode"] == "semantic" and again.json()["topK"] == 20


def test_settings_invalid_values_rejected(client):
    assert client.put("/api/v1/settings", json={"search_mode": "evil"}).status_code == 422
    assert client.put("/api/v1/settings", json={"w_kw": 100}).status_code == 422
    assert client.put("/api/v1/settings", json={"topK": 0}).status_code == 422
