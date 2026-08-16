"""文档/图片处理（M2 doc + M3 ocr：architecture.md §10.3/§11.5 reindex 一致性）。

流程：提取/OCR → 切分 → 全部在 SQLite 事务外（内存完成）→ 完整成功后单个短事务
（DELETE old chunks + INSERT new chunks + FTS 触发器联动 + ocr_text 更新 + files.status=AI_DONE）。
任一阶段失败 → 抛异常（旧 chunks/FTS 保留，task=FAILED）。
「重新索引失败时，旧搜索能力必须保持可用。」
"""
from __future__ import annotations

import logging
from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.common.embedding import EmbeddingProvider
from omnisearch.common.models import EmbeddingStatus, FileStatus, SourceType
from omnisearch.common.utils.point_id import point_id
from omnisearch.common.utils.seg import seg_text
from omnisearch.common.vector import VectorPoint, VectorStore
from omnisearch.worker.pipeline.chunker import chunk_text, estimate_tokens
from omnisearch.worker.pipeline.doc import extract_text
from omnisearch.worker.pipeline.exif import extract_exif
from omnisearch.worker.pipeline.ocr import OcrError, normalize_ocr_text, ocr_image

logger = logging.getLogger("omnisearch.worker.pipeline")


def process_doc_file(
    db: Database,
    file_id: int,
    path: str,
    embedder: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
) -> None:
    """处理一个文档文件：提取 → 切分 → 短事务替换 chunks（FTS 触发器联动）→ embedding。

    embedding 在 SQLite 事务外执行（架构 §10.2）；失败 → 状态 FAILED + 抛异常
    （FTS 不受影响，task FAILED，旧数据保留）。
    """
    text = extract_text(path)  # 事务外（架构 §11.5）
    chunks = chunk_text(text)  # 事务外
    prepared = [(c, seg_text(c), estimate_tokens(c)) for c in chunks]

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))  # 触发器清 fts_body
        conn.executemany(
            """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text,
                                   chunk_text_seg, token_count, embedding_status)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",  # embedding_status=PENDING（M4 向量化）
            [
                (file_id, SourceType.DOC_CHUNK.value, i, c, seg, tc)
                for i, (c, seg, tc) in enumerate(prepared)
            ],
        )
        conn.execute(
            "UPDATE files SET status = ?, updated_at = unixepoch() WHERE id = ?",
            (FileStatus.AI_DONE.value, file_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    _embed_file_chunks(db, file_id, embedder, vector_store)


def process_image_file(
    db: Database,
    file_id: int,
    path: str,
    embedder: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    caption_provider=None,
) -> None:
    """处理一个图片文件：OCR（zh+en）+ Caption（M4）→ 短事务替换 chunks + 更新 ocr_text → embedding。

    - ocr_text：原始结果存档（text/lang/confidence/created_at）
    - chunks(ocr, 0)：OCR 标准化搜索输入（FTS 触发器同步）
    - chunks(image_caption, 0)：中文标签文本（M4.4；**不进 FTS，仅 Vector**——VIEW 保证）
    - 无文字图片 → 不插 ocr chunk；caption 为空 → 不插 image_caption chunk
    """
    result = ocr_image(path)  # 事务外（架构 §11.5；失败抛 OcrError → task FAILED）
    caption = caption_provider.caption(Path(path)) if caption_provider is not None else None
    exif = extract_exif(path)  # 事务外；时间过滤 exact 语义（M5，§12.7）

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        # 旧数据先保留，新结果完整后单事务替换（架构 §11.5）
        conn.execute(
            "DELETE FROM chunks WHERE file_id = ? AND source_type IN (?, ?)",
            (file_id, SourceType.OCR.value, SourceType.IMAGE_CAPTION.value),
        )
        # 职责分离（M3）：ocr_text 存原始 OCR 输出；chunks(ocr) 存标准化搜索输入（英文拆词）
        search_text = normalize_ocr_text(result.text)
        if search_text:
            seg = seg_text(search_text)
            conn.execute(
                """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text,
                                       chunk_text_seg, token_count, embedding_status)
                   VALUES (?, ?, 0, ?, ?, ?, 0)""",  # embedding_status=PENDING（M4 向量化）
                (file_id, SourceType.OCR.value, search_text, seg, estimate_tokens(search_text)),
            )
        # M4.4：image_caption（中文标签文本，仅 Vector 通道）
        if caption is not None and caption.text:
            seg = seg_text(caption.text)
            conn.execute(
                """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text,
                                       chunk_text_seg, token_count, embedding_status)
                   VALUES (?, ?, 0, ?, ?, ?, 0)""",
                (file_id, SourceType.IMAGE_CAPTION.value, caption.text, seg, estimate_tokens(caption.text)),
            )
        conn.execute(
            """INSERT INTO ocr_text (file_id, text, lang, confidence, created_at)
               VALUES (?, ?, 'zh+en', ?, unixepoch())
               ON CONFLICT(file_id) DO UPDATE SET text=excluded.text,
                   lang=excluded.lang, confidence=excluded.confidence, created_at=unixepoch()""",
            (file_id, result.text, result.confidence),
        )
        # M5：EXIF 拍摄时间（exact 时间过滤；无 EXIF → 不写，fallback 走 mtime/ctime）
        if exif is not None:
            conn.execute(
                """INSERT INTO exif (file_id, datetime_original, datetime_original_epoch)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET
                       datetime_original=excluded.datetime_original,
                       datetime_original_epoch=excluded.datetime_original_epoch""",
                (file_id, exif["datetime_original"], exif["datetime_original_epoch"]),
            )
        conn.execute(
            "UPDATE files SET status = ?, updated_at = unixepoch() WHERE id = ?",
            (FileStatus.AI_DONE.value, file_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    _embed_file_chunks(db, file_id, embedder, vector_store)


def _embed_file_chunks(
    db: Database,
    file_id: int,
    embedder: EmbeddingProvider | None,
    vector_store: VectorStore | None,
) -> None:
    """该文件全部 PENDING chunk → BGE batch embed → Qdrant upsert(wait=true) → SUCCESS。

    - 全部在 SQLite 事务外（架构 §10.2）
    - 失败：chunks 置 FAILED（单短事务）→ 抛异常 → task FAILED；FTS 不受影响（§9.3）
    - stale 清理（§11.5）：upsert 前记录 old keys（不提前删除）→ 差集异步删除，失败仅记录
    """
    if embedder is None or vector_store is None:
        return  # 纯 FTS 模式（测试/降级）
    with db.connect() as c:
        rows = c.execute(
            """SELECT id, source_type, chunk_index, chunk_text
               FROM chunks WHERE file_id = ? AND embedding_status = ?""",
            (file_id, EmbeddingStatus.PENDING.value),
        ).fetchall()
    if not rows:
        return

    chunks = [dict(r) for r in rows]
    try:
        old_keys = vector_store.list_keys_by_file(file_id)  # upsert 前记录（不提前删除，§11.5）
        vectors = embedder.embed_texts([c["chunk_text"] for c in chunks], batch_size=32)
        points = [
            VectorPoint(
                point_id=point_id(file_id, c["source_type"], c["chunk_index"]),
                vector=vec,
                file_id=file_id,
                source_type=c["source_type"],
                chunk_index=c["chunk_index"],
                text=c["chunk_text"],
            )
            for c, vec in zip(chunks, vectors)
        ]
        vector_store.upsert_points(points)  # wait=true：成功才置 SUCCESS（§9.3）
        with db.connect() as c:
            c.executemany(
                "UPDATE chunks SET embedding_status = ?, updated_at = unixepoch() WHERE id = ?",
                [(EmbeddingStatus.SUCCESS.value, c["id"]) for c in chunks],
            )
        # stale 清理（§11.5：upsert 成功后；清理失败不影响新 points）
        new_keys = {point_id(file_id, c["source_type"], c["chunk_index"]) for c in chunks}
        stale = [k for k in old_keys if k not in new_keys]
        if stale:
            try:
                vector_store.delete_points(stale)
            except Exception:  # noqa: BLE001
                logger.warning("stale cleanup failed for file %d (P2 对账兜底)", file_id)
    except Exception:
        with db.connect() as c:
            c.executemany(
                "UPDATE chunks SET embedding_status = ?, updated_at = unixepoch() WHERE id = ?",
                [(EmbeddingStatus.FAILED.value, c["id"]) for c in chunks],
            )
        raise
