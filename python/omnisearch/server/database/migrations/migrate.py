"""版本化迁移执行器 + fts_files 动态 DDL（ADR-005）。

- schema 文件按 user_version 顺序执行（短事务逐版本提交）
- fts_files 按运行时 sqlite_version 检测：
    ≥ 3.43  → contentless-delete 表（支持普通 UPDATE/DELETE）
    < 3.43  → 普通 contentless 表（FtsRepository 必须使用 FTS5 special delete command）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from omnisearch.common.database import Database

_MIGRATIONS_DIR = Path(__file__).parent


def _create_fts_files(conn: sqlite3.Connection) -> None:
    """创建 fts_files（rowid = files.id）。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts_files'"
    ).fetchone()
    if exists:
        return
    version = sqlite3.sqlite_version_info
    if version >= (3, 43):
        # contentless-delete：支持普通 UPDATE/DELETE + integrity-check（首选，ADR-005）
        conn.execute(
            """CREATE VIRTUAL TABLE fts_files USING fts5(
                filename, filename_seg, dir_tokens,
                content='', contentless_delete=1,
                tokenize='unicode61')"""
        )
    else:
        # 普通 contentless：必须使用 FTS5 special delete command（兜底，ADR-005）
        conn.execute(
            """CREATE VIRTUAL TABLE fts_files USING fts5(
                filename, filename_seg, dir_tokens,
                contentless, tokenize='unicode61')"""
        )


def migrate(db: Database) -> int:
    """执行未应用的迁移，返回当前 schema 版本。"""
    conn = db.connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        scripts = sorted(_MIGRATIONS_DIR.glob("schema_v*.sql"))
        for script in scripts[version:]:
            conn.executescript(script.read_text(encoding="utf-8"))
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
        if version >= 1:
            _create_fts_files(conn)
        conn.commit()
        return version
    finally:
        conn.close()
