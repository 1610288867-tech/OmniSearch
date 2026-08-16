"""SQLite 连接门面 —— 位于 common（唯一共享层）。

背景（架构冲突的处理）：architecture.md §2.1 要求「FastAPI/Worker 各自连接 SQLite」，
但 §14 目录树将 database/ 置于 server 层。若 worker import server.database 将违反
CLAUDE.md 依赖规则「共享代码只能进 common」。因此连接类置于 common/database.py；
server/database/ 仅保留 migrations（迁移执行是 server 启动职责，worker 不执行迁移）。

规则（architecture.md §7 / §10.2）：
- WAL + busy_timeout + foreign_keys（多进程读写）
- 每线程独立连接；Repository 接收连接工厂而非连接实例
- 所有写事务必须短事务；耗时操作一律在事务外
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Database:
    """SQLite 主库访问门面（事实数据源，architecture.md §3）。"""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def connect(self) -> sqlite3.Connection:
        """当前线程的新连接（调用方负责关闭；短事务推荐）。"""
        return self._open()

    def connection(self) -> sqlite3.Connection:
        """线程局部连接（长生命周期；不跨线程传递）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def checkpoint(self) -> None:
        """WAL checkpoint（退出时落盘，architecture.md §2.3）。"""
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
