"""IndexService —— 扫描/增量/删除/rename（architecture.md §11）。

全量扫描：有序 DFS（显式栈，不跟随符号链接）→ 生产者-消费者
（8 个 stat 线程 + 有界队列 10k；1 个写线程每 1000 行一个短事务，files + fts_files 原子提交）
→ 每 500 文件更新 index_jobs 进度 → 扫描完比对磁盘/库差集做删除同步。

增量（handle_changes/handle_rename）：防抖合并后由 WatchService 调用，
事件线程不做耗时操作（只入队，本 Service 在工作线程执行）。
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.common.models import FileType, JobStatus
from omnisearch.common.utils.files import (
    file_type_for,
    mime_type_for,
    should_skip_dir,
    should_skip_extension,
)
from omnisearch.common.utils.paths import normalize
from omnisearch.common.models import FileStatus, FileType, JobStatus
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.tasks import TaskRepository

logger = logging.getLogger("omnisearch.server.index")

SCAN_WORKERS = 8        # stat 线程数（architecture.md §11.1）
SCAN_QUEUE_SIZE = 10_000
WRITE_BATCH = 1_000     # 每 1000 行一个短事务
PROGRESS_EVERY = 500    # 每 500 文件更新进度
CURSOR_EVERY = 1_000    # 断点续扫（P2.1）：每弹出 1000 个目录更新 cursor_path

_SENTINEL = None


@dataclass
class _ScanResult:
    total: int
    errors: int


class IndexService:
    def __init__(
        self,
        db: Database,
        files: FileRepository,
        fts: FtsRepository,
        jobs: IndexJobRepository,
        tasks: TaskRepository | None = None,
    ):
        self._db = db
        self._files = files
        self._fts = fts
        self._jobs = jobs
        self._tasks = tasks

    # ================= 全量扫描 =================

    def start_scan(self, root: str, scan_type: str = "full") -> int:
        """创建扫描作业（立即返回 job_id；执行在后台线程，见 run_scan）。"""
        return self._jobs.create(root, scan_type)

    def run_scan(self, job_id: int, root: str) -> None:
        """后台执行扫描（FastAPI BackgroundTasks 线程池调用；UI 经 /index/status 轮询进度）。

        P2.1 断点续扫：job.cursor_path 非空（上次中断）→ DFS 从该目录继续；
        完成/失败 → 清空 cursor。已处理部分的文件由幂等 upsert + 完成时 _sync_deleted 覆盖。
        """
        try:
            result = self._scan_tree(root, job_id)
            if result.total < 0:  # S1：writer 写入失败 → job FAILED（旧数据保留，不执行删除同步）
                logger.error("scan aborted root=%s job=%d (write failure)", root, job_id)
                self._jobs.update_cursor(job_id, None)
                self._jobs.finish(job_id, JobStatus.FAILED.value, 0)
                return
            # 删除同步：库中活跃但磁盘已消失的 path → 软删除 + FTS cleanup
            self._sync_deleted(root)
            self._jobs.update_cursor(job_id, None)  # 完成：清断点
            self._jobs.finish(job_id, JobStatus.DONE.value, result.total)
            logger.info("scan done root=%s job=%d total=%d errors=%d", root, job_id, result.total, result.errors)
        except Exception:
            logger.exception("scan failed root=%s job=%d", root, job_id)
            self._jobs.update_cursor(job_id, None)
            self._jobs.finish(job_id, JobStatus.FAILED.value, 0)

    def _scan_tree(self, root: str, job_id: int) -> _ScanResult:
        scan_q: queue.Queue = queue.Queue(maxsize=SCAN_QUEUE_SIZE)
        write_q: queue.Queue = queue.Queue(maxsize=SCAN_QUEUE_SIZE)
        stop = threading.Event()
        scanned_total: list[int] = [0]  # writer 线程汇总

        def _put(q: queue.Queue, item) -> bool:
            """带 stop 感知的入队（S1：writer 异常中止后队列不再被消费，put 不得永久阻塞）。"""
            while not stop.is_set():
                try:
                    q.put(item, timeout=1.0)
                    return True
                except queue.Full:
                    continue
            return False

        def producer() -> None:
            """有序 DFS（显式栈），产出待 stat 的文件路径。

            P2.1 断点续扫：启动栈取 job.cursor_path（上次中断位置，有效目录才用）；
            每弹出 CURSOR_EVERY 个目录更新 cursor_path = 当前栈顶（中断恢复点）。
            """
            try:
                job = self._jobs.get(job_id)
                cursor = job.get("cursor_path") if job else None
                start = Path(cursor) if cursor and os.path.isdir(cursor) else Path(root)
                stack = [start]
                pops = 0
                while stack and not stop.is_set():
                    current = stack.pop()
                    pops += 1
                    if pops % CURSOR_EVERY == 0:
                        # 断点 = 下一个待处理目录（栈顶）；栈空表示即将完成
                        self._jobs.update_cursor(job_id, str(stack[-1]) if stack else None)
                    try:
                        entries = sorted(os.scandir(current), key=lambda e: e.name.lower(), reverse=True)
                    except OSError as exc:
                        logger.warning("scandir failed %s: %s", current, exc)
                        continue
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not should_skip_dir(entry.name):
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                ext = Path(entry.name).suffix.lower()
                                if not should_skip_extension(ext) and not _put(scan_q, entry.path):
                                    break
                        except OSError:
                            continue
            finally:
                for _ in range(SCAN_WORKERS):
                    try:
                        scan_q.put(_SENTINEL, timeout=1.0)
                    except queue.Full:
                        break

        def worker() -> None:
            """stat + 元数据提取（不跟随符号链接）。"""
            while True:
                p = scan_q.get()
                if p is _SENTINEL:
                    scan_q.task_done()
                    _put(write_q, _SENTINEL)
                    return
                try:
                    st = os.stat(p, follow_symlinks=False)
                    path = str(Path(p))
                    filename = Path(p).name
                    if not _put(
                        write_q,
                        FileMeta(
                            path=path,
                            filename=filename,
                            dir_path=str(Path(p).parent),
                            extension=Path(filename).suffix.lower(),
                            size_bytes=st.st_size,
                            mtime_ns=st.st_mtime_ns,
                            ctime_ns=st.st_ctime_ns,
                            file_type=file_type_for(Path(filename).suffix),
                            mime_type=mime_type_for(filename),
                        ),
                    ):
                        return  # stop：writer 已中止
                except OSError as exc:
                    logger.warning("stat failed %s: %s", p, exc)
                finally:
                    scan_q.task_done()

        def writer() -> None:
            """批量写：每 1000 行一个短事务（files + fts_files 原子）；汇总进度。

            S1 修正：写入异常（SQLITE_BUSY/磁盘满）不得杀死 writer 线程——
            否则队列链式阻塞导致扫描永久挂起；改为记录错误并中止（job 由 run_scan 置 FAILED）。
            """
            seen_sentinels = 0
            batch: list[FileMeta] = []
            scanned = 0
            failed = False
            while seen_sentinels < SCAN_WORKERS:
                item = write_q.get()
                if item is _SENTINEL:
                    seen_sentinels += 1
                    write_q.task_done()
                    continue
                batch.append(item)
                write_q.task_done()
                if len(batch) >= WRITE_BATCH:
                    try:
                        self._flush_batch(batch, priority=1)  # 批量扫描：低优先级
                    except Exception:
                        logger.exception("scan write failed at %d files (job=%d) → aborting scan", scanned, job_id)
                        failed = True
                        break
                    scanned += len(batch)
                    batch = []
                    if scanned % PROGRESS_EVERY < WRITE_BATCH:
                        self._jobs.update_progress(job_id, scanned)
            if failed:
                stop.set()  # 通知 producer/worker 停止投递（防队列阻塞）
                scanned_total[0] = -1  # 标记失败（run_scan 识别）
                return
            if batch:
                try:
                    self._flush_batch(batch, priority=1)
                except Exception:
                    logger.exception("scan write failed (tail batch, job=%d) → aborting scan", job_id)
                    stop.set()
                    scanned_total[0] = -1
                    return
                scanned += len(batch)
            self._jobs.update_progress(job_id, scanned)
            scanned_total[0] = scanned

        threads = [threading.Thread(target=producer, daemon=True)]
        threads += [threading.Thread(target=worker, daemon=True) for _ in range(SCAN_WORKERS)]
        threads.append(threading.Thread(target=writer, daemon=True))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return _ScanResult(total=scanned_total[0], errors=0)

    def _flush_batch(self, batch: list[FileMeta], priority: int = 1) -> None:
        """一个短事务：files upsert（复活规则）→ fts_files 同步 → doc 文件入队 AI 任务。

        fts 操作按类型分发：insert（新/复活）/ replace（改名/移动）。
        M2：file_type=doc 且 status=METADATA_ONLY 的文件入队 index_file
        （priority：批量扫描=1 低，监听触发=0 高；image 任务 M3 再入队）。
        """
        conn = self._db.connect()
        try:
            conn.execute("BEGIN")
            fts_ops = self._files.upsert_batch(batch, conn=conn)
            for op in fts_ops:
                if op.op == "replace":
                    self._fts.replace(op.file_id, op.filename, op.filename_seg, op.dir_tokens, conn=conn)
                else:
                    self._fts.insert(op.file_id, op.filename, op.filename_seg, op.dir_tokens, conn=conn)
            # 入队：本批次中 status=METADATA_ONLY 的 doc/image 文件
            # （新插入/复活/内容变化重置；不依赖 fts_ops——内容变化可能无 FTS 操作；
            #   M3 起 image 入队走 OCR pipeline）
            if batch and self._tasks is not None:
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"""SELECT id, file_type FROM files
                        WHERE path IN ({placeholders}) AND status = ?""",
                    (*[normalize(m.path) for m in batch], FileStatus.METADATA_ONLY.value),
                ).fetchall()
                ai_ids = [
                    r["id"] for r in rows
                    if r["file_type"] in (FileType.DOC.value, FileType.IMAGE.value)
                ]
                if ai_ids:
                    self._tasks.enqueue(ai_ids, priority=priority, conn=conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ================= 增量（Watchdog 调用） =================

    def handle_changes(self, paths: list[str]) -> None:
        """增量处理一批路径：存在 → stat+upsert；不存在 → 软删除 + FTS cleanup。

        事件线程只负责入队（防抖合并），本方法在工作线程执行（不做耗时操作在事件线程）。
        """
        for p in paths:
            try:
                if os.path.isfile(p):
                    st = os.stat(p, follow_symlinks=False)
                    meta = FileMeta(
                        path=p, filename=Path(p).name, dir_path=str(Path(p).parent),
                        extension=Path(p).suffix.lower(), size_bytes=st.st_size,
                        mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns,
                        file_type=file_type_for(Path(p).suffix), mime_type=mime_type_for(p),
                    )
                    self._flush_batch([meta], priority=0)  # 监听触发：高优先级（架构 §7.1）
                else:
                    self.handle_delete_path(p)
            except OSError as exc:
                logger.warning("handle_changes failed %s: %s", p, exc)

    def handle_delete_path(self, path: str) -> None:
        """删除同步：is_deleted=1 先行（canonical 立即排除）→ 异步 FTS cleanup。"""
        ids = self._files.mark_deleted_by_paths([path])
        for fid in ids:
            self._fts.delete(fid)  # FTS cleanup（搜索排除不依赖它，architecture.md §11.3）

    def handle_delete_id(self, file_id: int) -> None:
        self._files.mark_deleted(file_id)
        self._fts.delete(file_id)

    def handle_rename(self, src: str, dst: str) -> None:
        """RENAME 文件身份规则（architecture.md §11.4）。

        P2.2 修复：Windows watchdog 的 rename 常伴生 created(dst) 事件——若该事件先于
        moved 被 flush，会在 dst 路径留下「同一文件的假记录」（stat 与 src 相同）。
        此时视为 rename（合并假记录，保留 src file_id + AI 产物），而非真 conflict
        （dst 为不同文件时才 rescan both）。
        """
        src = normalize(src)
        dst = normalize(dst)
        src_row = self._files.get_by_path(src)
        dst_row = self._files.get_by_path(dst)
        if dst_row and not dst_row["is_deleted"]:
            # 合并判定（假 conflict）：stat 相同（rename 伴生 created 的同一文件）
            # **且 dst 无 AI 产物**——有产物说明 dst 是真实处理过的文件，绝不合并（真 conflict）
            same_file = (
                src_row is not None
                and dst_row["mtime_ns"] == src_row["mtime_ns"]
                and dst_row["size_bytes"] == src_row["size_bytes"]
            )
            if same_file:
                with self._db.connect() as c:
                    n_chunks = c.execute(
                        "SELECT count(*) n FROM chunks WHERE file_id=?", (dst_row["id"],)
                    ).fetchone()["n"]
                if n_chunks > 0:
                    same_file = False
            if same_file:
                # 假 conflict：dst 行是 rename 伴生 created 的同一文件（stat 相同 + 无 AI 产物）
                # → 物理删除假记录（含 FTS；chunks/ocr/exif 由 FK CASCADE 级联，ai_tasks 一并清），走正常 rename
                logger.info("rename merged (watchdog created artifact) src=%s dst=%s", src, dst)
                conn = self._db.connect()
                try:
                    conn.execute("BEGIN")
                    self._fts.delete(dst_row["id"], conn=conn)  # fts_files 无 FK，须显式删
                    # ai_tasks 由 FK CASCADE 删除（防悬空任务）；chunks/ocr_text/exif 同由 cascade 处理
                    conn.execute("DELETE FROM files WHERE id=?", (dst_row["id"],))
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
                dst_row = None
            else:
                # 真 conflict：不自动覆盖、不删除目标记录——重新扫描 source + target
                logger.warning("rename conflict src=%s dst=%s → rescan both", src, dst)
                self.handle_changes([src, dst])
                return
        if src_row is None:
            self.handle_changes([dst])
            return
        # 目标不存在：保留 file_id/chunks 等，仅更新 path/filename/dir_path + fts replace
        new_filename = Path(dst).name
        new_dir = normalize(str(Path(dst).parent))
        new_ext = Path(new_filename).suffix.lower()
        conn = self._db.connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """UPDATE files SET path=?, filename=?, dir_path=?, extension=?, file_type=?,
                                    mime_type=?, updated_at=unixepoch() WHERE id=?""",
                (dst, new_filename, new_dir, new_ext,
                 file_type_for(new_ext).value, mime_type_for(new_filename), src_row["id"]),
            )
            self._fts.replace(
                src_row["id"],
                new_filename,
                self._files._seg(new_filename),
                self._files._dir_tokens(new_dir),
                conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        logger.info("rename %s → %s (file_id=%d)", src, dst, src_row["id"])

    # ================= 删除同步（扫描比对） =================

    def _sync_deleted(self, root: str) -> None:
        """扫描后比对：库中活跃但磁盘消失 → 软删除 + FTS cleanup（批处理）。"""
        known = self._files.get_active_paths(root)
        missing = [p for p in known if not os.path.exists(p)]
        for p in missing:
            self.handle_delete_path(p)
        if missing:
            logger.info("sync_deleted root=%s removed=%d", root, len(missing))
