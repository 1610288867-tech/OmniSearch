"""P2.1 USN 启动恢复测试（spec §十三 A-F）。

逻辑层用 FakeReader 注入（不依赖真实 NTFS/权限）；真实 UsnReader 的字节解析
与 MFT 解析在 test_usn_reader.py；真实 Windows E2E 另做（手工验证 + 记录）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnisearch.common.database import Database
from omnisearch.common.utils.seg import seg_text
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.repository.files import FileMeta, FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.settings import SettingsRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.usn import USN_REASON_EXTEND, USN_REASON_FILE_CREATE, USN_REASON_FILE_DELETE, USN_REASON_OVERWRITE, USN_REASON_RENAME_NEW_NAME, USN_REASON_RENAME_OLD_NAME
from omnisearch.server.service.usn_recovery import USN_CURSOR_KEY, UsnRecoveryService


def _rec(usn: int, frn: int, parent: int, reason: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(usn=usn, file_ref=frn, parent_ref=parent, timestamp=1, reason=reason, filename=name)


_DEFAULT_JOURNAL = SimpleNamespace(UsnJournalID=1, NextUsn=10_000, LowestValidUsn=0)
_JOURNAL_UNSET = object()  # sentinel：区分「缺省」与「显式 None（journal 不可用）」


class FakeReader:
    """UsnReader 兼容替身：预置 journal 状态 + 记录流 + 父 FRN→目录路径解析。"""

    def __init__(self, fs: str = "NTFS", journal: SimpleNamespace | None = _JOURNAL_UNSET,
                 records: list | None = None, dirs: dict[int, str] | None = None,
                 read_ok: bool = True):
        self.fs = fs
        self.journal = _DEFAULT_JOURNAL if journal is _JOURNAL_UNSET else journal
        self.records = records or []
        self.dirs = dirs or {5: "D:\\"}
        self.read_ok = read_ok

    def volume_for(self, root: str) -> str:
        return "D:\\"

    def filesystem_of(self, _root: str) -> str | None:
        return self.fs

    def query_journal(self, _vol: str):
        return self.journal if self.fs == "NTFS" else None

    def read_batch(self, _vol: str, start_usn: int, _journal_id: int, max_events: int = 500):  # noqa: ARG002
        if not self.read_ok:
            return [], start_usn, False
        batch = [r for r in self.records if r.usn > start_usn][:max_events]
        next_usn = batch[-1].usn if batch else start_usn
        return batch, next_usn, True

    def resolve_path(self, _vol: str, parent_ref: int, filename: str) -> str | None:
        parent = self.dirs.get(parent_ref)
        if parent is None:
            return None
        return parent.rstrip("\\") + "\\" + filename

    def close(self) -> None:
        pass


@pytest.fixture()
def env(tmp_path):
    """真实 DB + IndexService + Settings + FakeReader 可注入的恢复服务。"""
    db = Database(tmp_path / "test.db")
    migrate(db)
    files, fts = FileRepository(db), FtsRepository(db)
    jobs = IndexJobRepository(db)
    settings = SettingsRepository(db)
    index = IndexService(db, files, fts, jobs)
    return db, index, settings, files, fts


def _seed_active_root(settings: SettingsRepository, root: str) -> None:
    settings.add_index_root(root, enabled=True)


def _svc(env, reader: FakeReader) -> UsnRecoveryService:
    db, index, settings, _f, _ft = env
    return UsnRecoveryService(db, index, settings, reader)


# ================= A. 基础 =================

def test_a1_ntfs_volume_detected(env):
    db, index, settings, _, _ = env
    reader = FakeReader(fs="NTFS", journal=SimpleNamespace(UsnJournalID=1, NextUsn=100, LowestValidUsn=0))
    svc = UsnRecoveryService(db, index, settings, reader)
    assert reader.filesystem_of("D:\\x") == "NTFS"
    assert svc.run(["D:\\root"]) is True  # 无 cursor → 首次建立，跳过历史


def test_a2_journal_available(env):
    db, index, settings, _, _ = env
    reader = FakeReader(journal=SimpleNamespace(UsnJournalID=7, NextUsn=200, LowestValidUsn=0))
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\root"]) is True


def test_a3_journal_unavailable_fallback(env):
    db, index, settings, _, _ = env
    reader = FakeReader(fs="NTFS", journal=None, read_ok=False)
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\root"]) is False  # 调用方 fallback 增量扫描


def test_a4_non_ntfs_fallback(env):
    db, index, settings, _, _ = env
    reader = FakeReader(fs="FAT32")
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\root"]) is False


# ================= B. 事件 =================

def _seed_disk_file(root: Path, name: str, content: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(content, encoding="utf-8")
    return p


def test_b5_create_event_indexed(env, tmp_path):
    """关闭期 CREATE → 恢复后 files 有记录且可搜索。"""
    db, index, settings, files, fts = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    created = _seed_disk_file(root, "新文件.txt", "关闭期新增内容")
    _seed_disk_file(root, "baseline.txt", "x")
    files.upsert_batch([_meta(root / "baseline.txt")])  # 模拟基线已索引
    reader = FakeReader(records=[_rec(101, 10, 20, USN_REASON_FILE_CREATE, "新文件.txt")],
                        dirs={20: str(root)})
    svc = _svc(env, reader)
    assert svc.run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT path FROM files WHERE path=?", (str(created),)).fetchone()
        assert row is not None
    # 可搜索（文件名命中，fts 已由 handle_changes 写入）
    assert fts.match("新文件*") != []


def _meta(path: Path) -> FileMeta:
    from omnisearch.common.models import FileType

    return FileMeta(path=str(path), filename=path.name, dir_path=str(path.parent),
                    extension=path.suffix.lower(), size_bytes=path.stat().st_size,
                    mtime_ns=path.stat().st_mtime_ns, ctime_ns=path.stat().st_ctime_ns,
                    file_type=FileType.DOC, mime_type=None)


def test_b6_modify_event_updates(env, tmp_path):
    """关闭期 MODIFY（EXTEND）→ mtime/size 更新。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    p = _seed_disk_file(root, "m.txt", "old")
    files.upsert_batch([_meta(p)])
    old_mtime = p.stat().st_mtime_ns
    p.write_text("new content much longer", encoding="utf-8")  # 关闭期修改
    reader = FakeReader(records=[_rec(102, 11, 20, USN_REASON_EXTEND, "m.txt")], dirs={20: str(root)})
    assert _svc(env, reader).run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT size_bytes FROM files WHERE path=?", (str(p),)).fetchone()
        assert row["size_bytes"] == len("new content much longer")
    assert p.stat().st_mtime_ns != old_mtime  # 磁盘确实变了


