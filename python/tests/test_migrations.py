"""Migration 与 FTS 语义测试（architecture.md §8 / ADR-005）。

关键验证：image_caption 永不进入 FTS 关键词通道（含 rebuild 场景）。
"""
from __future__ import annotations

from omnisearch.common.database import Database
from omnisearch.common.models import SourceType


def _insert_file(conn, path: str, file_type: str = "image") -> int:
    cur = conn.execute(
        """INSERT INTO files (path, filename, dir_path, extension, mtime_ns, ctime_ns, file_type)
           VALUES (?, ?, ?, '', 1, 1, ?)""",
        (path, path.rsplit("/", 1)[-1], path.rsplit("/", 1)[0], file_type),
    )
    return cur.lastrowid


def test_migration_applies_all_tables(db: Database):
    conn = db.connect()
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = {
        "files", "exif", "chunks", "ocr_text", "search_history",
        "ai_tasks", "index_jobs", "settings", "fts_body", "fts_files",
        "worker_heartbeat",  # v003（M5 收口 4：worker readiness）
    }
    assert expected <= tables, f"missing: {expected - tables}"
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 3
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_fts_files_created_per_adr005(db: Database):
    """ADR-005：fts_files 按 sqlite_version 动态创建，rowid 语义为 files.id。"""
    conn = db.connect()
    # contentless-delete 表（≥3.43）允许普通 DELETE；此处验证可写可删
    fid = _insert_file(conn, "/x/a.jpg")
    conn.execute(
        "INSERT INTO fts_files(rowid, filename, filename_seg, dir_tokens) VALUES (?, ?, ?, ?)",
        (fid, "a.jpg", "a jpg", "/x"),
    )
    conn.commit()
    rows = conn.execute("SELECT rowid FROM fts_files").fetchall()
    assert rows and rows[0]["rowid"] == fid
    conn.execute("DELETE FROM fts_files WHERE rowid = ?", (fid,))
    conn.commit()
    conn.close()


def test_image_caption_never_enters_fts(db: Database):
    conn = db.connect()
    fid = _insert_file(conn, "/x/img1.jpg")
    # image_caption chunk —— 不应进入 fts_body
    cur = conn.execute(
        """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
           VALUES (?, ?, 0, '一张自由女神像的夜景照片', '一张 自由女神像 的 夜景 照片')""",
        (fid, SourceType.IMAGE_CAPTION.value),
    )
    caption_chunk_id = cur.lastrowid
    # doc_chunk + ocr —— 应进入 fts_body
    conn.execute(
        """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
           VALUES (?, ?, 0, '关于机器学习的研究笔记', '关于 机器学习 的 研究 笔记')""",
        (fid, SourceType.DOC_CHUNK.value),
    )
    conn.execute(
        """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
           VALUES (?, ?, 0, 'New York 2026', 'New York 2026')""",
        (fid, SourceType.OCR.value),
    )
    conn.commit()

    # 触发器不写入 image_caption
    hits = conn.execute(
        "SELECT rowid FROM fts_body WHERE fts_body MATCH '自由女神'"
    ).fetchall()
    assert hits == []

    # doc_chunk / ocr 正常命中
    assert conn.execute("SELECT rowid FROM fts_body WHERE fts_body MATCH '机器学习'").fetchall()
    assert conn.execute("SELECT rowid FROM fts_body WHERE fts_body MATCH 'New'").fetchall()

    # rebuild 也只含 doc_chunk + ocr（content 指向过滤 VIEW）
    conn.execute("INSERT INTO fts_body(fts_body) VALUES('rebuild')")
    conn.commit()
    caption_hits = conn.execute(
        "SELECT rowid FROM fts_body WHERE fts_body MATCH '自由女神'"
    ).fetchall()
    assert caption_hits == []
    assert conn.execute("SELECT rowid FROM fts_body WHERE fts_body MATCH '机器学习'").fetchall()
    conn.close()


def test_fts_body_delete_and_update_sync(db: Database):
    """chunks DELETE/UPDATE 触发器同步 fts_body（architecture.md §8.2）。"""
    conn = db.connect()
    fid = _insert_file(conn, "/x/doc1.txt", file_type="doc")
    cur = conn.execute(
        """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text, chunk_text_seg)
           VALUES (?, ?, 0, '原始段落', '原始 段落')""",
        (fid, SourceType.DOC_CHUNK.value),
    )
    chunk_id = cur.lastrowid
    conn.commit()
    assert conn.execute("SELECT 1 FROM fts_body WHERE rowid=?", (chunk_id,)).fetchone()

    # UPDATE → delete+insert 同步
    conn.execute(
        "UPDATE chunks SET chunk_text='更新后段落', chunk_text_seg='更新后 段落' WHERE id=?",
        (chunk_id,),
    )
    conn.commit()
    assert not conn.execute("SELECT 1 FROM fts_body WHERE fts_body MATCH '原始'").fetchone()
    assert conn.execute("SELECT 1 FROM fts_body WHERE fts_body MATCH '更新后'").fetchone()

    # DELETE → 级联删除
    conn.execute("DELETE FROM chunks WHERE id=?", (chunk_id,))
    conn.commit()
    assert not conn.execute("SELECT 1 FROM fts_body WHERE rowid=?", (chunk_id,)).fetchone()
    conn.close()
