"""FtsRepository —— FTS5 唯一访问入口（architecture.md §8.2，业务层禁止直接操作 FTS 表）。

- fts_files：contentless-delete（SQLite ≥ 3.43）优先；否则普通 contentless + special delete（ADR-005）
- 四方法：insert / delete / replace / integrity_check
- 查询：match(query, top_k) → [(file_id, bm25)]（搜索正确性经调用方 join files 应用 canonical WHERE）
"""
from __future__ import annotations

import sqlite3

from omnisearch.common.database import Database
from omnisearch.server.repository.files import FileRepository


class FtsRepository:
    def __init__(self, db: Database):
        self._db = db
        self._contentless_delete: bool | None = None

    def _is_contentless_delete(self, c) -> bool:
        """contentless-delete 模式判定（S6：按「实际表定义」而非「当前运行时版本」）。

        原实现用模块级常量 `sqlite3.sqlite_version_info >= (3, 43)`——但表是**建库时**
        按当时版本创建并持久化的；DB 换机器/升级 Python 后运行时版本可能与建表时不同，
        判定漂移会让 contentless（非 contentless_delete）表被误用普通 DELETE → FTS5 报错。
        现从 sqlite_master 读取实际 DDL；表不存在（迁移前）才回退运行时版本判定。
        """
        if self._contentless_delete is None:
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_files'"
            ).fetchone()
            sql = row[0] if row else ""
            if sql:
                self._contentless_delete = "contentless_delete=1" in sql
            else:
                self._contentless_delete = sqlite3.sqlite_version_info >= (3, 43)
        return self._contentless_delete

    # ---------------- 写入（唯一入口） ----------------

    def insert(self, file_id: int, filename: str, filename_seg: str, dir_tokens: str, conn: sqlite3.Connection | None = None) -> None:
        self.insert_batch([(file_id, filename, filename_seg, dir_tokens)], conn)

    def insert_batch(self, rows: list[tuple[int, str, str, str]], conn: sqlite3.Connection | None = None) -> None:
        own = conn is None
        c = conn or self._db.connect()
        try:
            c.executemany(
                "INSERT INTO fts_files(rowid, filename, filename_seg, dir_tokens) VALUES (?,?,?,?)",
                rows,
            )
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    def delete(self, file_id: int, conn: sqlite3.Connection | None = None) -> None:
        own = conn is None
        c = conn or self._db.connect()
        try:
            if self._is_contentless_delete(c):
                c.execute("DELETE FROM fts_files WHERE rowid=?", (file_id,))
            else:
                # 普通 contentless：必须用 FTS5 special delete command（需原始列值）。
                # files 主表只存 filename/dir_path，filename_seg/dir_tokens 需按与写入时
                # 相同的规则重算（files._seg/_dir_tokens）——原查询直接取 files.filename_seg
                # 列（该列不存在）→ 旧版 sqlite 兜底路径实际会崩（S6 回归发现）。
                row = c.execute(
                    "SELECT filename, dir_path FROM files WHERE id=?", (file_id,)
                ).fetchone()
                if row:
                    fname = row["filename"] or ""
                    dtokens = FileRepository._dir_tokens(row["dir_path"] or "")
                    c.execute(
                        """INSERT INTO fts_files(fts_files, rowid, filename, filename_seg, dir_tokens)
                           VALUES('delete', ?, ?, ?, ?)""",
                        (file_id, fname, FileRepository._seg(fname), dtokens),
                    )
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    def replace(self, file_id: int, filename: str, filename_seg: str, dir_tokens: str, conn: sqlite3.Connection | None = None) -> None:
        """文件名/路径更新（rename）后的 FTS 同步。"""
        own = conn is None
        c = conn or self._db.connect()
        try:
            if self._is_contentless_delete(c):
                c.execute(
                    "UPDATE fts_files SET filename=?, filename_seg=?, dir_tokens=? WHERE rowid=?",
                    (filename, filename_seg, dir_tokens, file_id),
                )
            else:
                self.delete(file_id, conn=c)
                c.execute(
                    "INSERT INTO fts_files(rowid, filename, filename_seg, dir_tokens) VALUES (?,?,?,?)",
                    (file_id, filename, filename_seg, dir_tokens),
                )
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    def integrity_check(self) -> list[str]:
        """'integrity-check'：返回不一致列表（空 = 一致）。"""
        with self._db.connect() as c:
            rows = c.execute("INSERT INTO fts_files(fts_files, rank) VALUES('integrity-check', 1)").fetchall()
            return [r["rank"] for r in rows]

    # ---------------- 查询 ----------------

    def match(self, query: str, top_k: int = 100) -> list[tuple[int, float]]:
        """文件名通道：fts_files MATCH → bm25 → topK（M1，rowid = files.id）。

        返回 [(file_id, bm25_score)]；过滤（is_deleted=0）由 SearchService 回表 join 完成。
        """
        if not query or not query.strip():
            return []
        with self._db.connect() as c:
            rows = c.execute(
                """SELECT rowid AS file_id, bm25(fts_files) AS score
                   FROM fts_files WHERE fts_files MATCH ?
                   ORDER BY score LIMIT ?""",
                (query, top_k),
            ).fetchall()
            return [(r["file_id"], r["score"]) for r in rows]

    def body_match(self, query: str, top_k: int = 100) -> list[tuple[int, int, float, str]]:
        """正文/OCR 通道（M2+M3）：fts_body MATCH → join chunks 拿 file_id + source_type → topK。

        返回 [(chunk_id, file_id, bm25_score, source_type)]；
        过滤（is_deleted=0）由 SearchService 回表 join 完成。
        """
        if not query or not query.strip():
            return []
        with self._db.connect() as c:
            rows = c.execute(
                """SELECT b.rowid AS chunk_id, c.file_id AS file_id, bm25(fts_body) AS score,
                          c.source_type AS source_type
                   FROM fts_body b JOIN chunks c ON c.id = b.rowid
                   WHERE fts_body MATCH ?
                   ORDER BY score LIMIT ?""",
                (query, top_k),
            ).fetchall()
            return [(r["chunk_id"], r["file_id"], r["score"], r["source_type"]) for r in rows]
