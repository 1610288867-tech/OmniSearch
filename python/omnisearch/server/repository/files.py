"""FileRepository —— files 表访问（事实数据源，architecture.md §3/§7.1）。

- upsert_batch：批量写入，含「同路径复活」规则（path 已存在且 is_deleted=1 → 复活原记录）
- mark_deleted：软删除（is_deleted=1 先行，搜索立即可排除）
- 事务由调用方（Service 层）管理：方法接受可选 conn（同一事务内与 FtsRepository 保持原子）
"""
from __future__ import annotations

import platform
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.common.models import FileStatus, FileType
from omnisearch.common.utils.paths import normalize

FTS_COLUMNS = ("filename", "filename_seg", "dir_tokens")

# S5：NTFS/exFAT 等大小写不敏感 FS —— case-only rename 后磁盘以新大小写出现，
# BINARY 精确匹配找不到旧行 → 会新建「同文件第二个活跃行」。仅在未命中时做一次
# NOCASE 兜底查询（热路径仍走 path 索引，零开销）；非 Windows（区分大小写 FS）不启用。
_CASE_INSENSITIVE_FS = platform.system() == "Windows"


@dataclass(frozen=True)
class FtsOp:
    """一次 FTS 同步操作：op = 'insert'（新/复活）| 'replace'（改名/移动）。"""

    op: str
    file_id: int
    filename: str
    filename_seg: str
    dir_tokens: str


@dataclass(frozen=True)
class FileMeta:
    """一次扫描得到的文件元数据（M1 字段集，architecture.md §11.1）。"""

    path: str
    filename: str
    dir_path: str
    extension: str
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    file_type: FileType
    mime_type: str | None


