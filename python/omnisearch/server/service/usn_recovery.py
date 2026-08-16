"""USN 启动恢复服务（P2.1）。

流程：启动时对每个 NTFS 卷读取 Journal 增量事件（CREATE/MODIFY/DELETE/RENAME）→
归一化后复用 IndexService.handle_changes/handle_delete_path/handle_rename（不复制索引逻辑）→
事件处理成功后更新 cursor（settings KV，崩溃时 cursor 落后 → 重启重放，靠幂等兜底）。

降级（spec P2.1 §1/§8/§9）：
- 非 NTFS / Journal 不可用 / 权限不足 / Journal 回绕（journal_id 变化或 cursor < LowestValidUsn）
  → 返回 False，调用方 fallback 启动增量扫描（mtime+size），同时重置 cursor。
- 单事件路径解析失败（MFT 记录已释放等）→ 丢弃并记 warning（delete 兜底由调用方 sync_deleted 覆盖）。

事件语义（USN → IndexService）：
- FILE_CREATE / DATA_EXTEND / DATA_OVERWRITE → handle_changes（存在→upsert，不存在→delete）
- FILE_DELETE → handle_delete_path
- RENAME_OLD_NAME + RENAME_NEW_NAME（同 file_ref 配对）→ handle_rename（保留 file_id）
  只有 OLD（rename 出 active root）→ handle_delete_path(旧路径)；只有 NEW（rename 进 root）→ handle_changes(新路径)
- 事件路径必须属于 active/enabled root（root_covers），否则丢弃（多 root 同卷共享 journal）
"""
from __future__ import annotations

import logging
import time

from omnisearch.common.database import Database
from omnisearch.common.utils.paths import root_covers, root_key
from omnisearch.server.repository.settings import SettingsRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.usn import (
    USN_REASON_EXTEND,
    USN_REASON_FILE_CREATE,
    USN_REASON_FILE_DELETE,
    USN_REASON_OVERWRITE,
    USN_REASON_RENAME_NEW_NAME,
    USN_REASON_RENAME_OLD_NAME,
)

logger = logging.getLogger("omnisearch.usn_recovery")

USN_CURSOR_KEY = "usn_cursor"  # settings KV: {volume: {journal_id, usn, ts}}
MAX_BATCHES = 200  # 单次启动恢复的最大读取批次（防异常长 journal 拖慢启动）


class _JournalRotated(Exception):
    """Journal 已回绕/重建：旧 cursor 失效 → 需重置（区别于 crash：crash 保留 cursor 供重放）。"""


