"""WatchService —— 实时文件监听（architecture.md §11.4，ReadDirectoryChangesW）。

- 事件线程只做「入缓冲」（不做耗时操作）；防抖 2s 后由 Timer 线程批量处理
- 合并规则：CREATE+DELETE 忽略；CREATE+MODIFY 合并为一次增量 upsert
- RENAME 走 IndexService.handle_rename（保留 file_id / conflict 重新扫描，architecture.md §11.4）
- 监听根 = settings 中的 index roots（应用启动时恢复，architecture.md §11.4）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

logger = logging.getLogger("omnisearch.server.watch")

DEBOUNCE_S = 2.0  # 防抖窗口（可调参数）

_CREATED = "created"
_MODIFIED = "modified"
_DELETED = "deleted"


class _Handler(FileSystemEventHandler):
    """watchdog 事件回调：只入缓冲（线程安全），不执行耗时操作。"""

    def __init__(self, svc: "WatchService"):
        self._svc = svc

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._svc._queue_event(event.src_path, _CREATED)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._svc._queue_event(event.src_path, _MODIFIED)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if not event.is_directory:
            self._svc._queue_event(event.src_path, _DELETED)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._svc._queue_moved(event.src_path, event.dest_path)


class WatchService:
    def __init__(
        self,
        on_changes: Callable[[list[str]], None],
        on_deleted_paths: Callable[[list[str]], None],
        on_renamed: Callable[[str, str], None],
        debounce_s: float = DEBOUNCE_S,
    ):
        """回调由调用方注入（IndexService.handle_changes 等）；在 Timer 工作线程执行。"""
        self._on_changes = on_changes
        self._on_deleted = on_deleted_paths
        self._on_renamed = on_renamed
        self._debounce_s = debounce_s
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, set[str]] = {}
        self._moved: list[tuple[str, str]] = []
        self._timer: threading.Timer | None = None

    # ---------------- 生命周期 ----------------

    def start(self, roots: list[str]) -> None:
        """启动监听（对失效 root 容错：不存在/不可访问 → warning + 跳过，不阻塞其他 root）。"""
        if not roots or self._observer:
            return
        valid = self._valid_roots(roots)
        if not valid:
            logger.warning("no valid watch roots, file watching disabled")
            return
        observer = Observer()
        handler = _Handler(self)
        for root in valid:
            observer.schedule(handler, root, recursive=True)
        try:
            observer.start()
        except Exception:  # noqa: BLE001 —— 打开句柄失败等竞态：不阻塞 lifespan
            logger.warning("watch observer failed to start, file watching disabled", exc_info=True)
            return
        self._observer = observer
        logger.info("watch started roots=%s", valid)

    def add_roots(self, roots: list[str]) -> None:
        """动态添加监听根（首次扫描后调用；未启动时启动，已启动则追加 schedule）。"""
        if not roots:
            return
        valid = self._valid_roots(roots)
        if not valid:
            return
        if self._observer is None:
            self.start(roots)
            return
        for root in valid:
            self._observer.schedule(_Handler(self), root, recursive=True)
        logger.info("watch roots added: %s", valid)

    @staticmethod
    def _valid_roots(roots: list[str]) -> list[str]:
        """过滤存在且可访问的 root；无效 root 记 warning 并跳过（架构：单 root 失效不影响整体）。"""
        valid = []
        for root in roots:
            if os.path.isdir(root):  # 不存在/无权限 → False（os.path.isdir 内部吞 OSError）
                valid.append(root)
            else:
                logger.warning("watch root invalid, skipped: %s (not a directory or inaccessible)", root)
        return valid

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        logger.info("watch stopped")

    # ---------------- 事件缓冲（watchdog 线程） ----------------

    def _queue_event(self, path: str, kind: str) -> None:
        with self._lock:
            self._pending.setdefault(path, set()).add(kind)
        self._reset_timer()

    def _queue_moved(self, src: str, dest: str) -> None:
        with self._lock:
            self._moved.append((src, dest))
            # 排除与 moved 重叠的增删事件（Windows 上 rename 可能伴生 created/deleted）
            self._pending.pop(src, None)
            self._pending.pop(dest, None)
        self._reset_timer()

    def _reset_timer(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._flush)
            self._timer.daemon = True
            self._timer.start()

    # ---------------- 批量处理（Timer 工作线程，防抖后） ----------------

    def _flush(self) -> None:
        with self._lock:
            pending = dict(self._pending)
            moved = list(self._moved)
            self._pending.clear()
            self._moved.clear()
            self._timer = None

        changes: list[str] = []
        deletes: list[str] = []
        for path, kinds in pending.items():
            if _CREATED in kinds and _DELETED in kinds:
                continue  # CREATE + DELETE → 忽略
            if _DELETED in kinds:
                deletes.append(path)
            else:
                changes.append(path)

        if changes:
            self._on_changes(changes)
        if deletes:
            self._on_deleted(deletes)
        for src, dest in moved:
            self._on_renamed(src, dest)