class FileRepository:
    def __init__(self, db: Database):
        self._db = db

    def upsert_batch(
        self, metas: list[FileMeta], conn: sqlite3.Connection | None = None
    ) -> list[FtsOp]:
        """批量写入 files；返回需同步 fts_files 的操作（insert=新/复活，replace=改名/移动）。

        规则（architecture.md §3/§11.3）：
        - path 不存在 → INSERT（新 file_id）
        - path 已存在且 is_deleted=1 → 复活原记录（不新建 file_id）
        - path 已存在且活跃 → UPDATE 元数据（filename/dir_path 变化时需 fts replace）
        """
        own = conn is None
        c = conn or self._db.connect()
        fts_ops: list[FtsOp] = []
        try:
            for m in metas:
                norm_path = normalize(m.path)  # 统一分隔符（事实数据源唯一写入规范）
                row = c.execute(
                    "SELECT id, filename, dir_path, is_deleted, size_bytes, mtime_ns, status, path FROM files WHERE path = ?",
                    (norm_path,),
                ).fetchone()
                if row is None and _CASE_INSENSITIVE_FS:
                    # S5：case-only rename 的兜底——大小写不敏感命中既有行（同路径复活/更新），
                    # 并把 path 规范为磁盘当前大小写（消除重复活跃行 + 展示一致性）。
                    row = c.execute(
                        "SELECT id, filename, dir_path, is_deleted, size_bytes, mtime_ns, status, path "
                        "FROM files WHERE path = ? COLLATE NOCASE LIMIT 1",
                        (norm_path,),
                    ).fetchone()
                    if row is not None and row["path"] != norm_path:
                        c.execute(
                            "UPDATE files SET path=?, updated_at=unixepoch() WHERE id=?",
                            (norm_path, row["id"]),
                        )
                        row = dict(row)
                        row["path"] = norm_path
                if row is None:
                    cur = c.execute(
                        """INSERT INTO files (path, filename, dir_path, extension, size_bytes,
                                              mtime_ns, ctime_ns, file_type, mime_type, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (norm_path, m.filename, m.dir_path, m.extension, m.size_bytes,
                         m.mtime_ns, m.ctime_ns, m.file_type.value, m.mime_type, FileStatus.METADATA_ONLY.value),
                    )
                    fts_ops.append(FtsOp("insert", cur.lastrowid, m.filename, self._seg(m.filename), self._dir_tokens(m.dir_path)))
                elif row["is_deleted"]:
                    # 同路径复活：复用 file_id，状态重置；FTS 索引此前已随删除清理 → insert
                    c.execute(
                        """UPDATE files SET filename=?, dir_path=?, extension=?, size_bytes=?,
                                            mtime_ns=?, ctime_ns=?, file_type=?, mime_type=?,
                                            is_deleted=0, status=?, updated_at=unixepoch()
                           WHERE id=?""",
                        (m.filename, m.dir_path, m.extension, m.size_bytes,
                         m.mtime_ns, m.ctime_ns, m.file_type.value, m.mime_type,
                         FileStatus.METADATA_ONLY.value, row["id"]),
                    )
                    fts_ops.append(FtsOp("insert", row["id"], m.filename, self._seg(m.filename), self._dir_tokens(m.dir_path)))
                else:
                    # 更新元数据；filename/dir_path 变化 → fts replace（rename/移动）
                    # content 变化（size/mtime）→ status 回退 METADATA_ONLY（触发重新 AI 处理，architecture.md §11.2）
                    content_changed = m.size_bytes != row["size_bytes"] or m.mtime_ns != row["mtime_ns"]
                    new_status = (
                        FileStatus.METADATA_ONLY.value
                        if content_changed and row["status"] == FileStatus.AI_DONE.value
                        else row["status"]
                    )
                    c.execute(
                        """UPDATE files SET size_bytes=?, mtime_ns=?, ctime_ns=?, file_type=?,
                                            mime_type=?, status=?, updated_at=unixepoch()
                           WHERE id=?""",
                        (m.size_bytes, m.mtime_ns, m.ctime_ns, m.file_type.value, m.mime_type, new_status, row["id"]),
                    )
                    if row["filename"] != m.filename or row["dir_path"] != m.dir_path:
                        c.execute(
                            "UPDATE files SET filename=?, dir_path=?, updated_at=unixepoch() WHERE id=?",
                            (m.filename, m.dir_path, row["id"]),
                        )
                        fts_ops.append(FtsOp("replace", row["id"], m.filename, self._seg(m.filename), self._dir_tokens(m.dir_path)))
            if own:
                c.commit()
            return fts_ops
        except Exception:
            if own:
                c.rollback()
            raise
        finally:
            if own:
                c.close()

    def mark_deleted(self, file_id: int, conn: sqlite3.Connection | None = None) -> None:
        """软删除（立即从 canonical WHERE 排除；FTS cleanup 由调用方异步执行）。"""
        own = conn is None
        c = conn or self._db.connect()
        try:
            c.execute(
                "UPDATE files SET is_deleted=1, updated_at=unixepoch() WHERE id=? AND is_deleted=0",
                (file_id,),
            )
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    def mark_deleted_by_paths(self, paths: list[str], conn: sqlite3.Connection | None = None) -> list[int]:
        """按 path 批量软删除，返回受影响 file_id（搜索排除不依赖 FTS/Qdrant 清理）。"""
        own = conn is None
        c = conn or self._db.connect()
        try:
            ids = []
            for p in paths:
                norm_path = normalize(p)
                row = c.execute(
                    "SELECT id FROM files WHERE path=? AND is_deleted=0", (norm_path,)
                ).fetchone()
                if row is None and _CASE_INSENSITIVE_FS:
                    # S5：大小写变体路径的删除也要命中（case-only rename 后删除事件带新大小写）
                    row = c.execute(
                        "SELECT id FROM files WHERE path=? COLLATE NOCASE AND is_deleted=0 LIMIT 1",
                        (norm_path,),
                    ).fetchone()
                if row:
                    c.execute("UPDATE files SET is_deleted=1, updated_at=unixepoch() WHERE id=?", (row["id"],))
                    ids.append(row["id"])
            if own:
                c.commit()
            return ids
        finally:
            if own:
                c.close()

    def get_by_path(self, path: str) -> sqlite3.Row | None:
        with self._db.connect() as c:
            return c.execute("SELECT * FROM files WHERE path=?", (normalize(path),)).fetchone()

    def get_active_paths(self, root: str) -> list[str]:
        """root 下未删除文件的 path 列表（扫描比对用）。"""
        prefix = root.rstrip("\\/") + "\\"
        with self._db.connect() as c:
            rows = c.execute(
                "SELECT path FROM files WHERE is_deleted=0 AND (path=? OR path LIKE ?)",
                (root, prefix + "%"),
            ).fetchall()
            return [r["path"] for r in rows]

    @staticmethod
    def _seg(filename: str) -> str:
        from omnisearch.common.utils.seg import seg_text

        return seg_text(filename)

    @staticmethod
    def _dir_tokens(dir_path: str) -> str:
        # 路径按分隔符拆目录片段（fts_files.dir_tokens 供目录检索，M2 启用）
        parts = [p for p in Path(dir_path).parts if p]
        return " ".join(parts)
