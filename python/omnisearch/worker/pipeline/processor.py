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
import os
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


def _resolve_reuse(db: Database, file_id: int, hash_value: str, file_type: str) -> dict | None:
    """判定 AI 结果复用（P2.2 §五）：

    - 同 file_id 的 content_hash 相同 **且 AI 产物完整** → {'kind': 'same_file'}（touch/重扫：跳过 AI）
      完整性：所有源类型 chunk 存在且 embedding_status 全 SUCCESS；
      **缺失/FAILED（如 embedding 失败）→ 不能因 hash 相同跳过**（P2.3 一致性回归）——
      MVP 无 stage retry → 安全完整重新处理。
    - 其他文件（**同 file_type** + 同 hash + **AI 产物完整**，含 is_deleted=1 的关闭期删除源）
      → {'kind': 'clone', 'src_id'}——源不完整（embedding FAILED 等）不选（防跨类型/跨完整性污染）
    - 无 → None（正常 pipeline）
    """
    with db.connect() as c:
        row = c.execute("SELECT content_hash, file_type FROM files WHERE id=?", (file_id,)).fetchone()
        if row and row["content_hash"] == hash_value and _ai_complete(c, file_id, row["file_type"]):
            return {"kind": "same_file"}
        src = c.execute(
            "SELECT id FROM files WHERE content_hash = ? AND id <> ? AND file_type = ? ORDER BY id LIMIT 1",
            (hash_value, file_id, file_type),
        ).fetchone()
        if src and _ai_complete(c, src["id"], file_type):
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
    embedder: EmbeddingProvider | None,
    vector_store: VectorStore | None,
) -> bool:
    """复用源文件 AI 产物到新 file_id（复制/跨路径移动，P2.2 §五 D/E）。

    顺序（ADR-007 + 审查修正）：读旧 vectors（事务外）→ Qdrant 先 upsert 新 point_id
    （wait=true，失败 → 抛异常，无 SQLite 半成品）→ **短事务先 DELETE 目标旧产物再 INSERT**
    （§11.5 替换模式，防 UNIQUE 冲突与旧 FTS 残留）→ 尾部补向量（PENDING chunk 经
    _embed_file_chunks 补齐，防止 PENDING 成为终态）。元数据（path/filename/mtime/exif）永不复制。
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

    # 1) 读旧 vectors（Qdrant，事务外）→ 组装新 point_id 的 points（point_id 每 chunk 只算一次）
    copied: set[tuple[str, int]] = set()
    new_points: list[VectorPoint] = []
    old_vectors = {}
    if vector_store is not None:
        old_ids = [
            point_id(src_id, r["source_type"], r["chunk_index"])
            for r in src_chunks
            if r["embedding_status"] == EmbeddingStatus.SUCCESS.value
        ]
        old_vectors = vector_store.get_vectors(old_ids)
    for r in src_chunks:
        if r["embedding_status"] != EmbeddingStatus.SUCCESS.value:
            continue
        old = old_vectors.get(point_id(src_id, r["source_type"], r["chunk_index"]))
        if old is None:
            continue  # 向量缺失 → 文本仍复用；向量由尾部 _embed_file_chunks 补齐
        vec, payload = old
        new_points.append(
            VectorPoint(
                point_id=point_id(file_id, r["source_type"], r["chunk_index"]),
                vector=vec, file_id=file_id,
                source_type=r["source_type"], chunk_index=r["chunk_index"],
                text=payload.get("text", r["chunk_text"]),
            )
        )
        copied.add((r["source_type"], r["chunk_index"]))

    # 2) Qdrant 先 upsert 新 points（wait=true）；失败 → 抛异常（无 SQLite 变更，旧文件不受影响）
    if new_points:
        vector_store.upsert_points(new_points)  # type: ignore[union-attr]

    # 3) 短事务：DELETE 旧产物 → INSERT 复制文本 + hash + AI_DONE（§11.5 替换模式，防 UNIQUE 冲突）
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))  # 触发器清旧 FTS
        for r in src_chunks:
            emb = EmbeddingStatus.SUCCESS.value if (r["source_type"], r["chunk_index"]) in copied else EmbeddingStatus.PENDING.value
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
                   VALUES (?, ?, ?, ?, unixepoch())
                   ON CONFLICT(file_id) DO UPDATE SET text=excluded.text,
                       lang=excluded.lang, confidence=excluded.confidence, created_at=unixepoch()""",
                (file_id, src_ocr["text"], src_ocr["lang"], src_ocr["confidence"]),
            )
        conn.execute(
            "UPDATE files SET content_hash = ?, status = ?, updated_at = unixepoch() WHERE id = ?",
            (hash_value, FileStatus.AI_DONE.value, file_id),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # 4) 补向量：PENDING chunk（源向量缺失/embedder 可用）→ 走共享 embedding 路径，PENDING 不成为终态
    _embed_file_chunks(db, file_id, embedder, vector_store)

    logger.info(
        "AI results reused file=%d ← src=%d (hash=%s, chunks=%d, vectors=%d)",
        file_id, src_id, hash_value[:8], len(src_chunks), len(new_points),
    )
    return True


def _try_reuse(
    db: Database,
    file_id: int,
    hash_value: str,
    embedder: EmbeddingProvider | None,
    vector_store: VectorStore | None,
    label: str,
) -> bool:
    """P2.2 复用分发（doc/image 共用，审查去重）。

    返回 True = 复用路径已处理（调用方直接返回，无需正常 pipeline）。
    - same_file：hash 相同 + AI 完整 → 校验 Qdrant 向量（**丢失/重建 → 补向量而非跳过**）→ 跳过
    - clone：同 file_type + 同 hash + 源完整 → 复制 AI 产物（含向量，缺则补）
    """
    with db.connect() as c:
        row = c.execute("SELECT content_hash, file_type FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return False
        reuse = _resolve_reuse(db, file_id, hash_value, row["file_type"])
        if reuse is None:
            return False
        if reuse["kind"] == "same_file":
            # Qdrant 校验：SUCCESS chunk 的 point 是否真实存在（Qdrant = 可重建索引，需能自愈）
            missing = _missing_vectors(c, db, file_id, vector_store)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE files SET content_hash = ?, status = ?, updated_at = unixepoch() WHERE id = ?",
                    (hash_value, FileStatus.AI_DONE.value, file_id),
                )
            if missing:
                logger.info("%s %d unchanged, Qdrant missing %d vectors → re-embed", label, file_id, missing)
                _reembed_file(db, file_id, embedder, vector_store)
            else:
                logger.info("%s %d unchanged (hash %s), AI skipped", label, file_id, hash_value[:8])
            return True
        return _clone_ai_results(db, file_id, reuse["src_id"], hash_value, embedder, vector_store)


def _missing_vectors(conn, db: Database, file_id: int, vector_store: VectorStore | None) -> int:
    """SUCCESS chunk 中 Qdrant 缺失的向量数（0 = 完整）。vector_store 不可用（FTS-only）→ 视为 0。"""
    if vector_store is None:
        return 0
    ids = [
        point_id(file_id, r["source_type"], r["chunk_index"])
        for r in conn.execute(
            "SELECT source_type, chunk_index FROM chunks WHERE file_id = ? AND embedding_status = 1",
            (file_id,),
        ).fetchall()
    ]
    if not ids:
        return 0
    return len(ids) - len(vector_store.get_vectors(ids))


def _reembed_file(
    db: Database,
    file_id: int,
    embedder: EmbeddingProvider | None,
    vector_store: VectorStore | None,
) -> None:
    """重新 embedding 该文件全部 chunk（Qdrant 丢失/重建后补向量，文本产物不动，幂等覆盖）。"""
    if embedder is None or vector_store is None:
        return
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, source_type, chunk_index, chunk_text FROM chunks WHERE file_id = ?",
            (file_id,),
        ).fetchall()
    if not rows:
        return
    chunks = [dict(r) for r in rows]
    vectors = embedder.embed_texts([c["chunk_text"] for c in chunks], batch_size=32)
    points = [
        VectorPoint(
            point_id=point_id(file_id, c["source_type"], c["chunk_index"]),
            vector=vec, file_id=file_id,
            source_type=c["source_type"], chunk_index=c["chunk_index"],
            text=c["chunk_text"],
        )
        for c, vec in zip(chunks, vectors)
    ]
    vector_store.upsert_points(points)  # wait=true
    with db.connect() as c:
        c.executemany(
            "UPDATE chunks SET embedding_status = ?, updated_at = unixepoch() WHERE id = ?",
            [(EmbeddingStatus.SUCCESS.value, c["id"]) for c in chunks],
        )
    logger.info("re-embedded file=%d chunks=%d (Qdrant 补齐)", file_id, len(chunks))