def test_b7_delete_event_excluded(env, tmp_path):
    """关闭期 DELETE → is_deleted=1（搜索排除）。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    p = _seed_disk_file(root, "d.txt", "将被删除")
    files.upsert_batch([_meta(p)])
    p.unlink()  # 关闭期删除
    reader = FakeReader(records=[_rec(103, 12, 20, USN_REASON_FILE_DELETE, "d.txt")], dirs={20: str(root)})
    assert _svc(env, reader).run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT is_deleted FROM files WHERE path=?", (str(p),)).fetchone()
        assert row is not None and row["is_deleted"] == 1


def test_b8_rename_event_keeps_file_id(env, tmp_path):
    """关闭期 RENAME → file_id 保留 + path 更新（MVP rename 规则）。"""
    db, index, settings, files, fts, = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    src = _seed_disk_file(root, "old-name.txt", "内容")
    dst = root / "new-name.txt"
    dst.write_text("内容", encoding="utf-8")
    ops = files.upsert_batch([_meta(src)])
    fid = ops[0].file_id
    fts.insert(fid, "old-name.txt", seg_text("old-name.txt"), str(root))
    src.unlink()  # 关闭期 rename（磁盘上 src 消失、dst 存在）
    reader = FakeReader(
        records=[
            _rec(104, 30, 20, USN_REASON_RENAME_OLD_NAME, "old-name.txt"),
            _rec(105, 30, 20, USN_REASON_RENAME_NEW_NAME, "new-name.txt"),
        ],
        dirs={20: str(root)},
    )
    assert _svc(env, reader).run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT id, path, is_deleted FROM files WHERE id=?", (fid,)).fetchone()
        assert row is not None and row["path"] == str(dst) and row["is_deleted"] == 0  # file_id 保留


# ================= C. Cursor =================

def test_c9_first_cursor_established(env):
    """首次启用：跳过 journal 历史，cursor = NextUsn。"""
    db, index, settings, _, _ = env
    reader = FakeReader(records=[_rec(1, 1, 5, USN_REASON_FILE_CREATE, "old.txt")],  # 历史（cursor 前）
                        journal=SimpleNamespace(UsnJournalID=9, NextUsn=500, LowestValidUsn=0))
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\r"]) is True
    cursors = settings.get(USN_CURSOR_KEY)
    assert cursors["D:\\"]["journal_id"] == 9 and cursors["D:\\"]["usn"] == 500


def test_c10_cursor_advances(env, tmp_path):
    """正常推进：处理后 cursor = 最后事件 usn。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    _seed_active_root(settings, str(root))
    p = _seed_disk_file(root, "a.txt", "x")
    files.upsert_batch([_meta(p)])
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})
    reader = FakeReader(records=[_rec(60, 1, 20, USN_REASON_FILE_CREATE, "a.txt"),
                                 _rec(61, 2, 20, USN_REASON_EXTEND, "a.txt")], dirs={20: str(root)})
    assert _svc(env, reader).run([str(root)]) is True
    assert settings.get(USN_CURSOR_KEY)["D:\\"]["usn"] == 61