class UsnRecoveryService:
    def __init__(
        self,
        db: Database,
        index: IndexService,
        settings: SettingsRepository,
        reader=None,
    ):
        self._db = db
        self._index = index
        self._settings = settings
        self._reader = reader  # UsnReader 或测试注入的 fake；None → 永远降级

    # ================= 入口 =================

    def run(self, roots: list[str]) -> bool:
        """启动恢复：按卷分组读取 USN 增量事件并应用。

        返回 True = USN 路径成功（事件已处理）；False = 不可用/失败 → 调用方 fallback 增量扫描。
        - Journal 回绕/重建（_JournalRotated）→ 重置 cursor（旧 cursor 失效）
        - 读取/处理失败（crash 等）→ **保留 cursor**（未推进 → 重启重放，幂等兜底，不丢事件）
        任何卷失败只降级该卷（记录 warning），不影响其他卷。
        """
        if not roots or self._reader is None:
            return False
        volumes: dict[str, list[str]] = {}
        for r in roots:
            vol = self._reader.volume_for(r)
            if vol:
                volumes.setdefault(vol, []).append(r)
        if not volumes:
            logger.warning("USN 不可用（无 NTFS 卷），已使用增量扫描")
            return False

        any_ok = False
        for vol, vol_roots in volumes.items():
            try:
                ok = self._recover_volume(vol, vol_roots)
            except _JournalRotated:
                logger.warning("USN journal rotated for %s，已使用增量扫描", vol)
                self._reset_cursor(vol)
                ok = False
            except Exception:  # noqa: BLE001 —— USN 故障不得阻塞应用启动；cursor 保留供重放
                logger.warning("USN recovery failed for %s，已使用增量扫描", vol, exc_info=True)
                ok = False
            if ok:
                any_ok = True
        return any_ok

    # ================= 单卷恢复 =================

    def _recover_volume(self, vol: str, roots: list[str]) -> bool:
        journal = self._reader.query_journal(vol)
        if journal is None:
            logger.warning("USN 不可用（%s：Journal 不存在或权限不足），已使用增量扫描", vol)
            return False
        journal_id = journal.UsnJournalID
        cursors = self._settings.get(USN_CURSOR_KEY, {}) or {}
        cursor = cursors.get(vol)

        if cursor is None:
            # 首次启用：无历史关闭期 → 直接建立 cursor（跳过 journal 历史）
            self._save_cursor(vol, journal_id, journal.NextUsn)
            return True
        if cursor.get("journal_id") != journal_id:
            # Journal 已重建/回绕：旧 cursor 无效 → 降级增量扫描并重置 cursor
            raise _JournalRotated(f"journal_id changed for {vol}")
        if cursor.get("usn", 0) < journal.LowestValidUsn:
            # 回绕：cursor 已不在有效范围
            raise _JournalRotated(f"cursor below LowestValidUsn for {vol}")

        # 读取 cursor 之后的事件（分批，防长 journal 拖慢启动）
        start_usn = cursor["usn"]
        all_records = []
        last_usn = start_usn
        for _ in range(MAX_BATCHES):
            records, next_usn, ok = self._reader.read_batch(vol, start_usn, journal_id)
            if not ok:
                logger.warning("USN read failed for %s，已使用增量扫描", vol)
                return False
            if not records:
                break
            all_records.extend(records)
            last_usn = next_usn
            start_usn = next_usn
            if len(records) < 100:  # 不足一批 → journal 已追平
                break

        if not all_records:
            return True  # 无变化，cursor 无需推进

        # 事件归一化（按 file_ref 配对 rename）→ root 过滤 → 应用
        self._apply_records(vol, all_records, roots)
        # 全部成功应用后才推进 cursor（崩溃 → cursor 落后 → 重启重放，幂等兜底）
        self._save_cursor(vol, journal_id, last_usn)
        logger.info(
            "USN recovery %s: %d records applied (cursor %d → %d)",
            vol, len(all_records), cursor["usn"], last_usn,
        )
        return True

    # ================= 事件归一化 + 应用 =================

    def _apply_records(self, vol: str, records, roots: list[str]) -> None:
        active_keys = [root_key(r) for r in roots]

        def belongs(path: str) -> bool:
            return any(root_covers(path, key) for key in active_keys)

        # 按 file_ref 聚合：确定每条路径的最终状态
        old_paths: dict[int, str] = {}   # file_ref → 旧路径（RENAME_OLD）
        new_paths: dict[int, str] = {}   # file_ref → 新路径（RENAME_NEW）
        upserts: dict[int, str] = {}     # file_ref → 路径（CREATE/EXTEND/OVERWRITE）
        deletes: dict[int, str] = {}     # file_ref → 路径（DELETE）
        for rec in records:
            path = self._reader.resolve_path(vol, rec.parent_ref, rec.filename)
            if path is None:
                logger.debug("usn path resolve failed (file_ref=%d) → discard", rec.file_ref)
                continue
            if not belongs(path):
                continue  # 不属于 active root（多 root 同卷共享 journal 时过滤）
            if rec.reason & USN_REASON_FILE_DELETE:
                deletes[rec.file_ref] = path
            elif rec.reason & USN_REASON_RENAME_OLD_NAME:
                old_paths[rec.file_ref] = path
            elif rec.reason & USN_REASON_RENAME_NEW_NAME:
                new_paths[rec.file_ref] = path
            elif rec.reason & (USN_REASON_FILE_CREATE | USN_REASON_EXTEND | USN_REASON_OVERWRITE):
                upserts[rec.file_ref] = path

        # 应用顺序：先 delete（清理）→ upsert（create/modify）→ rename
        for path in deletes.values():
            self._index.handle_delete_path(path)
        if upserts:
            self._index.handle_changes(list(upserts.values()))
        for frn in old_paths:
            old = old_paths[frn]
            new = new_paths.get(frn)
            if new:
                self._index.handle_rename(old, new)  # 保留 file_id（MVP rename 规则）
            else:
                self._index.handle_delete_path(old)  # rename 出 active root → 旧路径消失
        for frn in new_paths:
            if frn not in old_paths:
                self._index.handle_changes([new_paths[frn]])  # rename 进 active root

    # ================= cursor =================

    def _save_cursor(self, vol: str, journal_id: int, usn: int) -> None:
        cursors = self._settings.get(USN_CURSOR_KEY, {}) or {}
        cursors[vol] = {"journal_id": journal_id, "usn": usn, "ts": int(time.time())}
        self._settings.set(USN_CURSOR_KEY, cursors)

    def _reset_cursor(self, vol: str) -> None:
        cursors = self._settings.get(USN_CURSOR_KEY, {}) or {}
        cursors.pop(vol, None)
        self._settings.set(USN_CURSOR_KEY, cursors)
