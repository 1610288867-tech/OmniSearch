"""AI Worker 主进程（architecture.md §10）。

M3 消费循环：claim index_file（单 Worker，SQLite persistent queue）→ 按 file_type 分发：
- doc → document pipeline（提取 → 切分 → chunks 短事务替换 → AI_DONE）
- image → OCR pipeline（PaddleOCR zh+en → ocr_text + chunks(ocr) → AI_DONE）
失败 → FAILED（旧数据保留；搜索按文件现有能力降级，architecture.md §10.3）。
轮询间隔为可调参数 poll_interval_ms（默认 500，非架构约束）。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time

from omnisearch.common.config import POLL_INTERVAL_MS, db_path, dev_data_dir, log_dir, qdrant_url
from omnisearch.common.database import Database
from omnisearch.common.embedding import BGEEmbeddingProvider
from omnisearch.common.logging_setup import setup_logging
from omnisearch.common.utils.models import models_dir
from omnisearch.common.vector import VectorStore
from omnisearch.worker.pipeline.processor import process_doc_file, process_image_file
from omnisearch.worker.task.queue import TaskQueue

logger = logging.getLogger("omnisearch.worker")

_stop = threading.Event()


def _handle_signal(_signum, _frame) -> None:  # noqa: ANN001
    logger.info("received shutdown signal, draining...")
    _stop.set()


def _process_task(
    queue: TaskQueue,
    db: Database,
    task_id: int,
    embedder=None,
    vector_store=None,
    caption_provider=None,
) -> None:
    """处理单个任务：状态回写短事务；推理/提取/embedding 在事务外。"""
    with db.connect() as c:
        row = c.execute(
            """SELECT t.id AS task_id, f.id AS file_id, f.path, f.file_type
               FROM ai_tasks t JOIN files f ON f.id = t.file_id WHERE t.id = ?""",
            (task_id,),
        ).fetchone()
    if row is None:
        logger.warning("task %d: file row missing, marking complete", task_id)
        queue.complete(task_id)
        return

    try:
        if row["file_type"] == "doc":
            process_doc_file(db, row["file_id"], row["path"], embedder, vector_store)
            logger.info("doc processed: %s (file_id=%d, task=%d)", row["path"], row["file_id"], task_id)
        elif row["file_type"] == "image":
            process_image_file(db, row["file_id"], row["path"], embedder, vector_store, caption_provider)
            logger.info("image OCR+caption done: %s (file_id=%d, task=%d)", row["path"], row["file_id"], task_id)
        else:
            # 其他类型（video/audio/archive/other）不入队，理论上不可达；防御性完成
            logger.info("task %d: file_type=%s skipped", task_id, row["file_type"])
        queue.complete(task_id)
    except Exception as exc:  # noqa: BLE001 —— 提取/解析/embedding 失败 → FAILED，旧数据保留
        logger.warning("task %d failed (%s): %s", task_id, row["path"], exc)
        queue.fail(task_id, str(exc)[:2000])


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniSearch AI Worker")
    parser.add_argument("--dev-data", type=str, default=None, help="开发期数据目录（覆盖）")
    args = parser.parse_args()

    if args.dev_data:
        os.environ["OMNISEARCH_DEV_DATA"] = args.dev_data
    data_dir = dev_data_dir()
    setup_logging("omnisearch.worker", log_dir(data_dir))
    logger.info("worker starting (data_dir=%s)", data_dir)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    db = Database(db_path(data_dir))
    queue = TaskQueue(db)

    # M4：BGE Embedding + Qdrant + Caption（模型缺失/Qdrant 未启动 → 降级，不阻塞 Worker）
    embedder = vector_store = caption_provider = None
    try:
        from omnisearch.common.embedding import BGEEmbeddingProvider as _B
        from omnisearch.common.vector import VectorStore as _V

        embedder = _B(models_dir(data_dir))
        _ = embedder.dim  # 触发加载（缺失会抛 RuntimeError）
        vector_store = _V(qdrant_url(), embedder.dim)
        vector_store.ensure_collection()
        logger.info("embedding ready (dim=%d)", embedder.dim)
    except Exception:  # noqa: BLE001 —— 模型缺失/Qdrant 未就绪：降级 FTS-only
        logger.warning("embedding unavailable, worker in FTS-only mode", exc_info=True)
    try:
        from omnisearch.worker.providers.caption import LocalImageCaptionProvider as _C

        caption_provider = _C(models_dir(data_dir) / "chinese-clip")
        caption_provider._ensure_loaded()  # noqa: SLF001 —— 触发加载（缺失会抛 RuntimeError）
        logger.info("caption provider ready (chinese-clip tagging)")
    except Exception:  # noqa: BLE001 —— 模型缺失：图片仅 OCR，无 Caption
        logger.warning("caption provider unavailable, images OCR-only", exc_info=True)

    poll_interval_s = int(os.environ.get("OMNISEARCH_POLL_INTERVAL_MS", POLL_INTERVAL_MS)) / 1000.0
    heartbeat_s = 5.0
    last_heartbeat = 0.0

    def _write_heartbeat() -> None:
        """心跳写 SQLite（M5 收口 4：/health worker_ready 依据；失败不阻塞主循环）。"""
        try:
            with db.connect() as c:
                c.execute(
                    """INSERT INTO worker_heartbeat(worker_id, last_seen) VALUES('worker', unixepoch())
                       ON CONFLICT(worker_id) DO UPDATE SET last_seen=unixepoch()"""
                )
        except Exception:  # noqa: BLE001 —— 心跳失败仅影响 readiness 判定
            logger.warning("heartbeat write failed", exc_info=True)

    while not _stop.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_s:
            logger.info("heartbeat: alive (poll_interval_ms=%d)", int(poll_interval_s * 1000))
            _write_heartbeat()
            last_heartbeat = now
        claimed: list[int] = []
        try:
            claimed = queue.claim_batch(batch_size=8)
            for task_id in claimed:
                _process_task(queue, db, task_id, embedder, vector_store, caption_provider)
        except Exception:  # noqa: BLE001 —— 队列/DB 瞬态错误（如 server 未就绪）：记录并继续
            logger.exception("worker loop error")
        if not claimed:
            _stop.wait(poll_interval_s)

    logger.info("worker stopped")


if __name__ == "__main__":
    main()
