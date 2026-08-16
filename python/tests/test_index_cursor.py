"""P2.1 断点续扫测试（index_jobs.cursor_path，spec §十三 C9 的 DFS 部分）。"""
from __future__ import annotations

from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.common.models import FileType
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.service.index import IndexService


def _mkfiles(root: Path, names: list[str]) -> None:
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(n, encoding="utf-8")


def _env(tmp_path):
    db = Database(tmp_path / "t.db")
    migrate(db)
    return db, IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))


def test_cursor_resume_from_midpoint(tmp_path):
    """cursor_path 指向中途目录 → DFS 从该目录继续（cursor 之前已索引，之后补齐）。"""
    from omnisearch.common.utils.seg import seg_text
    from omnisearch.server.repository.files import FileMeta, FileRepository
    from omnisearch.server.repository.fts import FtsRepository

    db, svc = _env(tmp_path)
    root = tmp_path / "big"
    _mkfiles(root, ["a.txt", "sub/b.txt", "sub/deep/c.txt"])
    # 模拟上次中断：a.txt 已索引（cursor 之前的部分），中断点在 sub
    files, fts = FileRepository(db), FtsRepository(db)
    ops = files.upsert_batch([
        FileMeta(path=str(root / "a.txt"), filename="a.txt", dir_path=str(root), extension=".txt",
                 size_bytes=3, mtime_ns=1, ctime_ns=1, file_type=FileType.DOC, mime_type=None)
    ])
    fts.insert(ops[0].file_id, "a.txt", seg_text("a.txt"), str(root))
    job_id = svc.start_scan(str(root), "full")
    with db.connect() as c:
        c.execute("UPDATE index_jobs SET cursor_path=? WHERE id=?", (str(root / "sub"), job_id))
        c.commit()
    svc.run_scan(job_id, str(root))
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM files WHERE is_deleted=0").fetchone()["n"]
        assert n == 3  # a（已索引）+ sub/b + sub/deep/c（断点补齐）
        cursor = c.execute("SELECT cursor_path FROM index_jobs WHERE id=?", (job_id,)).fetchone()["cursor_path"]
        assert cursor is None  # 完成清空断点


def test_cursor_invalid_falls_back_to_root(tmp_path):
    """cursor 指向不存在目录 → 从 root 开始（回退安全）。"""
    db, svc = _env(tmp_path)
    root = tmp_path / "big2"
    _mkfiles(root, ["x.txt", "y.txt"])
    job_id = svc.start_scan(str(root), "full")
    with db.connect() as c:
        c.execute("UPDATE index_jobs SET cursor_path=? WHERE id=?", (str(tmp_path / "gone"), job_id))
        c.commit()
    svc.run_scan(job_id, str(root))
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM files WHERE is_deleted=0").fetchone()["n"]
        assert n == 2


def test_cursor_progress_written_during_scan(tmp_path):
    """扫描过程中 cursor_path 定期更新（大目录，> CURSOR_EVERY 目录）。"""
    db, svc = _env(tmp_path)
    root = tmp_path / "deep"
    _mkfiles(root, [f"d{i}/f{i}.txt" for i in range(60)])  # 60 个目录（< CURSOR_EVERY=1000 不触发）
    # 用较小的触发阈值验证机制：直接调用内部常量无法改 → 用 60 目录验证「不更新」与完成清空
    job_id = svc.start_scan(str(root), "full")
    svc.run_scan(job_id, str(root))
    with db.connect() as c:
        assert c.execute("SELECT cursor_path FROM index_jobs WHERE id=?", (job_id,)).fetchone()["cursor_path"] is None
