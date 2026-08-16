"""域 I：Task Dashboard（M5 §18I）—— retry / reindex / duplicate enqueue / max attempts。

使用 API 层（TestClient）+ 仓库层断言（architecture.md §7.1/§13）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.common.models import TaskStatus
from omnisearch.server.main import create_app
from omnisearch.server.repository.tasks import TaskRepository


@pytest.fixture()
def client(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as c:
        yield c, TaskRepository(Database(db_path(tmp_path)))
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def _seed_task(db: Database, status: TaskStatus, attempt: int = 0, max_attempts: int = 3) -> int:
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, 'f.txt', '/x', '.txt', 1, 1, 'doc')""",
            (f"/x/f{status.value}{attempt}.txt",),
        )
        fid = cur.lastrowid
        cur = c.execute(
            """INSERT INTO ai_tasks (file_id, task_type, status, attempt, max_attempts)
               VALUES (?, 'index_file', ?, ?, ?)""",
            (fid, status.value, attempt, max_attempts),
        )
        tid = cur.lastrowid
        c.commit()
    return tid, fid


def test_stats_counts(client):
    c, repo = client
    t1, _ = _seed_task(repo._db, TaskStatus.PENDING)
    t2, _ = _seed_task(repo._db, TaskStatus.RUNNING)
    _seed_task(repo._db, TaskStatus.SUCCESS)
    _seed_task(repo._db, TaskStatus.FAILED)
    resp = c.get("/api/v1/task/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"queue_length": 1, "processing": 1, "success": 1, "failed": 1, "total": 4}


def test_failed_list(client):
    c, repo = client
    tid, fid = _seed_task(repo._db, TaskStatus.FAILED)
    with repo._db.connect() as conn:
        conn.execute("UPDATE ai_tasks SET last_error='ocr failed' WHERE id=?", (tid,))
        conn.commit()
    resp = c.get("/api/v1/task/failed")
    assert resp.status_code == 200
    items = resp.json()
    assert items[0]["id"] == tid and items[0]["file_id"] == fid
    assert items[0]["filename"] == "f.txt" and items[0]["last_error"] == "ocr failed"


def test_retry_success(client):
    """retry：FAILED → PENDING（复用任务，attempt 累计）。"""
    c, repo = client
    tid, _ = _seed_task(repo._db, TaskStatus.FAILED, attempt=1)
    resp = c.post(f"/api/v1/task/{tid}/retry")
    assert resp.json()["status"] == "retried"
    with repo._db.connect() as conn:
        assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (tid,)).fetchone()["status"] == "PENDING"


def test_retry_max_attempts_exceeded(client):
    """attempt >= max_attempts → 409 MAX_ATTEMPTS_EXCEEDED（§7.1，只能 reindex）。"""
    c, repo = client
    tid, _ = _seed_task(repo._db, TaskStatus.FAILED, attempt=3, max_attempts=3)
    resp = c.post(f"/api/v1/task/{tid}/retry")
    assert resp.status_code == 409
    assert "MAX_ATTEMPTS_EXCEEDED" in resp.text
    with repo._db.connect() as conn:
        assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (tid,)).fetchone()["status"] == "FAILED"


def test_retry_not_found(client):
    c, _ = client
    resp = c.post("/api/v1/task/99999/retry")
    assert resp.status_code == 409
    assert "TASK_NOT_FOUND" in resp.text


def test_reindex_creates_new_task(client):
    """reindex：新建任务（旧任务历史保留）；SUCCESS 后仍可 reindex。"""
    c, repo = client
    tid, _ = _seed_task(repo._db, TaskStatus.SUCCESS)
    resp = c.post(f"/api/v1/task/{tid}/reindex")
    assert resp.json()["status"] == "enqueued"
    with repo._db.connect() as conn:
        rows = conn.execute("SELECT count(*) n FROM ai_tasks WHERE status='PENDING'").fetchone()["n"]
        assert rows == 1


def test_reindex_blocked_by_active_task(client):
    """活跃任务存在（PENDING/RUNNING）→ ALREADY_ACTIVE（partial unique index 兜底，§7.1）。"""
    c, repo = client
    tid, _ = _seed_task(repo._db, TaskStatus.RUNNING)
    resp = c.post(f"/api/v1/task/{tid}/reindex")
    assert resp.json()["status"] == "ALREADY_ACTIVE"


def test_duplicate_enqueue_skipped(client):
    """防重复入队：同一 file_id 的 PENDING 任务存在 → 跳过（数据库级保证）。"""
    _c, repo = client
    tid, fid = _seed_task(repo._db, TaskStatus.PENDING)
    n = repo.enqueue([fid])
    assert n == 0  # ON CONFLICT DO NOTHING（partial unique index）
    with repo._db.connect() as conn:
        rows = conn.execute("SELECT count(*) n FROM ai_tasks WHERE file_id=? AND status='PENDING'", (fid,)).fetchone()["n"]
        assert rows == 1
