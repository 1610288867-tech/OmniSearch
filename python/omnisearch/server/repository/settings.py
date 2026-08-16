"""SettingsRepository —— settings KV 表（JSON 值，architecture.md §7.1）。

index_roots 结构（扫描位置管理）：[{path, enabled, created_at}]；
兼容旧格式 list[str]（M5 前）——读取时自动升级并持久化，不新增数据库表。
"""
from __future__ import annotations

import json
import time

from omnisearch.common.database import Database

INDEX_ROOTS_KEY = "index_roots"  # JSON: [{path, enabled, created_at}]


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

    # ---- index roots（扫描位置：Watchdog 监听根 + 扫描目标） ----
    def get_index_roots(self) -> list[dict]:
        """返回 [{path, enabled, created_at}]；旧格式 list[str] 自动升级为 dict 结构。"""
        raw = self.get(INDEX_ROOTS_KEY, [])
        if raw and isinstance(raw[0], str):  # M5 前格式 → 升级
            upgraded = [{"path": p, "enabled": True, "created_at": int(time.time())} for p in raw]
            self.set(INDEX_ROOTS_KEY, upgraded)
            return upgraded
        return [dict(r) for r in raw]

    def set_index_roots(self, roots: list[dict]) -> None:
        self.set(INDEX_ROOTS_KEY, roots)

    def add_index_root(self, path: str, enabled: bool = True) -> list[dict]:
        """追加 root（调用方负责重复/父子校验）；返回最新列表。"""
        roots = self.get_index_roots()
        roots.append({"path": path, "enabled": enabled, "created_at": int(time.time())})
        self.set_index_roots(roots)
        return roots

    def remove_index_root(self, path: str) -> list[dict]:
        """按规范化路径移除 root；返回最新列表。"""
        from omnisearch.common.utils.paths import root_key

        key = root_key(path)
        roots = [r for r in self.get_index_roots() if root_key(r["path"]) != key]
        self.set_index_roots(roots)
        return roots

    def update_index_root(self, path: str, **fields) -> list[dict]:
        """更新单个 root 字段（如 enabled）；返回最新列表。"""
        from omnisearch.common.utils.paths import root_key

        key = root_key(path)
        roots = []
        for r in self.get_index_roots():
            if root_key(r["path"]) == key:
                r.update(fields)
            roots.append(r)
        self.set_index_roots(roots)
        return roots
