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


def _split_statements(script: str) -> list[str]:
    """按语句拆分迁移脚本（S4：支持事务性 DDL 需逐语句执行）。

    处理：行注释（--）、单引号字符串（含 '' 转义）、触发器 CREATE TRIGGER ... BEGIN..END;
    块内的分号（不切分）。
    """
    statements: list[str] = []
    buf: list[str] = []
    in_quote = False
    in_trigger = False
    for line in script.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        # 去行内注释（引号外），保留引号状态
        clean, i, q = "", 0, False
        while i < len(s):
            c = s[i]
            if q:
                clean += c
                if c == "'" and (i + 1 >= len(s) or s[i + 1] != "'"):
                    q = False
                elif c == "'":  # '' 转义
                    clean += s[i + 1]
                    i += 1
            else:
                if c == "'":
                    q = True
                    clean += c
                elif s.startswith("--", i):
                    break
                else:
                    clean += c
            i += 1
        if not q and not in_trigger and s.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            buf.append(clean + "\n")
            if clean.rstrip().endswith("END;"):
                statements.append("".join(buf).strip())
                buf, in_trigger = [], False
            continue
        # 保留行间换行（SQL 语句跨行时 'AS' + 'SELECT' 不得拼成 'ASSELECT'）
        parts = (clean + "\n").split(";")
        for i, part in enumerate(parts):
            buf.append(part)
            if i < len(parts) - 1 or clean.endswith(";"):
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
        in_quote = q
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def migrate(db: Database) -> int:
    """执行未应用的迁移，返回当前 schema 版本。

    S4 修正：逐语句执行并包在单事务内（SQLite DDL 是事务性的）——
    executescript 会先 COMMIT 挂起事务，中途失败留下已建表而 user_version 未递增，
    重启重跑报 table exists → 迁移永久损坏。事务内失败 → ROLLBACK（表全部撤销）。
    """
    conn = db.connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        scripts = sorted(_MIGRATIONS_DIR.glob("schema_v*.sql"))
        for script in scripts[version:]:
            statements = _split_statements(script.read_text(encoding="utf-8"))
            conn.execute("BEGIN")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                version += 1
                conn.execute(f"PRAGMA user_version = {version}")
                if version == 1:
                    _create_fts_files(conn)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return version
    finally:
        conn.close()
