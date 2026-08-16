"""收尾 2：Watchdog 已缓冲事件在 remove/toggle 后的 discard 校验。

直接驱动 WatchService 缓冲（_queue_event/_queue_moved）+ _flush（绕过防抖 Timer），
确定性验证：事件真正执行前按 root 状态（仍存在且 active）二次校验。
"""
from __future__ import annotations

from pathlib import Path

from omnisearch.server.service.watch import WatchService


def _make_watch(tmp_path: Path):
    """构造 WatchService + 记录回调调用。"""
    calls: list[tuple[str, object]] = []

    def on_changes(paths):
        calls.append(("changes", paths))

    def on_deleted(paths):
        calls.append(("deleted", paths))

    def on_renamed(src, dest):
        calls.append(("renamed", (src, dest)))

    svc = WatchService(on_changes, on_deleted, on_renamed)
    return svc, calls


def _queue(svc: WatchService, root: Path, name: str, kind: str) -> None:
    """事件入缓冲（模拟 watchdog 线程在防抖窗口内收到事件）。"""
    if kind == "moved":
        svc._queue_moved(str(root / name), str(root / (name + ".renamed")))
    else:
        svc._queue_event(str(root / name), kind)


def _mkroot(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# ================= remove root 后已缓冲事件 discard =================

def test_remove_root_discards_buffered_create(tmp_path):
    root = _mkroot(tmp_path, "r1")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    _queue(svc, root, "a.txt", "created")  # 已入缓冲（remove 前）
    svc.remove_root(str(root))
    svc._flush()
    assert calls == [], f"remove 后已缓冲 CREATE 不应执行: {calls}"


def test_remove_root_discards_buffered_modify(tmp_path):
    root = _mkroot(tmp_path, "r2")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    _queue(svc, root, "b.txt", "modified")
    svc.remove_root(str(root))
    svc._flush()
    assert calls == [], f"remove 后已缓冲 MODIFY 不应执行: {calls}"


def test_remove_root_discards_buffered_delete_and_rename(tmp_path):
    root = _mkroot(tmp_path, "r3")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    _queue(svc, root, "c.txt", "deleted")
    _queue(svc, root, "d.txt", "moved")
    svc.remove_root(str(root))
    svc._flush()
    assert calls == [], f"remove 后已缓冲 DELETE/RENAME 不应执行: {calls}"


# ================= toggle disabled（= remove_root 语义） =================

def test_toggle_disabled_discards_buffered_event(tmp_path):
    root = _mkroot(tmp_path, "r4")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    _queue(svc, root, "e.txt", "created")
    svc.remove_root(str(root))  # toggle off 的 WatchService 语义 = remove_root
    svc._flush()
    assert calls == []


# ================= re-enable 后新事件恢复 =================

def test_re_enable_recovers_new_events(tmp_path):
    root = _mkroot(tmp_path, "r5")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    svc.remove_root(str(root))  # 禁用
    svc._flush()
    assert calls == []
    svc.add_roots([str(root)])  # 重新启用
    _queue(svc, root, "f.txt", "created")
    svc._flush()
    assert calls == [("changes", [str(root / "f.txt")])], f"re-enable 后新事件应恢复: {calls}"


# ================= sibling root 不受影响 =================

def test_sibling_root_still_processed(tmp_path):
    ra = _mkroot(tmp_path, "ra")
    rb = _mkroot(tmp_path, "rb")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(ra), str(rb)])
    _queue(svc, ra, "x.txt", "created")  # 两 root 的事件都已入缓冲
    _queue(svc, rb, "y.txt", "created")
    svc.remove_root(str(ra))  # 仅移除 ra
    svc._flush()
    assert calls == [("changes", [str(rb / "y.txt")])], f"sibling root 事件应正常执行: {calls}"


# ================= 正常路径不回归 =================

def test_active_root_events_still_processed(tmp_path):
    root = _mkroot(tmp_path, "r6")
    svc, calls = _make_watch(tmp_path)
    svc.add_roots([str(root)])
    _queue(svc, root, "g.txt", "created")
    svc._flush()
    assert calls == [("changes", [str(root / "g.txt")])]
