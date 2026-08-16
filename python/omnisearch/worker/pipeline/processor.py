"""文档/图片处理（M2 doc + M3 ocr + P2.2 hash 复用：architecture.md §10.3/§11.5）。

流程：提取/OCR → 切分 → 全部在 SQLite 事务外（内存完成）→ 完整成功后单个短事务
（DELETE old chunks + INSERT new chunks + FTS 触发器联动 + ocr_text 更新 + files.status=AI_DONE）。
任一阶段失败 → 抛异常（旧 chunks/FTS 保留，task=FAILED）。
「重新索引失败时，旧搜索能力必须保持可用。」

P2.2 content_hash AI 结果复用（ADR-007）：
- 处理前流式计算 xxh3_64；同 file_id 内容未变（touch/重扫）→ 跳过全部 AI，保留旧产物
- 同 hash 其他文件（复制/跨路径移动）→ 复用 ocr_text/chunks/embedding 文本数据 +
  Qdrant 向量按「新 logical_key → 新 point_id」复制（禁止直接复制旧 point_id，免 BGE inference）
- hash 计算失败 → 抛异常（task FAILED，不得误复用）；元数据（path/filename/mtime 等）永不复制
"""
from __future__ import annotations

import logging
from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.common.embedding import EmbeddingProvider
from omnisearch.common.models import EmbeddingStatus, FileStatus, SourceType
from omnisearch.common.utils.hash import content_hash_xxh3
from omnisearch.common.utils.point_id import point_id
from omnisearch.common.utils.seg import seg_text
from omnisearch.common.vector import VectorPoint, VectorStore
from omnisearch.worker.pipeline.chunker import chunk_text, estimate_tokens
from omnisearch.worker.pipeline.doc import extract_text
from omnisearch.worker.pipeline.exif import extract_exif
from omnisearch.worker.pipeline.ocr import OcrError, normalize_ocr_text, ocr_image

logger = logging.getLogger("omnisearch.worker.pipeline")


def _resolve_reuse(db: Database, file_id: int, hash_value: str) -> dict | None:
    """判定 AI 结果复用（P2.2 §五）：

    - 同 file_id 的 content_hash 相同 **且 AI 产物完整** → {'kind': 'same_file'}（touch/重扫：跳过 AI）
      完整性：所有源类型 chunk 存在且 embedding_status 全 SUCCESS；
      **缺失/FAILED（如 embedding 失败）→ 不能因 hash 相同跳过**（P2.3 一致性回归）——
      MVP 无 stage retry → 安全完整重新处理。
    - 其他文件（含 is_deleted=1 的关闭期删除源）content_hash 相同 → {'kind': 'clone', 'src_id'}
    - 无 → None（正常 pipeline）
    """
    with db.connect() as c:
        row = c.execute("SELECT content_hash, file_type FROM files WHERE id=?", (file_id,)).fetchone()
        if row and row["content_hash"] == hash_value and _ai_complete(c, file_id, row["file_type"]):
            return {"kind": "same_file"}
        src = c.execute(
            "SELECT id FROM files WHERE content_hash = ? AND id <> ? ORDER BY id LIMIT 1",
            (hash_value, file_id),
        ).fetchone()
        if src:
            return {"kind": "clone", "src_id": src["id"]}
    return None


def _ai_complete(conn, file_id: int, file_type: str) -> bool:
    """AI 产物完整性判定（P2.2 一致性）：该文件已有的每个 AI chunk 的 embedding 全部 SUCCESS，
    且至少有一个源类型产物（doc 需 doc_chunk；image 需 ocr 或 image_caption 任一）。

    任一 chunk embedding_status=FAILED/PENDING，或源类型完全缺失 → False（不得因 hash 相同跳过）。
    """
    rows = conn.execute(
        """SELECT source_type, count(*) AS n,
                  sum(CASE WHEN embedding_status = 1 THEN 1 ELSE 0 END) AS ok
           FROM chunks WHERE file_id = ? GROUP BY source_type""",
        (file_id,),
    ).fetchall()
    if not rows:
        return False  # 无任何 AI 产物
    by_type = {r["source_type"]: (r["n"], r["ok"]) for r in rows}
    if file_type == "doc":
        n, ok = by_type.get(SourceType.DOC_CHUNK.value, (0, 0))
        return n > 0 and ok == n
    # image：OCR 必须已执行（ocr_text 行存在——无文字图片也有行，区分「合法无文字」与「OCR 缺失/FAILED」）
    # + 至少一个 AI chunk（ocr / image_caption）且全部 SUCCESS
    ocr_done = conn.execute("SELECT 1 FROM ocr_text WHERE file_id = ?", (file_id,)).fetchone() is not None
    if not ocr_done:
        return False
    n = sum(by_type.get(st, (0, 0))[0] for st in (SourceType.OCR.value, SourceType.IMAGE_CAPTION.value))
    ok = sum(by_type.get(st, (0, 0))[1] for st in (SourceType.OCR.value, SourceType.IMAGE_CAPTION.value))
    return n > 0 and ok == n


