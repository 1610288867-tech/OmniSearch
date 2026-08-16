"""Worker document pipeline 集成测试（M2：architecture.md §10.3/§11.5）。

覆盖：doc 入队 → claim → 处理 → chunks/fts_body 可搜 / reindex 替换 /
处理失败旧数据保留 / image 不入队。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from omnisearch.common.database import Database
from omnisearch.common.models import FileStatus, SourceType, TaskStatus
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.tasks import TaskRepository
from omnisearch.server.service.index import IndexService
from omnisearch.common.utils.seg import fts_query_for
from omnisearch.worker.pipeline.processor import process_doc_file
from omnisearch.worker.task.queue import TaskQueue

from omnisearch.common.models import FileType  # noqa: E402


def _meta(path: str, mtime: int = 1, size: int = 1) -> FileMeta:
    return FileMeta(
        path=path, filename=Path(path).name, dir_path=str(Path(path).parent),
        extension=Path(path).suffix.lower(), size_bytes=size, mtime_ns=mtime, ctime_ns=1,
        file_type=FileType.DOC if Path(path).suffix in {".txt", ".md", ".pdf"} else FileType.IMAGE,
        mime_type=None,
    )


@pytest.fixture()
def env(db):
    """IndexService（含入队）+ TaskQueue + repos。"""
    files = FileRepository(db)
    fts = FtsRepository(db)
    jobs = IndexJobRepository(db)
    tasks = TaskRepository(db)
    index = IndexService(db, files, fts, jobs, tasks)
    return db, files, fts, tasks, index, TaskQueue(db)


def test_doc_enqueue_and_process(db, tmp_path, env):
    """doc 文件：扫描入队 → claim → 处理 → chunks 落库 + fts_body 可搜 + AI_DONE。"""
    db, files, fts, tasks, index, queue = env  # noqa: F841
    doc = tmp_path / "doc.txt"
    doc.write_text("机器学习是人工智能的一个重要分支。", encoding="utf-8")

    index.handle_changes([str(doc)])  # 增量：入队（priority=0）
    assert tasks.count_by_status(TaskStatus.PENDING) == 1

    claimed = queue.claim_batch()
    assert len(claimed) == 1
    fid = db.connect().execute("SELECT file_id FROM ai_tasks WHERE id=?", (claimed[0],)).fetchone()["file_id"]
    process_doc_file(db, fid, str(doc))
    queue.complete(claimed[0])

    conn = db.connect()
    row = conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()
    assert row["status"] == FileStatus.AI_DONE.value
    chunks = conn.execute(
        "SELECT chunk_text, embedding_status FROM chunks WHERE file_id=?", (fid,)
    ).fetchall()
    assert len(chunks) == 1
    assert chunks[0]["embedding_status"] == 0  # PENDING（M4 向量化）
    conn.close()
    # 正文可搜（jieba 分词一致性）
    assert FtsRepository(db).body_match(fts_query_for("机器学习"))


def test_reindex_replaces_chunks(db, tmp_path, env):
    """重新索引：内容变化 → status 回退 → 重新处理 → 新内容可搜、旧内容不可搜。"""
    db, files, fts, tasks, index, queue = env  # noqa: F841
    doc = tmp_path / "re.txt"
    doc.write_text("旧版内容：关于财务报表。", encoding="utf-8")
    index.handle_changes([str(doc)])
    claimed = queue.claim_batch()
    fid = db.connect().execute("SELECT file_id FROM ai_tasks WHERE id=?", (claimed[0],)).fetchone()["file_id"]
    process_doc_file(db, fid, str(doc))
    queue.complete(claimed[0])
    assert FtsRepository(db).body_match(fts_query_for("财务报表"))

    # 修改内容（mtime+size 变化）→ 再次增量处理 → 重新入队（partial unique 允许：旧任务已 SUCCESS）
    time.sleep(0.01)
    doc.write_text("新版内容：关于量子计算。", encoding="utf-8")
    index.handle_changes([str(doc)])
    assert tasks.count_by_status(TaskStatus.PENDING) == 1
    claimed = queue.claim_batch()
    process_doc_file(db, fid, str(doc))
    queue.complete(claimed[0])

    assert FtsRepository(db).body_match(fts_query_for("量子计算"))
    assert FtsRepository(db).body_match(fts_query_for("财务报表")) == []  # 旧 chunks 已替换


def test_process_failure_keeps_old_data(db, tmp_path, env):
    """处理失败：旧 chunks 保留、task=FAILED（「重新索引失败时，旧搜索能力必须保持可用」）。"""
    db, files, fts, tasks, index, queue = env  # noqa: F841
    doc = tmp_path / "fail.pdf"
    doc.write_bytes(b"%PDF-1.4 ok")  # 占位（先成功处理？无法成功——直接构造旧数据）
    # 预置：直接插入一个已处理文件（模拟旧索引存在）
    fid = _insert_file(db, str(doc))
    _seed_chunks(db, fid, "旧的可靠正文内容")

    # 损坏文件被重新处理 → 提取失败
    doc.write_bytes(b"garbage not pdf")
    index.handle_changes([str(doc)])
    claimed = queue.claim_batch()
    assert len(claimed) == 1
    from omnisearch.worker.pipeline.processor import process_doc_file as p

    with pytest.raises(Exception):
        p(db, fid, str(doc))
    queue.fail(claimed[0], "extract failed")

    # 旧 chunks 与 FTS 保留
    assert FtsRepository(db).body_match(fts_query_for("旧的可靠正文"))
    conn = db.connect()
    assert conn.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (fid,)).fetchone()["n"] == 1
    row = conn.execute("SELECT status FROM ai_tasks WHERE id=?", (claimed[0],)).fetchone()
    assert row["status"] == "FAILED"
    conn.close()


def test_image_enqueued_for_ocr(db, tmp_path, env):
    """M3：image 文件入队（OCR pipeline 消费）。"""
    db, files, fts, tasks, index, queue = env  # noqa: F841
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0")
    index.handle_changes([str(img)])
    assert tasks.count_by_status(TaskStatus.PENDING) == 1


def test_duplicate_enqueue_skipped(db, tmp_path, env):
    """partial unique index：同文件活跃任务存在时重复入队被跳过。"""
    db, files, fts, tasks, index, queue = env  # noqa: F841
    doc = tmp_path / "dup.txt"
    doc.write_text("x", encoding="utf-8")
    index.handle_changes([str(doc)])
    index.handle_changes([str(doc)])  # 再次触发（任务仍 PENDING）
    assert tasks.count_by_status(TaskStatus.PENDING) == 1


def _insert_file(db: Database, path: str) -> int:
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type, status)
               VALUES (?, ?, '', '.pdf', 1, 1, 'doc', 'AI_DONE')""",
            (path, Path(path).name),
        )
        return cur.lastrowid


def _seed_chunks(db: Database, file_id: int, text: str) -> None:
    from omnisearch.common.utils.seg import seg_text

    with db.connect() as c:
        c.execute(
            """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
               VALUES (?, ?, 0, ?, ?)""",
            (file_id, SourceType.DOC_CHUNK.value, text, seg_text(text)),
        )
        c.execute(
            "UPDATE files SET status='AI_DONE' WHERE id=?", (file_id,),
        )
