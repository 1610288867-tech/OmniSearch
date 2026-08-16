"""图片 OCR 搜索测试（M3：architecture.md §10.4/§11.5）。

真实 PaddleOCR 推理（paddle 2.6.2 + paddleocr 2.8.1 冻结组合）。
覆盖 12 项：中/英/混合 OCR、无文字、OCR 失败、重新识别、chunk replace、
旧数据保护、FTS rebuild、图片删除、Worker FAILED、重启后搜索。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from omnisearch.common.database import Database
from omnisearch.common.models import FileStatus, SourceType, TaskStatus
from omnisearch.common.utils.seg import fts_query_for
from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.tasks import TaskRepository
from omnisearch.server.service.index import IndexService
from omnisearch.worker.pipeline.ocr import OcrError, ocr_image
from omnisearch.worker.pipeline.processor import process_image_file
from omnisearch.worker.task.queue import TaskQueue

_FONT = "C:/Windows/Fonts/msyh.ttc"


def _make_image(path: Path, text: str) -> None:
    """生成白底黑字测试图片。"""
    img = Image.new("RGB", (400, 100), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT, 28)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 30), text, fill="black", font=font)
    img.save(str(path))


def _blank_image(path: Path) -> None:
    Image.new("RGB", (300, 100), "white").save(str(path))


@pytest.fixture(scope="module")
def ocr_ready():
    """预热 OCR 引擎（模型加载一次，后续测试共享）。"""
    from omnisearch.worker.pipeline.ocr import _get_engine

    _get_engine()
    yield


@pytest.fixture()
def env(db):
    files = FileRepository(db)
    fts = FtsRepository(db)
    jobs = IndexJobRepository(db)
    tasks = TaskRepository(db)
    index = IndexService(db, files, fts, jobs, tasks)
    return db, files, fts, tasks, index, TaskQueue(db)


def _process_enqueued(env, path: str) -> int:
    """增量入队 → claim → 处理 → 返回 file_id。"""
    db, files, fts, tasks, index, queue = env
    index.handle_changes([path])
    claimed = queue.claim_batch()
    assert len(claimed) == 1
    fid = db.connect().execute(
        "SELECT file_id FROM ai_tasks WHERE id=?", (claimed[0],)
    ).fetchone()["file_id"]
    process_image_file(db, fid, path)
    queue.complete(claimed[0])
    return fid


def test_chinese_ocr(env, tmp_path, ocr_ready):
    """1. 中文 OCR：识别 → chunks(ocr) → 搜索命中（matched_in=ocr）。"""
    img = tmp_path / "zh.jpg"
    _make_image(img, "会议记录第3页")
    _process_enqueued(env, str(img))

    db = env[0]
    row = db.connect().execute(
        "SELECT text, lang, confidence FROM ocr_text"
    ).fetchone()
    assert row and "会议记录" in row["text"]
    assert row["lang"] == "zh+en" and row["confidence"] > 0
    chunk = db.connect().execute(
        "SELECT source_type, chunk_index, embedding_status FROM chunks"
    ).fetchone()
    assert chunk["source_type"] == SourceType.OCR.value
    assert chunk["chunk_index"] == 0 and chunk["embedding_status"] == 0
    assert FtsRepository(db).body_match(fts_query_for("会议记录"))


def test_english_ocr(env, tmp_path, ocr_ready):
    """2. 英文 OCR：New York 2026 → 搜索命中。"""
    img = tmp_path / "en.jpg"
    _make_image(img, "New York 2026")
    _process_enqueued(env, str(img))
    assert FtsRepository(env[0]).body_match(fts_query_for("New York"))


def test_mixed_ocr(env, tmp_path, ocr_ready):
    """3. 中英混合。"""
    img = tmp_path / "mix.jpg"
    _make_image(img, "AI 人工智能 2026")
    _process_enqueued(env, str(img))
    db = env[0]
    text = db.connect().execute("SELECT text FROM ocr_text").fetchone()["text"]
    assert "AI" in text and "人工智能" in text


def test_blank_image(env, tmp_path, ocr_ready):
    """4. 无文字图片：SUCCESS + 无 ocr chunk（搜索无意义不插入）。"""
    img = tmp_path / "blank.jpg"
    _blank_image(img)
    _process_enqueued(env, str(img))
    db = env[0]
    assert db.connect().execute("SELECT count(*) n FROM chunks").fetchone()["n"] == 0
    row = db.connect().execute("SELECT status FROM files WHERE filename='blank.jpg'").fetchone()
    assert row["status"] == FileStatus.AI_DONE.value


def test_ocr_failure_raises(tmp_path, ocr_ready):
    """5. 损坏图片：OcrError。"""
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(OcrError):
        ocr_image(str(bad))


def test_reocr_replace(env, tmp_path, ocr_ready):
    """6/7. 重新识别：修改图片 → 重新入队 → 新 OCR 替换旧（DELETE old + INSERT new 同事务）。"""
    img = tmp_path / "re.jpg"
    _make_image(img, "旧内容Alpha")
    fid = _process_enqueued(env, str(img))
    db = env[0]
    assert FtsRepository(db).body_match(fts_query_for("Alpha"))

    time.sleep(0.01)
    _make_image(img, "新内容Beta")
    env[4].handle_changes([str(img)])  # 重新入队
    claimed = env[5].claim_batch()
    assert len(claimed) == 1
    process_image_file(db, fid, str(img))
    env[5].complete(claimed[0])

    assert FtsRepository(db).body_match(fts_query_for("Beta"))
    assert FtsRepository(db).body_match(fts_query_for("Alpha")) == []  # 旧 OCR 已替换


def test_old_ocr_kept_on_failure(env, tmp_path, ocr_ready):
    """8. 旧 OCR 数据保护：重新识别失败（损坏图片）→ 旧 OCR/chunks 仍可搜索。"""
    img = tmp_path / "keep.jpg"
    _make_image(img, "可靠旧内容Zeta")
    fid = _process_enqueued(env, str(img))
    db = env[0]
    assert FtsRepository(db).body_match(fts_query_for("Zeta"))

    img.write_bytes(b"corrupted")  # 模拟图片损坏 → 重新处理失败
    env[4].handle_changes([str(img)])
    claimed = env[5].claim_batch()
    assert len(claimed) == 1
    with pytest.raises(OcrError):
        process_image_file(db, fid, str(img))
    env[5].fail(claimed[0], "ocr failed")

    # 旧数据保留（架构 §11.5「重新索引失败时，旧搜索能力必须保持可用」）
    assert FtsRepository(db).body_match(fts_query_for("Zeta"))
    conn = db.connect()
    assert conn.execute("SELECT count(*) n FROM chunks WHERE file_id=?", (fid,)).fetchone()["n"] == 1
    assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (claimed[0],)).fetchone()["status"] == "FAILED"
    conn.close()


def test_fts_rebuild_keeps_ocr(env, tmp_path, ocr_ready):
    """9. FTS rebuild 后 ocr 仍可搜（fts_chunks_source VIEW 含 ocr）。"""
    img = tmp_path / "rebuild.jpg"
    _make_image(img, "RebuildMarker2026")
    _process_enqueued(env, str(img))
    db = env[0]
    with db.connect() as c:
        c.execute("INSERT INTO fts_body(fts_body) VALUES('rebuild')")
        c.commit()
    assert FtsRepository(db).body_match(fts_query_for("RebuildMarker"))


def test_image_delete_excluded(env, tmp_path, ocr_ready):
    """10. 图片删除：is_deleted=1 → 搜索排除（canonical）。"""
    img = tmp_path / "del.jpg"
    _make_image(img, "DeleteMeContent")
    _process_enqueued(env, str(img))
    db = env[0]
    assert FtsRepository(db).body_match(fts_query_for("DeleteMe"))
    env[4].handle_delete_path(str(img))
    assert FtsRepository(db).body_match(fts_query_for("DeleteMe"))  # FTS 未清（异步）
    from omnisearch.server.service.filter_builder import FilterBuilderService
    from omnisearch.server.service.query_parser import QueryParser
    from omnisearch.server.service.search import SearchService
    from omnisearch.server.service.time_range import TimeRangeService

    svc = SearchService(db, env[1], env[2], QueryParser(TimeRangeService()), FilterBuilderService())
    assert svc.search("DeleteMeContent").results == []  # canonical 排除


def test_worker_failed_task(db, tmp_path, ocr_ready):
    """11. Worker FAILED：损坏图片任务 → FAILED + files.status=FAILED。"""
    from omnisearch.worker.main import _process_task

    img = tmp_path / "wf.jpg"
    img.write_bytes(b"corrupted")
    files = FileRepository(db)
    with db.connect() as c:
        cur = c.execute(
            """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
               VALUES (?, 'wf.jpg', ?, '.jpg', 1, 1, 'image')""",
            (str(img), str(tmp_path)),
        )
        fid = cur.lastrowid
    TaskRepository(db).enqueue([fid])
    claimed = TaskQueue(db).claim_batch()
    _process_task(TaskQueue(db), db, claimed[0])
    conn = db.connect()
    assert conn.execute("SELECT status FROM ai_tasks WHERE id=?", (claimed[0],)).fetchone()["status"] == "FAILED"
    assert conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"] == FileStatus.FAILED.value
    conn.close()


def test_ocr_search_after_reopen(env, tmp_path, ocr_ready):
    """12. 重启后 OCR 搜索：重开连接仍命中。"""
    img = tmp_path / "persist.jpg"
    _make_image(img, "PersistWord")
    _process_enqueued(env, str(img))
    db = env[0]
    db.checkpoint()
    fts2 = FtsRepository(db)
    assert fts2.body_match(fts_query_for("PersistWord"))
