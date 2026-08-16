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
        self._fallback_roots: list[str] = []  # U5：run() 后需增量扫描降级的 root

    # ================= 入口 =================

    def run(self, roots: list[str]) -> bool:
        """启动恢复：按卷分组读取 USN 增量事件并应用。

        返回 True = USN 路径成功（事件已处理）；False = 不可用/失败 → 调用方 fallback 增量扫描。
        - Journal 回绕/重建（_JournalRotated）→ 重置 cursor（旧 cursor 失效）
        - 读取/处理失败（crash 等）→ **保留 cursor**（未推进 → 重启重放，幂等兜底，不丢事件）
        任何卷失败只降级该卷（记录 warning），不影响其他卷。

        U5 修正：fallback 逐卷传导——`fallback_roots` 属性返回「确实需要增量扫描降级的 root」
        （失败卷的 roots），成功卷不重复扫描（原实现 all-or-nothing：任一卷失败 → 全部 root 重扫，
        成功卷白做一遍）。返回 bool 保持向后兼容。
        """
        self._fallback_roots = list(roots)  # 默认全量 fallback；成功卷逐步剔除
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
                for r in vol_roots:
                    if r in self._fallback_roots:
                        self._fallback_roots.remove(r)
        return any_ok

    @property
    def fallback_roots(self) -> list[str]:
        """run() 后需增量扫描降级的 root 列表（成功卷已剔除；未调用 run 时为空）。"""
        return self._fallback_roots

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
        else:
            # U6：到达 MAX_BATCHES 上限仍每批满载 → 截断。必须告警——
            # 未消费事件依赖下次增量/全量扫描的 sync_deleted/upsert 兜底，不能静默丢。
            logger.warning(
                "USN journal exceeds MAX_BATCHES=%d for %s; tail events deferred to scan reconcile",
                MAX_BATCHES, vol,
            )

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
        unresolved_new: set[int] = set()  # U7：RENAME_NEW 路径解析失败（MFT 瞬态不可读等）
        for rec in records:
            path = self._reader.resolve_path(vol, rec.parent_ref, rec.filename)
            if path is None:
                # U7：RENAME_NEW 解析失败 ≠ 「rename 出 root」——可能只是 MFT 记录瞬态
                # 不可读（root 内 rename）。必须区分，否则会把仍存在的文件误删。
                if rec.reason & USN_REASON_RENAME_NEW_NAME:
                    unresolved_new.add(rec.file_ref)
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

        # rename 配对（U3 修正）：先确定 rename 涉及的 file_ref，其 old/new 路径
        # 不得再被 upsert 软删（「修改+重命名」时 upsert 旧路径会置 is_deleted=1，
        # 而 handle_rename 只改 path 不重置 → 新路径行以 deleted 落地、文件消失）。
        rename_refs = set(old_paths) & set(new_paths)
        for frn in rename_refs:
            upserts.pop(frn, None)
            deletes.pop(frn, None)

        # 应用顺序：delete（清理，排除已 rename 的）→ rename（保留 file_id）→ upsert（其余增改）
        for frn, path in deletes.items():
            self._index.handle_delete_path(path)
        for frn in old_paths:
            old = old_paths[frn]
            new = new_paths.get(frn)
            if new:
                if old == new:
                    # U4 防护：目录重命名时子文件 OLD/NEW 记录都解析到新路径（历史名无法
                    # 从 MFT 重建）——src==dst 的 rename 会新建错误 file_id + 幽灵旧行，
                    # 丢弃该事件（下次全量/增量扫描的 sync_deleted 兜底清理）
                    logger.debug("usn rename src==dst (%s) → discard (dir-rename artifact)", old)
                    continue
                self._index.handle_rename(old, new)  # 保留 file_id（MVP rename 规则）
            elif frn in unresolved_new:
                # U7：NEW 解析失败（root 内 rename 的瞬态不可读）——不删旧路径，避免误删
                # 仍存在的文件；由下次增量/全量扫描的 sync_deleted/upsert 兜底收敛。
                logger.warning(
                    "usn rename with unresolved NEW (file_ref=%d, old=%s) → defer, avoid wrongful delete",
                    frn, old,
                )
            else:
                self._index.handle_delete_path(old)  # rename 出 active root → 旧路径消失
        if upserts:
            self._index.handle_changes(list(upserts.values()))
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
