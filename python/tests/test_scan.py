"""全量扫描测试（M1 阶段 1：architecture.md §11.1）。

覆盖：空目录 / 普通文件 / 嵌套目录 / 系统目录过滤 / 黑名单扩展名 / 中文路径 / 复活规则。
"""
from __future__ import annotations

import os
from pathlib import Path

from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.service.index import IndexService


def _make_index_service(db):
    return IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))


def _make_tree(root: Path) -> None:
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "photo.JPG").write_bytes(b"\xff\xd8\xff")
    nested = root / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "报告.md").write_text("# 报告", encoding="utf-8")
    (nested / "notes.pdf").write_bytes(b"%PDF-1.4")
    (root / "sub" / "data.csv").write_text("a,b", encoding="utf-8")


def test_scan_basic(db, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _make_tree(root)
    svc = _make_index_service(db)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    conn = db.connect()
    rows = conn.execute("SELECT path, filename, file_type, extension FROM files WHERE is_deleted=0").fetchall()
    paths = {r["path"] for r in rows}
    assert len(rows) == 5
    assert str(root / "a.txt") in paths
    assert str(nested := root / "sub" / "deep" / "报告.md") in paths  # 中文路径
    by_type = {r["path"]: r["file_type"] for r in rows}
    assert by_type[str(root / "photo.JPG")] == "image"   # 扩展名大小写不敏感
    assert by_type[str(root / "sub" / "deep" / "notes.pdf")] == "doc"
    assert by_type[str(root / "sub" / "data.csv")] == "doc"
    conn.close()


def test_scan_empty_dir(db, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    svc = _make_index_service(db)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))
    job = db.connect().execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
    assert job["status"] == "DONE" and job["total_files"] == 0


def test_scan_filters_system_dirs_and_blacklist_ext(db, tmp_path):
    root = tmp_path / "tree2"
    (root / "Windows").mkdir(parents=True)
    (root / "Windows" / "hidden.dll").write_bytes(b"x")
    (root / "AppData").mkdir()
    (root / "AppData" / "cache.tmp").write_text("x")
    (root / "keep.txt").write_text("ok")
    svc = _make_index_service(db)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    conn = db.connect()
    paths = {r["path"] for r in conn.execute("SELECT path FROM files WHERE is_deleted=0").fetchall()}
    assert str(root / "keep.txt") in paths
    assert str(root / "Windows" / "hidden.dll") not in paths   # 系统目录过滤
    assert str(root / "AppData" / "cache.tmp") not in paths    # 黑名单扩展名
    conn.close()


def test_scan_delete_sync(db, tmp_path):
    """扫描比对：磁盘消失的文件 → is_deleted=1 + FTS 清理（搜索立即排除）。"""
    root = tmp_path / "tree3"
    root.mkdir()
    (root / "gone.txt").write_text("x")
    (root / "stay.txt").write_text("y")
    svc = _make_index_service(db)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    os.remove(root / "gone.txt")
    job_id2 = svc.start_scan(str(root))
    svc.run_scan(job_id2, str(root))

    conn = db.connect()
    gone = conn.execute("SELECT is_deleted FROM files WHERE filename='gone.txt'").fetchone()
    assert gone and gone["is_deleted"] == 1
    # FTS 已清理：搜索 gone 无结果
    assert FtsRepository(db).match("gone") == []
    conn.close()


def test_scan_resurrect_same_path(db, tmp_path):
    """同路径复活：is_deleted=1 的 path 重新出现 → 复用 file_id（architecture.md §3）。"""
    root = tmp_path / "tree4"
    root.mkdir()
    f = root / "revive.txt"
    f.write_text("v1")
    svc = _make_index_service(db)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    fid = db.connect().execute("SELECT id FROM files WHERE filename='revive.txt'").fetchone()["id"]
    os.remove(f)
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    f.write_text("v2")
    job_id = svc.start_scan(str(root))
    svc.run_scan(job_id, str(root))

    conn = db.connect()
    row = conn.execute("SELECT id, is_deleted, status FROM files WHERE filename='revive.txt'").fetchone()
    assert row["id"] == fid          # 复用原 file_id
    assert row["is_deleted"] == 0
    assert db.connect().execute("SELECT count(*) c FROM files").fetchone()["c"] == 1
    conn.close()


def test_writer_failure_marks_job_failed(tmp_path, monkeypatch):
    """S1：writer 写入异常 → job FAILED（不永久挂起）。"""
    from omnisearch.common.config import db_path as _dbp
    import os
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    from omnisearch.common.database import Database
    from omnisearch.server.database.migrations.migrate import migrate
    from omnisearch.server.repository.files import FileRepository
    from omnisearch.server.repository.fts import FtsRepository
    from omnisearch.server.repository.jobs import IndexJobRepository
    from omnisearch.server.service.index import IndexService

    db = Database(_dbp(tmp_path))
    migrate(db)
    root = tmp_path / "big"
    root.mkdir()
    for i in range(30):
        (root / f"f{i}.txt").write_text("x" * 10, encoding="utf-8")
    svc = IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))

    def boom(*a, **k):
        raise RuntimeError("write failure")

    monkeypatch.setattr(svc, "_flush_batch", boom)
    job_id = svc.start_scan(str(root), "full")
    svc.run_scan(job_id, str(root))  # 不应挂起
    with db.connect() as c:
        assert c.execute("SELECT status FROM index_jobs WHERE id=?", (job_id,)).fetchone()["status"] == "FAILED"
    os.environ.pop("OMNISEARCH_DEV_DATA", None)
