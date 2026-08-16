"""IndexJobRepository —— index_jobs 表访问（扫描进度 + 状态，architecture.md §7.1/§11.1）。"""
from __future__ import annotations

from omnisearch.common.database import Database
from omnisearch.common.models import JobStatus


class IndexJobRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, root_path: str, scan_type: str) -> int:
        with self._db.connect() as c:
            cur = c.execute(
                """INSERT INTO index_jobs (root_path, scan_type, status, started_at)
                   VALUES (?, ?, ?, unixepoch())""",
                (root_path, scan_type, JobStatus.RUNNING.value),
            )
            return cur.lastrowid

    def update_progress(self, job_id: int, scanned: int, errors: int = 0) -> None:
        with self._db.connect() as c:
            c.execute(
                "UPDATE index_jobs SET scanned_files=?, error_count=? WHERE id=?",
                (scanned, errors, job_id),
            )

    def finish(self, job_id: int, status: str, total: int) -> None:
        with self._db.connect() as c:
            c.execute(
                """UPDATE index_jobs SET status=?, total_files=?, finished_at=unixepoch()
                   WHERE id=?""",
                (status, total, job_id),
            )

    def get(self, job_id: int) -> dict | None:
        with self._db.connect() as c:
            row = c.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def latest(self) -> dict | None:
        with self._db.connect() as c:
            row = c.execute("SELECT * FROM index_jobs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def recent(self, limit: int = 20) -> list[dict]:
        """最近 N 条作业（多 Root 扫描进度展示：当前 Root i/N 由 UI 计算）。"""
        with self._db.connect() as c:
            rows = c.execute(
                "SELECT * FROM index_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
