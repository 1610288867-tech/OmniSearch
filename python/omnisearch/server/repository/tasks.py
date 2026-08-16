"""TaskRepository —— ai_tasks 表访问（architecture.md §7.1/§10.3）。

M2 生产者：IndexService（扫描/增量对 doc 文件入队 index_file）。
防重复入队：partial unique index（file_id WHERE PENDING/RUNNING）+ ON CONFLICT DO NOTHING。
"""
from __future__ import annotations

import sqlite3

from omnisearch.common.database import Database
from omnisearch.common.models import TaskStatus, TaskType


class TaskRepository:
    def __init__(self, db: Database):
        self._db = db

    def enqueue(self, file_ids: list[int], priority: int = 1, conn: sqlite3.Connection | None = None) -> int:
        """批量入队 index_file；返回实际入队数（活跃任务存在则跳过，不报错）。"""
        if not file_ids:
            return 0
        own = conn is None
        c = conn or self._db.connect()
        try:
            c.executemany(
                f"""INSERT INTO ai_tasks (file_id, task_type, priority)
                    VALUES (?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                [(fid, TaskType.INDEX_FILE.value, priority) for fid in file_ids],
            )
            if own:
                c.commit()
            return c.total_changes if own else len(file_ids)
        finally:
            if own:
                c.close()

    def count_by_status(self, status: TaskStatus) -> int:
        with self._db.connect() as c:
            row = c.execute("SELECT count(*) AS n FROM ai_tasks WHERE status=?", (status.value,)).fetchone()
            return row["n"]

    def stats(self) -> dict[str, int]:
        """队列汇总（Task Dashboard，architecture.md §13）：queue_length/processing/success/failed/total。"""
        with self._db.connect() as c:
            rows = c.execute("SELECT status, count(*) AS n FROM ai_tasks GROUP BY status").fetchall()
            counts = {r["status"]: r["n"] for r in rows}
            total = sum(counts.values())
        return {
            "queue_length": counts.get(TaskStatus.PENDING.value, 0),
            "processing": counts.get(TaskStatus.RUNNING.value, 0),
            "success": counts.get(TaskStatus.SUCCESS.value, 0),
            "failed": counts.get(TaskStatus.FAILED.value, 0),
            "total": total,
        }

    def failed_tasks(self, limit: int = 50) -> list[dict]:
        """最近 FAILED 任务明细（含文件名，Dashboard 重试列表用）。"""
        with self._db.connect() as c:
            rows = c.execute(
                """SELECT t.id, t.file_id, t.attempt, t.max_attempts, t.last_error,
                          COALESCE(f.filename, '<deleted>') AS filename
                   FROM ai_tasks t LEFT JOIN files f ON f.id = t.file_id
                   WHERE t.status = ? ORDER BY t.updated_at DESC LIMIT ?""",
                (TaskStatus.FAILED.value, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def retry(self, task_id: int) -> str:
        """手动重试：FAILED → PENDING（attempt 累计，architecture.md §7.1/§10.3）。

        attempt >= max_attempts → MAX_ATTEMPTS_EXCEEDED（只能 reindex 新建任务）；
        任务不存在 → TASK_NOT_FOUND；成功 → "retried"。
        """
        with self._db.connect() as c:
            row = c.execute("SELECT attempt, max_attempts, status FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "TASK_NOT_FOUND"
            if row["attempt"] >= row["max_attempts"]:
                return "MAX_ATTEMPTS_EXCEEDED"
            if row["status"] != TaskStatus.FAILED.value:
                return "NOT_FAILED"
            c.execute(
                """UPDATE ai_tasks SET status=?, updated_at=unixepoch() WHERE id=?""",
                (TaskStatus.PENDING.value, task_id),
            )
            return "retried"

    def reindex(self, task_id: int) -> tuple[int, str]:
        """重建索引：为任务对应文件创建新 index_file 任务（历史保留）。

        返回 (file_id, "enqueued" | "ALREADY_ACTIVE")；活跃任务（PENDING/RUNNING）
        存在时按 partial unique index 语义返回 ALREADY_ACTIVE（architecture.md §7.1）。
        """
        with self._db.connect() as c:
            row = c.execute("SELECT file_id FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return -1, "TASK_NOT_FOUND"
            active = c.execute(
                """SELECT 1 FROM ai_tasks
                   WHERE file_id=? AND status IN ('PENDING','RUNNING')""",
                (row["file_id"],),
            ).fetchone()
            if active:
                return row["file_id"], "ALREADY_ACTIVE"
            self.enqueue([row["file_id"]], priority=0, conn=c)
            return row["file_id"], "enqueued"
