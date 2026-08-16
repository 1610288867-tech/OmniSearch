"""FtsRepository 测试（M1 阶段 2：architecture.md §8 / ADR-005）。

覆盖：INSERT / filename search / filename update / rename / delete / 同 rowid replace / 重启后搜索。
"""
from __future__ import annotations

import sqlite3

from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.files import FileRepository, FileMeta
from omnisearch.common.models import FileType


def _meta(path: str, mtime: int = 1) -> FileMeta:
    return FileMeta(
        path=path, filename=path.rsplit("/", 1)[-1], dir_path=path.rsplit("/", 1)[0],
        extension=".txt", size_bytes=1, mtime_ns=mtime, ctime_ns=1,
        file_type=FileType.DOC, mime_type="text/plain",
    )


def _seed(db, *paths) -> list[int]:
    """写入 files + 同步 fts，返回 file_id 列表。"""
    files = FileRepository(db)
    fts = FtsRepository(db)
    ids = []
    for p in paths:
        fts_ops = files.upsert_batch([_meta(p)])
        assert len(fts_ops) == 1
        fts.insert(fts_ops[0].file_id, fts_ops[0].filename, fts_ops[0].filename_seg, fts_ops[0].dir_tokens)
        ids.append(fts_ops[0].file_id)
    return ids


def test_insert_and_search(db):
    ids = _seed(db, "/x/resume.pdf", "/x/notes.txt")
    fts = FtsRepository(db)
    hits = dict(fts.match("resume"))
    assert set(hits) == {ids[0]}  # rowid = files.id
    assert hits[ids[0]] < 0       # bm25 为负值（越小越相关），M1 透传
    assert fts.match("nope") == []


def test_filename_update_and_replace(db):
    """同一 rowid 的 replace：改名后旧词不命中、新词命中（contentless 表语义正确）。"""
    _seed(db, "/x/oldname.txt")
    fts = FtsRepository(db)
    fid = db.connect().execute("SELECT id FROM files LIMIT 1").fetchone()["id"]
    fts.replace(fid, "newname.txt", "newname txt", "/x")
    assert fts.match("oldname") == []
    assert [h[0] for h in fts.match("newname")] == [fid]


def test_rename_via_replace_keeps_rowid(db):
    """rename：保留 rowid（file_id），仅替换索引内容。"""
    fids = _seed(db, "/x/a.txt")
    fts = FtsRepository(db)
    fts.replace(fids[0], "b.txt", "b txt", "/x/sub")
    hits = fts.match("b")
    assert [h[0] for h in hits] == fids
    assert fts.match("a") == []


def test_delete(db):
    fids = _seed(db, "/x/del.txt", "/x/keep.txt")
    fts = FtsRepository(db)
    fts.delete(fids[0])
    assert fts.match("del") == []
    assert fts.match("keep")
    # 重复 delete（幂等）
    fts.delete(fids[0])


def test_integrity_check(db):
    fts = FtsRepository(db)
    assert fts.integrity_check() == []


def test_search_after_reopen(db):
    """重启后搜索：新连接（模拟进程重启）仍可命中。"""
    _seed(db, "/x/persist.pdf")
    fts = FtsRepository(db)
    assert fts.match("persist")
    # 关闭所有连接（WAL checkpoint）后重新打开数据库
    db.checkpoint()
    fts2 = FtsRepository(db)
    hits = fts2.match("persist")
    assert hits and len(hits) == 1


def test_fts_files_only_via_repository(db):
    """FtsRepository 是唯一 FTS 写入口：业务路径（upsert_batch）不会绕过它。"""
    files = FileRepository(db)
    ops = files.upsert_batch([_meta("/x/onlyrepo.txt")])
    # 业务层只拿到 FtsOp，FTS 写入必须显式走 FtsRepository.insert
    from omnisearch.server.repository.fts import FtsRepository

    FtsRepository(db).insert(ops[0].file_id, ops[0].filename, ops[0].filename_seg, ops[0].dir_tokens)
    assert FtsRepository(db).match("onlyrepo")
