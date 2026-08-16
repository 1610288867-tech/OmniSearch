"""WatchService 测试（M1 阶段 3/4：architecture.md §11.3/§11.4）。

真实 watchdog 事件 + 轮询断言；防抖参数调短以加速测试。
覆盖：CREATE / MODIFY / DELETE / RENAME（保留 file_id）/ conflict / CREATE+DELETE 忽略 / CREATE+MODIFY 合并。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.watch import WatchService


def _wait_until(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def watch_env(db, tmp_path):
    """IndexService + WatchService 组合（防抖 0.5s）。"""
    root = tmp_path / "watch_root"
    root.mkdir()
    svc = IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))
    watch = WatchService(
        on_changes=svc.handle_changes,
        on_deleted_paths=lambda paths: [svc.handle_delete_path(p) for p in paths],
        on_renamed=svc.handle_rename,
        debounce_s=0.5,
    )
    watch.start([str(root)])
    yield db, svc, watch, root
    watch.stop()


def _paths(db):
    conn = db.connect()
    rows = conn.execute("SELECT path, filename, is_deleted FROM files").fetchall()
    conn.close()
    return rows


def test_watch_create(watch_env):
    db, svc, watch, root = watch_env
    (root / "new.txt").write_text("hello", encoding="utf-8")
    assert _wait_until(lambda: any(r["filename"] == "new.txt" for r in _paths(db)))
    assert _wait_until(lambda: FtsRepository(db).match("new"))


def test_watch_modify_mtime_size(watch_env):
    db, svc, watch, root = watch_env
    f = root / "m.txt"
    f.write_text("v1", encoding="utf-8")
    assert _wait_until(lambda: any(r["filename"] == "m.txt" for r in _paths(db)))
    old = db.connect().execute("SELECT size_bytes FROM files WHERE filename='m.txt'").fetchone()["size_bytes"]
    f.write_text("v1 much longer content", encoding="utf-8")
    assert _wait_until(
        lambda: db.connect().execute("SELECT size_bytes FROM files WHERE filename='m.txt'").fetchone()["size_bytes"] != old
    )


def test_watch_delete(db, tmp_path):
    """删除同步：is_deleted=1 先行 → FTS 清理（canonical 排除，不依赖清理）。"""
    root = tmp_path / "wdel"
    root.mkdir()
    svc = IndexService(db, FileRepository(db), FtsRepository(db), IndexJobRepository(db))
    watch = WatchService(
        on_changes=svc.handle_changes,
        on_deleted_paths=lambda paths: [svc.handle_delete_path(p) for p in paths],
        on_renamed=svc.handle_rename,
        debounce_s=0.5,
    )
    watch.start([str(root)])
    try:
        f = root / "gone.txt"
        f.write_text("x", encoding="utf-8")
        assert _wait_until(lambda: any(r["filename"] == "gone.txt" for r in _paths(db)))
        f.unlink()
        assert _wait_until(
            lambda: any(r["filename"] == "gone.txt" and r["is_deleted"] == 1 for r in _paths(db))
        )
        # FTS 清理（异步，最终一致）
        assert _wait_until(lambda: FtsRepository(db).match("gone") == [])
    finally:
        watch.stop()


def test_watch_rename_keeps_file_id(watch_env):
    db, svc, watch, root = watch_env
    f = root / "old.txt"
    f.write_text("x", encoding="utf-8")
    assert _wait_until(lambda: any(r["filename"] == "old.txt" for r in _paths(db)))
    fid = db.connect().execute("SELECT id FROM files WHERE filename='old.txt'").fetchone()["id"]

    f.rename(root / "new.txt")
    assert _wait_until(lambda: any(r["filename"] == "new.txt" for r in _paths(db)))
    row = db.connect().execute("SELECT id, path, is_deleted FROM files WHERE filename='new.txt'").fetchone()
    assert row["id"] == fid       # 保留 file_id（architecture.md §11.4）
    assert row["is_deleted"] == 0
    assert _wait_until(lambda: FtsRepository(db).match("new"))  # fts 同步（旧词清理）
    assert FtsRepository(db).match("old") == []


def test_watch_rename_conflict_rescans(watch_env):
    """目标 path 已存在（不同内容）：不覆盖、不删除目标记录；source 消失 → 删除（重新扫描语义）。

    注：目标与源 stat 不同（不同 size）+ 目标有 AI 产物 → 真 conflict（P2.2 合并判定排除）。
    """
    db, svc, watch, root = watch_env
    a = root / "a.txt"
    b = root / "b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B" * 8, encoding="utf-8")  # 与 a 不同 size（stat 不同 → 真 conflict）
    assert _wait_until(lambda: len([r for r in _paths(db) if not r["is_deleted"]]) == 2)
    b_fid = db.connect().execute("SELECT id FROM files WHERE filename='b.txt'").fetchone()["id"]
    with db.connect() as c:  # b 为真实文件（有 AI 产物，防误合并）
        c.execute(
            "INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg) "
            "VALUES (?, 'doc_chunk', 0, 'b', 'b')",
            (b_fid,),
        )
        c.commit()

    os.replace(a, b)  # 覆盖式重命名（Windows os.rename 会失败，用 os.replace）
    # conflict：b 记录不得被覆盖删除；a 消失 → is_deleted=1
    assert _wait_until(lambda: any(r["filename"] == "a.txt" and r["is_deleted"] == 1 for r in _paths(db)))
    assert _wait_until(lambda: len([r for r in _paths(db) if r["filename"] == "b.txt" and not r["is_deleted"]]) == 1)
    assert db.connect().execute("SELECT id FROM files WHERE filename='b.txt'").fetchone()["id"] == b_fid


def test_watch_create_delete_ignored(watch_env):
    """CREATE + DELETE 合并 → 忽略（临时文件场景）。"""
    db, svc, watch, root = watch_env
    f = root / "temp.tmp"
    f.write_text("x", encoding="utf-8")
    f.unlink()
    time.sleep(2.0)  # 等防抖窗口过去
    assert not any(r["filename"] == "temp.tmp" for r in _paths(db))


def test_watch_create_modify_merged(watch_env):
    """CREATE + MODIFY 合并：最终一条记录、元数据为最终值（mtime_ns+size 检测）。"""
    db, svc, watch, root = watch_env
    f = root / "burst.txt"
    f.write_text("v1", encoding="utf-8")
    time.sleep(0.2)
    f.write_text("v2-final-content", encoding="utf-8")
    assert _wait_until(lambda: any(r["filename"] == "burst.txt" for r in _paths(db)))
    size = db.connect().execute("SELECT size_bytes FROM files WHERE filename='burst.txt'").fetchone()["size_bytes"]
    assert size == os.path.getsize(f)  # 最终值生效（合并未丢更新）