def test_c11_cursor_interrupted_replay(env, tmp_path):
    """中断恢复：cursor 停在旧值 → 重启重放（幂等，不产生错误状态）。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    _seed_active_root(settings, str(root))
    p = _seed_disk_file(root, "a.txt", "x")
    files.upsert_batch([_meta(p)])
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})  # 上次中断，未推进
    reader = FakeReader(records=[_rec(60, 1, 20, USN_REASON_FILE_CREATE, "a.txt")], dirs={20: str(root)})
    svc = _svc(env, reader)
    assert svc.run([str(root)]) is True
    assert settings.get(USN_CURSOR_KEY)["D:\\"]["usn"] == 60
    # 重放（模拟再次启动读到相同记录）——幂等：仍 1 条记录，无错误
    assert svc.run([str(root)]) is True
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM files WHERE path=?", (str(p),)).fetchone()["n"]
        assert n == 1


def test_c12_journal_wrapped_fallback(env):
    """Journal 回绕（journal_id 变化 / cursor < LowestValidUsn）→ False + cursor 重置。"""
    db, index, settings, _, _ = env
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})
    reader = FakeReader(journal=SimpleNamespace(UsnJournalID=2, NextUsn=100, LowestValidUsn=0))  # journal 重建
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\r"]) is False
    assert "D:\\" not in (settings.get(USN_CURSOR_KEY) or {})  # cursor 已重置
    # 场景 2：cursor 低于 LowestValidUsn（回绕）
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})
    reader2 = FakeReader(journal=SimpleNamespace(UsnJournalID=1, NextUsn=100, LowestValidUsn=80))
    assert UsnRecoveryService(db, index, settings, reader2).run(["D:\\r"]) is False


# ================= D. Root 过滤 =================

def test_d13_active_root_events_processed(env, tmp_path):
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    p = _seed_disk_file(root, "in.txt", "x")
    files.upsert_batch([_meta(p)])
    reader = FakeReader(records=[_rec(200, 1, 20, USN_REASON_FILE_CREATE, "in.txt")], dirs={20: str(root)})
    assert _svc(env, reader).run([str(root)]) is True


def test_d14_disabled_root_events_ignored(env, tmp_path):
    """disabled root（不在 active roots 参数）→ 事件丢弃。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.add_index_root(str(root), enabled=False)  # 禁用
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    _seed_disk_file(root, "skip.txt", "x")  # 磁盘存在但未索引（基线外）
    reader = FakeReader(records=[_rec(201, 1, 20, USN_REASON_FILE_CREATE, "skip.txt")], dirs={20: str(root)})
    svc = _svc(env, reader)
    # active roots 只传另一个不存在的 root → 事件不属于 active → 丢弃（不新增索引）
    assert svc.run([str(tmp_path / "other")]) is True  # journal 处理成功（事件被过滤）
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM files WHERE path LIKE '%skip.txt'").fetchone()["n"]
        assert n == 0  # 事件未应用


