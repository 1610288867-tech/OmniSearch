"""SettingsRepository —— settings KV 表（JSON 值，architecture.md §7.1）。"""
from __future__ import annotations

import json

from omnisearch.common.database import Database

INDEX_ROOTS_KEY = "index_roots"  # JSON: list[str]


class SettingsRepository:
    def __init__(self, db: Database):
        self._db = db

    def get(self, key: str, default=None):
        with self._db.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    def set(self, key: str, value) -> None:
        with self._db.connect() as c:
            c.execute(
                """INSERT INTO settings(key, value) VALUES(?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---- index roots（Watchdog 监听根 + 扫描目标） ----
    def get_index_roots(self) -> list[str]:
        return self.get(INDEX_ROOTS_KEY, [])

    def add_index_root(self, path: str) -> list[str]:
        roots = self.get_index_roots()
        if path not in roots:
            roots.append(path)
            self.set(INDEX_ROOTS_KEY, roots)
        return roots