def _clone_ai_results(
    db: Database,
    file_id: int,
    src_id: int,
    hash_value: str,
    vector_store: VectorStore | None,
) -> None:
    """复用源文件 AI 产物到新 file_id（复制/跨路径移动，P2.2 §五 D/E）。

    顺序（ADR-007）：读旧 vectors（事务外）→ Qdrant 先 upsert 新 point_id（wait=true，
    失败 → 抛异常，无 SQLite 半成品）→ 短事务复制 ocr_text + chunks（embedding_status
    按实际向量是否复制成功）。元数据（path/filename/mtime/exif 等）永不复制。
    """
    with db.connect() as c:
        src_chunks = c.execute(
            """SELECT source_type, chunk_index, chunk_text, chunk_text_seg,
                      token_count, embedding_status
               FROM chunks WHERE file_id = ?""",
            (src_id,),
        ).fetchall()
        src_ocr = c.execute(
            "SELECT text, lang, confidence FROM ocr_text WHERE file_id = ?", (src_id,)
        ).fetchone()
    if not src_chunks:
        # 源无 AI 产物（从未处理）→ 无法复用：正常 pipeline（调用方继续处理）
        return False

    # 1) 读旧 vectors（Qdrant，事务外）→ 组装新 point_id 的 points
    old_ids = [
        point_id(src_id, r["source_type"], r["chunk_index"])
        for r in src_chunks
        if r["embedding_status"] == EmbeddingStatus.SUCCESS.value
    ]
    old_vectors = vector_store.get_vectors(old_ids) if (vector_store is not None and old_ids) else {}
    new_points: list[VectorPoint] = []
    for r in src_chunks:
        if r["embedding_status"] != EmbeddingStatus.SUCCESS.value:
            continue
        old = old_vectors.get(point_id(src_id, r["source_type"], r["chunk_index"]))
        if old is None:
            continue  # 向量缺失 → 该 chunk 文本仍复用，embedding 保持 PENDING
        vec, payload = old
        new_points.append(
            VectorPoint(
                point_id=point_id(file_id, r["source_type"], r["chunk_index"]),
                vector=vec, file_id=file_id,
                source_type=r["source_type"], chunk_index=r["chunk_index"],
                text=payload.get("text", r["chunk_text"]),
            )
        )

    # 2) Qdrant 先 upsert 新 points（wait=true）；失败 → 抛异常（无 SQLite 变更，旧文件不受影响）
    if new_points:
        vector_store.upsert_points(new_points)  # type: ignore[union-attr]

    # 3) 短事务：复制文本产物 + hash + AI_DONE（embedding_status 按向量是否复制成功）
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        for r in src_chunks:
            emb = (
                EmbeddingStatus.SUCCESS.value
                if any(p.source_type == r["source_type"] and p.chunk_index == r["chunk_index"] for p in new_points)
                else EmbeddingStatus.PENDING.value
            )
            conn.execute(
                """INSERT INTO chunks (file_id, source_type, chunk_index, chunk_text,
                                       chunk_text_seg, token_count, embedding_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (file_id, r["source_type"], r["chunk_index"], r["chunk_text"],
                 r["chunk_text_seg"], r["token_count"], emb),
            )
        if src_ocr:
            conn.execute(
                """INSERT INTO ocr_text (file_id, text, lang, confidence, created_at)
                   VALUES (?, ?, ?, ?, unixepoch())""",
                (file_id, src_ocr["text"], src_ocr["lang"], src_ocr["confidence"]),
            )
        conn.execute(
            "UPDATE files SET content_hash = ?, status = ?, updated_at = unixepoch() WHERE id = ?",
            (hash_value, FileStatus.AI_DONE.value, file_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    logger.info(
        "AI results reused file=%d ← src=%d (hash=%s, chunks=%d, vectors=%d)",
        file_id, src_id, hash_value[:8], len(src_chunks), len(new_points),
    )
    return True


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

    P2.2：处理前计算 content_hash → 同 file_id 内容未变 → 跳过 AI（旧产物保留）；
    同 hash 其他文件 → 复用 AI 产物（免 OCR/Caption/Embedding）。
    """
    hash_value = content_hash_xxh3(path)
    if hash_value is None:
        raise ValueError(f"content hash failed: {path}")  # 不可读 → task FAILED，不误复用
    reuse = _resolve_reuse(db, file_id, hash_value)
    if reuse is not None:
        if reuse["kind"] == "same_file":
            with db.connect() as c:
                c.execute(
                    "UPDATE files SET content_hash = ?, status = ?, updated_at = unixepoch() WHERE id = ?",
                    (hash_value, FileStatus.AI_DONE.value, file_id),
                )
            logger.info("file %d unchanged (hash %s), AI skipped", file_id, hash_value[:8])
            return
        if _clone_ai_results(db, file_id, reuse["src_id"], hash_value, vector_store):
            return
        # 源无 AI 产物 → 继续正常 pipeline

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
            "UPDATE files SET status = ?, content_hash = ?, updated_at = unixepoch() WHERE id = ?",
            (FileStatus.AI_DONE.value, hash_value, file_id),
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

    P2.2：同文件内容未变 → 跳过 OCR/Caption/Embedding；同 hash 其他文件 → 复用 AI 产物。
    """
    hash_value = content_hash_xxh3(path)
    if hash_value is None:
        raise ValueError(f"content hash failed: {path}")  # 不可读 → task FAILED，不误复用
    reuse = _resolve_reuse(db, file_id, hash_value)
    if reuse is not None:
        if reuse["kind"] == "same_file":
            with db.connect() as c:
                c.execute(
                    "UPDATE files SET content_hash = ?, status = ?, updated_at = unixepoch() WHERE id = ?",
                    (hash_value, FileStatus.AI_DONE.value, file_id),
                )
            logger.info("image %d unchanged (hash %s), AI skipped", file_id, hash_value[:8])
            return
        if _clone_ai_results(db, file_id, reuse["src_id"], hash_value, vector_store):
            return
        # 源无 AI 产物 → 继续正常 pipeline

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
            "UPDATE files SET status = ?, content_hash = ?, updated_at = unixepoch() WHERE id = ?",
            (FileStatus.AI_DONE.value, hash_value, file_id),
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