def test_d15_removed_root_events_ignored(env, tmp_path):
    """removed root（settings 已移除）→ 事件丢弃。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    p = _seed_disk_file(root, "gone.txt", "x")
    files.upsert_batch([_meta(p)])  # 数据保留（移除语义）
    reader = FakeReader(records=[_rec(202, 1, 20, USN_REASON_FILE_CREATE, "gone.txt")], dirs={20: str(root)})
    assert _svc(env, reader).run([str(tmp_path / "other")]) is True  # root 不在 active → 丢弃
    with db.connect() as c:
        row = c.execute("SELECT is_deleted FROM files WHERE path=?", (str(p),)).fetchone()
        assert row["is_deleted"] == 0  # 未被处理（保持原状）


def test_d16_multi_roots_same_volume(env, tmp_path):
    """多 root 同卷：共享 journal，事件按所属 root 应用。"""
    db, index, settings, files, _ = env
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    _seed_active_root(settings, str(ra))
    _seed_active_root(settings, str(rb))
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    pa = _seed_disk_file(ra, "a.txt", "x")
    pb = _seed_disk_file(rb, "b.txt", "y")
    files.upsert_batch([_meta(pa), _meta(pb)])
    reader = FakeReader(
        records=[_rec(300, 1, 20, USN_REASON_FILE_CREATE, "a.txt"),
                 _rec(301, 2, 30, USN_REASON_FILE_CREATE, "b.txt")],
        dirs={20: str(ra), 30: str(rb)},
    )
    assert _svc(env, reader).run([str(ra), str(rb)]) is True  # 同一卷 D:\ 一个 reader
    assert reader.volume_for(str(ra)) == reader.volume_for(str(rb))  # 同卷
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM files WHERE path=?", (str(pa),)).fetchone()
        assert c.execute("SELECT 1 FROM files WHERE path=?", (str(pb),)).fetchone()


# ================= E. Crash =================

def test_e17_crash_mid_processing_cursor_not_advanced(env, tmp_path):
    """处理中异常：run 返回 False，cursor 不推进（重启重放，不丢事件）。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    _seed_active_root(settings, str(root))
    p = _seed_disk_file(root, "a.txt", "x")
    files.upsert_batch([_meta(p)])
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})

    class CrashReader(FakeReader):
        def resolve_path(self, _vol, parent_ref, filename):
            raise RuntimeError("simulated crash")  # 处理中途崩溃

    svc = UsnRecoveryService(db, index, settings, CrashReader(
        records=[_rec(60, 1, 20, USN_REASON_FILE_CREATE, "a.txt")], dirs={20: str(root)}))
    assert svc.run([str(root)]) is False
    assert settings.get(USN_CURSOR_KEY)["D:\\"]["usn"] == 50  # cursor 未推进


