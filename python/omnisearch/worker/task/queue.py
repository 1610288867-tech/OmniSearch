"""ai_tasks 队列访问（共享 SQLite，单机单 Worker 轻量持久化队列）。

claim 事务边界（architecture.md §10.2）：BEGIN IMMEDIATE → SELECT PENDING LIMIT n
→ UPDATE RUNNING + files.status=PROCESSING → COMMIT。原子且短。
推理/OCR/Embedding 一律在事务外执行（此处只做状态流转）。
"""
from __future__ import annotations

import logging

from omnisearch.common.database import Database
from omnisearch.common.models import FileStatus, TaskStatus

logger = logging.getLogger("omnisearch.worker.task")


class TaskQueue:
    """ai_tasks 轻量持久化队列（生产者：FastAPI/索引管道；消费者：单 Worker）。"""

    def __init__(self, db: Database):
        self._db = db

    def claim_batch(self, batch_size: int = 8) -> list[int]:
        """原子领取 PENDING 任务（短事务），返回 task_id 列表。

        同一事务内将对应 files.status 置 PROCESSING（架构 §10.2/§10.3）。
        """
        conn = self._db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id FROM ai_tasks
                   WHERE status = ? ORDER BY priority, id LIMIT ?""",
                (TaskStatus.PENDING.value, batch_size),
            ).fetchall()
            if not rows:
                conn.execute("COMMIT")
                return []
            ids = [r["id"] for r in rows]
            conn.execute(
                """UPDATE ai_tasks SET status = ?, updated_at = unixepoch()
                   WHERE id IN ({})""".format(",".join("?" * len(ids))),
                (TaskStatus.RUNNING.value, *ids),
            )
            conn.execute(
                """UPDATE files SET status = ? WHERE id IN (
                       SELECT file_id FROM ai_tasks WHERE id IN ({})
                   )""".format(",".join("?" * len(ids))),
                (FileStatus.PROCESSING.value, *ids),
            )
            conn.execute("COMMIT")
            return ids
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete(self, task_id: int) -> None:
        """任务成功：SUCCESS + files.status=AI_DONE（短事务）。"""
        with self._db.connect() as conn:
            conn.execute(
                """UPDATE ai_tasks SET status = ?, updated_at = unixepoch() WHERE id = ?""",
                (TaskStatus.SUCCESS.value, task_id),
            )
            conn.execute(
                """UPDATE files SET status = ? WHERE id IN (
                       SELECT file_id FROM ai_tasks WHERE id = ?
                   )""",
                (FileStatus.AI_DONE.value, task_id),
            )

    def fail(self, task_id: int, error: str) -> None:
        """任务失败：FAILED + last_error + files.status=FAILED（短事务）。

        FAILED 表示未完整完成，不代表文件完全不可搜索（architecture.md §10.3）——
        已生成的 OCR/chunks/FTS/embedding 结果保留，由搜索侧按现有能力降级。
        """
        with self._db.connect() as conn:
            conn.execute(
                """UPDATE ai_tasks SET status = ?, last_error = ?, updated_at = unixepoch()
                   WHERE id = ?""",
                (TaskStatus.FAILED.value, error[:2000], task_id),
            )
            conn.execute(
                """UPDATE files SET status = ? WHERE id IN (
                       SELECT file_id FROM ai_tasks WHERE id = ?
                   )""",
                (FileStatus.FAILED.value, task_id),
            )

    def retry(self, task_id: int, max_attempts: int = 3) -> str | None:
        """手动重试：FAILED → PENDING（attempt 累计）。

        attempt >= max_attempts 时拒绝并返回 MAX_ATTEMPTS_EXCEEDED（architecture.md §7.1）。
        成功返回 None。
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT attempt FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return "TASK_NOT_FOUND"
            if row["attempt"] >= max_attempts:
                return "MAX_ATTEMPTS_EXCEEDED"
            conn.execute(
                """UPDATE ai_tasks SET status = ?, updated_at = unixepoch() WHERE id = ?""",
                (TaskStatus.PENDING.value, task_id),
            )
            return None