def _reject_if_changed_during(path: str, hash_value: str, st0) -> None:
    """S7：处理期间文件内容变化 → 本次 AI 结果可能陈旧，拒绝（抛异常 → task FAILED，重试重新处理）。

    正常文件零 I/O 负担：仅一次 stat 对比 size/mtime；只有变化时才重算 content_hash。
    变化但 hash 相同（touch/内容未变）→ 接受（索引内容即当前内容）。
    """
    try:
        st1 = os.stat(path, follow_symlinks=False)
    except OSError:
        return  # 文件已消失：删除事件兜底，无需拒绝
    if st0.st_size == st1.st_size and st0.st_mtime_ns == st1.st_mtime_ns:
        return
    if content_hash_xxh3(path) != hash_value:
        raise ValueError(f"file changed during AI processing (stale index prevented): {path}")


def _ensure_exif(db: Database, file_id: int, path: str) -> None:
    """提取并写入新文件自身的 EXIF（clone/same_file 路径也执行——EXIF 不是被复制的 metadata，
    而是新路径文件的独立属性；缺失则精确时间过滤会误排除副本）。"""
    exif = extract_exif(path)
    if exif is None:
        return
    with db.connect() as c:
        c.execute(
            """INSERT INTO exif (file_id, datetime_original, datetime_original_epoch)
               VALUES (?, ?, ?)
               ON CONFLICT(file_id) DO UPDATE SET
                   datetime_original=excluded.datetime_original,
                   datetime_original_epoch=excluded.datetime_original_epoch""",
            (file_id, exif["datetime_original"], exif["datetime_original_epoch"]),
        )


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
    hash 每次计算并写库（复用建立的前提；首次全量虽无源可复用，仍需写 hash 供后续扫描）。
    """
    hash_value = content_hash_xxh3(path)
    if hash_value is None:
        raise ValueError(f"content hash failed: {path}")  # 不可读 → task FAILED，不误复用
    if _try_reuse(db, file_id, hash_value, embedder, vector_store, "file"):
        return

    st0 = os.stat(path, follow_symlinks=False)  # S7：处理前快照（期间内容变化 → 拒绝陈旧结果）
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
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    _embed_file_chunks(db, file_id, embedder, vector_store)
    _reject_if_changed_during(path, hash_value, st0)


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

    P2.2：同文件内容未变 → 跳过 OCR/Caption/Embedding；同 hash 其他文件 → 复用 AI 产物
    （复用路径也会补 EXIF——新文件自身的 EXIF，防止精确时间过滤误排除副本）。
    """
    hash_value = content_hash_xxh3(path)
    if hash_value is None:
        raise ValueError(f"content hash failed: {path}")  # 不可读 → task FAILED，不误复用
    if _try_reuse(db, file_id, hash_value, embedder, vector_store, "image"):
        _ensure_exif(db, file_id, path)  # 复用路径补 EXIF（same_file/clone 均幂等）
        return

    st0 = os.stat(path, follow_symlinks=False)  # S7：处理前快照（期间内容变化 → 拒绝陈旧结果）
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
    _reject_if_changed_during(path, hash_value, st0)


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
