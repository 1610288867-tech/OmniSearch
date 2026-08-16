"""WatchService 失效 root 容错测试（修复 verify-omnisearch 实测发现的缺陷）。

覆盖：valid root 正常启动 / missing root 不抛异常 / 混合场景 valid 仍工作 /
invalid root 记 warning / FastAPI lifespan 不受失效 root 影响 / 重启后仍可启动。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.database import Database
from omnisearch.server.main import create_app
from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.watch import WatchService


def _make_watch(tmp_path, debounce_s: float = 0.5) -> tuple[WatchService, IndexService]:
    from omnisearch.server.database.migrations.migrate import migrate

    db = Database(tmp_path / "watch.db")
    migrate(db)
    svc = IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))
    watch = WatchService(
        on_changes=svc.handle_changes,
        on_deleted_paths=lambda paths: [svc.handle_delete_path(p) for p in paths],
        on_renamed=svc.handle_rename,
        debounce_s=debounce_s,
    )
    return watch, svc


def _wait_until(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_valid_root_starts_watcher(tmp_path):
    """1. valid root → watcher 正常启动（真实事件可达）。"""
    root = tmp_path / "valid"
    root.mkdir()
    watch, svc = _make_watch(tmp_path)
    watch.start([str(root)])
    try:
        (root / "new.txt").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: any(
            r["filename"] == "new.txt" for r in _paths_of(svc)
        )), "valid root 事件应被监听到"
    finally:
        watch.stop()


def test_missing_root_does_not_raise(tmp_path):
    """2. missing root → start 不抛异常。"""
    watch, _ = _make_watch(tmp_path)
    watch.start([str(tmp_path / "missing_dir")])  # 不应抛
    assert watch._observer is None  # 全部无效 → 不启动 observer


def test_missing_plus_valid_root(tmp_path):
    """3. missing + valid → valid root 正常工作。"""
    valid = tmp_path / "valid2"
    valid.mkdir()
    watch, svc = _make_watch(tmp_path)
    watch.start([str(tmp_path / "missing_dir"), str(valid)])
    try:
        (valid / "ok.txt").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: any(r["filename"] == "ok.txt" for r in _paths_of(svc)))
    finally:
        watch.stop()


def test_invalid_root_warns(caplog, tmp_path):
    """4. invalid root → warning log。"""
    watch, _ = _make_watch(tmp_path)
    with caplog.at_level(logging.WARNING, logger="omnisearch.server.watch"):
        watch.start([str(tmp_path / "ghost")])
    assert any("watch root invalid" in r.message for r in caplog.records)


def test_lifespan_ok_with_invalid_root(tmp_path):
    """5. FastAPI lifespan 在存在失效 root 时仍成功启动（/health 200）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    try:
        # 预置 settings：一个失效 root + 一个有效 root
        valid = tmp_path / "valid_root"
        valid.mkdir()
        from omnisearch.common.config import db_path
        from omnisearch.server.database.migrations.migrate import migrate
        from omnisearch.server.repository.settings import SettingsRepository

        db = Database(db_path(tmp_path))
        migrate(db)
        SettingsRepository(db).set("index_roots", [str(tmp_path / "gone"), str(valid)])

        with TestClient(create_app()) as client:  # lifespan 含 watch.start（失效 root 不应炸掉启动）
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["components"]["sqlite"]["ok"] is True
    finally:
        os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_restart_with_stale_root_succeeds(tmp_path):
    """6. 重启后 settings 存在失效 root → 服务仍能启动（同一 db 二次 create_app）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    try:
        # 第一次：有效 root 扫描 → settings 持久化该 root
        valid = tmp_path / "stale_root"
        valid.mkdir()
        (valid / "a.txt").write_text("x", encoding="utf-8")
        from omnisearch.common.config import db_path
        from omnisearch.server.database.migrations.migrate import migrate
        from omnisearch.server.repository.settings import SettingsRepository

        db = Database(db_path(tmp_path))
        migrate(db)
        SettingsRepository(db).set("index_roots", [str(valid)])
        # 模拟：root 随后被删除（磁盘失效，settings 仍持有）
        import shutil

        shutil.rmtree(valid)

        # 第二次启动（重启场景）：失效 root 不应导致启动失败
        with TestClient(create_app()) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            # qdrant 组件在测试环境不可用 → degraded 属预期；关键是无异常启动且 sqlite 正常
            assert resp.json()["status"] in ("ok", "degraded")
            assert resp.json()["components"]["sqlite"]["ok"] is True
    finally:
        os.environ.pop("OMNISEARCH_DEV_DATA", None)


def _paths_of(svc):
    conn = svc._db.connect()
    rows = conn.execute("SELECT path, filename, is_deleted FROM files").fetchall()
    conn.close()
    return [dict(r) for r in rows]