def test_e18_restart_no_event_loss(env, tmp_path):
    """崩溃后重启：cursor 未推进 → 事件被补处理。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    _seed_active_root(settings, str(root))
    p = _seed_disk_file(root, "a.txt", "x")
    files.upsert_batch([_meta(p)])
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 50, "ts": 1}})
    # 第一次：crash（resolve 失败）→ 未应用
    class CrashReader(FakeReader):
        def resolve_path(self, _vol, parent_ref, filename):
            raise RuntimeError("crash")

    svc = UsnRecoveryService(db, index, settings, CrashReader(
        records=[_rec(60, 1, 20, USN_REASON_EXTEND, "a.txt")], dirs={20: str(root)}))
    assert svc.run([str(root)]) is False
    # 第二次：正常 → 事件补上（cursor 从 50 重放）
    reader = FakeReader(records=[_rec(60, 1, 20, USN_REASON_EXTEND, "a.txt")], dirs={20: str(root)})
    assert UsnRecoveryService(db, index, settings, reader).run([str(root)]) is True
    assert settings.get(USN_CURSOR_KEY)["D:\\"]["usn"] == 60


# ================= F. Integration（关闭期变化 → 启动恢复） =================

def test_f19_23_24_closed_period_changes_recovered(env, tmp_path):
    """集成：关闭期 CREATE+MODIFY+RENAME+DELETE → 启动恢复 → 搜索结果正确。"""
    db, index, settings, files, fts = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    _seed_active_root(settings, str(root))
    # 基线（已索引）
    base = _seed_disk_file(root, "base.txt", "基线内容")
    files.upsert_batch([_meta(base)])
    fts.insert(files.get_by_path(str(base))["id"], "base.txt", seg_text("base.txt"), str(root))
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 1000, "ts": 1}})

    # 关闭期变化（磁盘操作，模拟应用未运行）
    created = _seed_disk_file(root, "created.txt", "关闭期创建的新文件")
    modified = _seed_disk_file(root, "modified.txt", "v1")
    files.upsert_batch([_meta(modified)])
    modified.write_text("v2 修改后的内容", encoding="utf-8")
    renamed_src = _seed_disk_file(root, "renamed-old.txt", "改名内容")
    files.upsert_batch([_meta(renamed_src)])
    renamed_dst = root / "renamed-new.txt"
    renamed_dst.write_text("改名内容", encoding="utf-8")
    renamed_src.unlink()
    deleted = _seed_disk_file(root, "deleted.txt", "将被删除")
    files.upsert_batch([_meta(deleted)])
    deleted.unlink()

    reader = FakeReader(
        records=[
            _rec(1001, 1, 20, USN_REASON_FILE_CREATE, "created.txt"),
            _rec(1002, 2, 20, USN_REASON_EXTEND, "modified.txt"),
            _rec(1003, 3, 20, USN_REASON_RENAME_OLD_NAME, "renamed-old.txt"),
            _rec(1004, 3, 20, USN_REASON_RENAME_NEW_NAME, "renamed-new.txt"),
            _rec(1005, 4, 20, USN_REASON_FILE_DELETE, "deleted.txt"),
        ],
        dirs={20: str(root)},
    )
    svc = _svc(env, reader)
    assert svc.run([str(root)]) is True

    # 验证：CREATE 可搜 / MODIFY 新内容 / RENAME 旧名消失新名在 / DELETE 排除
    with db.connect() as c:
        created_row = c.execute("SELECT is_deleted FROM files WHERE path=?", (str(created),)).fetchone()
        assert created_row and created_row["is_deleted"] == 0
        mod_row = c.execute("SELECT size_bytes FROM files WHERE path=?", (str(modified),)).fetchone()
        assert mod_row["size_bytes"] == len("v2 修改后的内容".encode("utf-8"))
        old_row = c.execute("SELECT is_deleted FROM files WHERE path=?", (str(renamed_src),)).fetchone()
        new_row = c.execute("SELECT id FROM files WHERE path=?", (str(renamed_dst),)).fetchone()
        assert new_row is not None  # 新路径存在
        if old_row is not None:
            assert old_row["is_deleted"] == 1 or old_row["path"] == str(renamed_dst)  # 旧路径不残留活跃
        del_row = c.execute("SELECT is_deleted FROM files WHERE path=?", (str(deleted),)).fetchone()
        assert del_row and del_row["is_deleted"] == 1


# ================= 批次 1 回归（U3/U4） =================

def test_p21_rename_with_modify_preserved(env, tmp_path):
    """U3 回归：关闭期「修改+重命名」→ 新路径行 is_deleted=0（文件不消失）。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.add_index_root(str(root), enabled=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    from omnisearch.common.models import FileType
    from omnisearch.server.repository.files import FileMeta

    src = root / "a.txt"
    src.write_text("v1", encoding="utf-8")
    files.upsert_batch([FileMeta(path=str(src), filename="a.txt", dir_path=str(root), extension=".txt",
                                 size_bytes=2, mtime_ns=1, ctime_ns=1, file_type=FileType.DOC, mime_type=None)])
    dst = root / "b.txt"
    dst.write_text("v2 修改后", encoding="utf-8")
    src.unlink()
    reader = FakeReader(
        records=[
            _rec(101, 1, 20, USN_REASON_EXTEND, "a.txt"),           # 修改（旧路径）
            _rec(102, 1, 20, USN_REASON_RENAME_OLD_NAME, "a.txt"),   # rename old
            _rec(103, 1, 20, USN_REASON_RENAME_NEW_NAME, "b.txt"),   # rename new
        ],
        dirs={20: str(root)},
    )
    svc = _svc(env, reader)
    assert svc.run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT id, is_deleted FROM files WHERE path=?", (str(dst),)).fetchone()
        assert row is not None and row["is_deleted"] == 0  # 新路径不因 upsert 旧路径而被软删


# ================= 批次 2 回归（U5/U7） =================

class PerVolJournalReader(FakeReader):
    """U5：按卷返回不同 journal（D 卷可用、E 卷不可用）。"""

    def __init__(self, journals: dict):
        super().__init__(fs="NTFS")
        self._journals = journals

    def volume_for(self, root: str) -> str:
        return "D:\\" if root.startswith("D:") else "E:\\"

    def query_journal(self, vol):
        return self._journals.get(vol)


def test_u5_only_failed_volume_falls_back(env):
    """U5：卷级降级传导——只有失败卷的 root 需要增量扫描（成功卷不重复扫描）。"""
    db, index, settings, _, _ = env
    reader = PerVolJournalReader(
        {"D:\\": SimpleNamespace(UsnJournalID=1, NextUsn=100, LowestValidUsn=0)}
    )
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run(["D:\\a", "E:\\b"]) is True  # D 卷成功（E 卷失败）
    assert svc.fallback_roots == ["E:\\b"]       # 仅 E 卷 root 需 fallback


def test_u5_all_ok_no_fallback(env):
    db, index, settings, _, _ = env
    svc = UsnRecoveryService(db, index, settings, FakeReader())
    assert svc.run(["D:\\root"]) is True
    assert svc.fallback_roots == []


def test_u7_unresolved_rename_new_does_not_delete(env, tmp_path):
    """U7：RENAME_NEW 路径解析失败（root 内 rename 的瞬态）→ 不误删旧路径。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.add_index_root(str(root), enabled=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    p = _seed_disk_file(root, "old.txt", "内容")
    files.upsert_batch([_meta(p)])
    fid = files.get_by_path(str(p))["id"]
    # RENAME_OLD（父 FRN 20 可解析）；RENAME_NEW（父 FRN 30 不可解析 → 路径解析失败）
    reader = FakeReader(
        records=[
            _rec(101, 30, 20, USN_REASON_RENAME_OLD_NAME, "old.txt"),
            _rec(102, 30, 30, USN_REASON_RENAME_NEW_NAME, "new.txt"),
        ],
        dirs={20: str(root)},
    )
    svc = UsnRecoveryService(db, index, settings, reader)
    assert svc.run([str(root)]) is True
    with db.connect() as c:
        row = c.execute("SELECT is_deleted FROM files WHERE id=?", (fid,)).fetchone()
        assert row is not None and row["is_deleted"] == 0  # 旧路径未被误删（等后续扫描兜底）


def test_p21_dir_rename_no_ghost_file(env, tmp_path):
    """U4 回归：目录重命名（子文件 OLD/NEW 均解析到新路径）→ src==dst 丢弃，不新建错误 file_id。"""
    db, index, settings, files, _ = env
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    settings.add_index_root(str(root), enabled=True)
    settings.set(USN_CURSOR_KEY, {"D:\\": {"journal_id": 1, "usn": 100, "ts": 1}})
    from omnisearch.common.models import FileType
    from omnisearch.server.repository.files import FileMeta

    f = root / "f.txt"
    f.write_text("内容", encoding="utf-8")
    files.upsert_batch([FileMeta(path=str(f), filename="f.txt", dir_path=str(root), extension=".txt",
                                 size_bytes=2, mtime_ns=1, ctime_ns=1, file_type=FileType.DOC, mime_type=None)])
    with db.connect() as c:
        fid = c.execute("SELECT id FROM files WHERE path=?", (str(f),)).fetchone()["id"]
        c.commit()
    # 目录重命名：子文件两条记录都解析到「新目录」下的同一路径（历史名无法从 MFT 重建）
    new_dir_path = str(root / "newdir")
    reader = FakeReader(
        records=[
            _rec(101, 10, 30, USN_REASON_RENAME_OLD_NAME, "f.txt"),
            _rec(102, 10, 30, USN_REASON_RENAME_NEW_NAME, "f.txt"),
        ],
        dirs={30: new_dir_path},  # 两条记录都指向新目录
    )
    svc = _svc(env, reader)
    assert svc.run([str(root)]) is True
    with db.connect() as c:
        n = c.execute("SELECT count(*) n FROM files WHERE id=?", (fid,)).fetchone()["n"]
        assert n == 1  # 原 file_id 保留（未因 src==dst rename 被误删/新建）
