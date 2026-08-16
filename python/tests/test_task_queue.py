"""任务队列测试（architecture.md §7.1 / §10.2 / §10.3）。

验证：claim 短事务、状态回写、retry 上限、partial unique 防重复入队。
"""
from __future__ import annotations

import sqlite3

from omnisearch.common.database import Database
from omnisearch.common.models import FileStatus, TaskStatus
from omnisearch.worker.task.queue import TaskQueue


def _insert_file(db: Database, path: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, ?, '', '', 1, 1, 'image')""",
            (path, path),
        )
        return cur.lastrowid


def _enqueue(db: Database, file_id: int, priority: int = 0) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO ai_tasks (file_id, task_type, priority)
               VALUES (?, 'index_file', ?)""",
            (file_id, priority),
        )
        return cur.lastrowid


def test_claim_transition(db: Database):
    q = TaskQueue(db)
    fid = _insert_file(db, "/x/a.jpg")
    tid = _enqueue(db, fid)

    claimed = q.claim_batch()
    assert claimed == [tid]

    conn = db.connect()
    row = conn.execute("SELECT status FROM ai_tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == TaskStatus.RUNNING.value
    frow = conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()
    assert frow["status"] == FileStatus.PROCESSING.value
    conn.close()

    # 无 PENDING 可再领
    assert q.claim_batch() == []


def test_complete_and_fail(db: Database):
    q = TaskQueue(db)
    fid = _insert_file(db, "/x/b.jpg")
    tid = _enqueue(db, fid)
    q.claim_batch()

    q.complete(tid)
    conn = db.connect()
    assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (tid,)).fetchone()["status"] == "SUCCESS"
    assert conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"] == "AI_DONE"
    conn.close()

    fid2 = _insert_file(db, "/x/c.jpg")
    tid2 = _enqueue(db, fid2)
    q.claim_batch()
    q.fail(tid2, "decode error")
    conn = db.connect()
    row = conn.execute("SELECT status, last_error FROM ai_tasks WHERE id=?", (tid2,)).fetchone()
    assert row["status"] == "FAILED" and row["last_error"] == "decode error"
    assert conn.execute("SELECT status FROM files WHERE id=?", (fid2,)).fetchone()["status"] == "FAILED"
    conn.close()


def test_retry_attempt_limit(db: Database):
    """retry：attempt >= max_attempts 拒绝并返回 MAX_ATTEMPTS_EXCEEDED（architecture.md §7.1）。"""
    q = TaskQueue(db)
    fid = _insert_file(db, "/x/d.jpg")
    tid = _enqueue(db, fid)
    with db.connect() as conn:
        conn.execute(
            "UPDATE ai_tasks SET status='FAILED', attempt=3, last_error='x' WHERE id=?",
            (tid,),
        )

    assert q.retry(tid) == "MAX_ATTEMPTS_EXCEEDED"
    # attempt 未达上限 → 置回 PENDING
    with db.connect() as conn:
        conn.execute(
            "UPDATE ai_tasks SET attempt=1 WHERE id=?", (tid,),
        )
    assert q.retry(tid) is None
    conn = db.connect()
    assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (tid,)).fetchone()["status"] == "PENDING"
    conn.close()


def test_duplicate_enqueue_rejected(db: Database):
    """partial unique index：同一 file_id 最多一个 PENDING/RUNNING 任务（防重复入队）。"""
    fid = _insert_file(db, "/x/e.jpg")
    _enqueue(db, fid)
    with db.connect() as conn:
        try:
            conn.execute(
                """INSERT INTO ai_tasks (file_id, task_type) VALUES (?, 'index_file')""",
                (fid,),
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
    assert raised, "第二个 PENDING 任务应被 partial unique index 拒绝"


# ================= W1/W2 回归（批次 1 审查修复） =================

def test_claim_increments_attempt(db, tmp_path):
    """W1：claim 时 attempt 递增（MAX_ATTEMPTS_EXCEEDED 可达）。"""
    from omnisearch.common.models import TaskStatus
    from omnisearch.server.repository.tasks import TaskRepository

    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, 'a.txt', '/x', '.txt', 1, 1, 'doc')""",
            (str(tmp_path / "a.txt"),),
        )
        fid = cur.lastrowid
        c.commit()
    TaskRepository(db).enqueue([fid])
    q = TaskQueue(db)
    q.claim_batch()
    q.claim_batch()  # RUNNING 不可再领（0）
    with db.connect() as c:
        row = c.execute("SELECT attempt, status FROM ai_tasks WHERE file_id=?", (fid,)).fetchone()
        assert row["attempt"] == 1 and row["status"] == TaskStatus.RUNNING.value


def test_recover_interrupted(db, tmp_path):
    """W2：崩溃遗留 RUNNING → 启动复位 PENDING。"""
    from omnisearch.common.models import TaskStatus
    from omnisearch.server.repository.tasks import TaskRepository

    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, 'b.txt', '/x', '.txt', 1, 1, 'doc')""",
            (str(tmp_path / "b.txt"),),
        )
        fid = cur.lastrowid
        c.commit()
    TaskRepository(db).enqueue([fid])
    q = TaskQueue(db)
    q.claim_batch()  # RUNNING（模拟崩溃遗留）
    assert q.recover_interrupted() == 1
    with db.connect() as c:
        assert c.execute("SELECT status FROM ai_tasks WHERE file_id=?", (fid,)).fetchone()["status"] == TaskStatus.PENDING.value
    assert q.recover_interrupted() == 0  # 幂等
