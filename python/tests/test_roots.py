"""扫描位置管理测试（产品增强）：多 Root 添加/删除/冲突/持久化/顺序扫描/Watchdog。

覆盖：添加文件夹/磁盘、删除、多 Root、重复、父子冲突（双向）、持久化、重启恢复、
多 Root 顺序扫描、多 Root Watchdog、invalid Root 不阻塞、旧格式兼容。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnisearch.common.config import db_path
from omnisearch.common.database import Database
from omnisearch.common.utils.paths import canonical_root, root_covers, root_key
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.main import create_app
from omnisearch.server.repository.settings import SettingsRepository


@pytest.fixture()
def client(tmp_path):
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    with TestClient(create_app()) as c:
        yield c, SettingsRepository(Database(db_path(tmp_path)))
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


def _mkroot(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """在 tmp 下建 root 目录 + 测试文件。"""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (root / fname).write_text(content, encoding="utf-8")
    return root


# ================= 添加 / 删除 / 多 Root =================

def test_add_folder_and_list(client, tmp_path):
    c, repo = client
    root = _mkroot(tmp_path, "photos", {"a.txt": "机器学习"})
    resp = c.post("/api/v1/index/roots/add", json={"path": str(root) + "/"})  # trailing slash
    assert resp.status_code == 200
    item = resp.json()
    assert item["path"] == canonical_root(str(root))  # 规范化
    assert item["enabled"] is True and item["created_at"] > 0
    listing = c.get("/api/v1/index/roots").json()
    assert [r["path"] for r in listing["roots"]] == [canonical_root(str(root))]
    assert listing["roots"][0]["file_count"] == 1  # 已索引 1 个文件


def test_add_drive_root(client):
    c, _repo = client
    # 用 tmp 卷的根路径模拟盘符根（Windows 盘符根：保留尾部斜杠）
    resp = c.post("/api/v1/index/roots/add", json={"path": "C:\\"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "C:\\"  # 盘符根保留尾部斜杠


def test_add_duplicate_rejected(client, tmp_path):
    c, _repo = client
    root = _mkroot(tmp_path, "dup", {"a.txt": "x"})
    assert c.post("/api/v1/index/roots/add", json={"path": str(root)}).status_code == 200
    # 大小写/斜杠差异也算重复（Windows 大小写不敏感）
    dup = str(root).replace("\\", "/").upper()
    resp = c.post("/api/v1/index/roots/add", json={"path": dup})
    assert resp.status_code == 400
    assert "ROOT_ALREADY_EXISTS" in resp.text


def test_parent_child_conflict_both_directions(client, tmp_path):
    c, _repo = client
    parent = _mkroot(tmp_path, "P", {})
    child = _mkroot(tmp_path, "P", {"sub": {}}) if False else tmp_path / "P" / "sub"
    child.mkdir(exist_ok=True)
    # 先加父 → 子被拒
    assert c.post("/api/v1/index/roots/add", json={"path": str(parent)}).status_code == 200
    resp = c.post("/api/v1/index/roots/add", json={"path": str(child)})
    assert resp.status_code == 400 and "ROOT_ALREADY_COVERED" in resp.text
    # 反向：先加子 → 父被拒
    c2 = None
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path / "db2")
    try:
        with TestClient(create_app()) as cc:
            assert cc.post("/api/v1/index/roots/add", json={"path": str(child)}).status_code == 200
            resp2 = cc.post("/api/v1/index/roots/add", json={"path": str(parent)})
            assert resp2.status_code == 400 and "ROOT_ALREADY_COVERED" in resp2.text
    finally:
        os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_invalid_root_rejected_service_alive(client):
    """无效 Root → 400 INVALID_ROOT，服务不崩溃（后续请求正常）。"""
    c, _repo = client
    resp = c.post("/api/v1/index/roots/add", json={"path": "Z:\\definitely-not-exist-xyz"})
    assert resp.status_code == 400 and "INVALID_ROOT" in resp.text
    assert c.get("/api/v1/index/roots").status_code == 200  # 服务仍正常


def test_remove_root_keeps_data(client, tmp_path):
    """移除 Root：settings 移除 + 已索引数据保留（不 DELETE 记录）。"""
    c, repo = client
    root = _mkroot(tmp_path, "keep", {"doc.txt": "机器学习正文内容"})
    assert c.post("/api/v1/index/roots/add", json={"path": str(root)}).status_code == 200
    resp = c.post("/api/v1/index/roots/remove", json={"path": str(root)})
    assert resp.status_code == 200
    assert resp.json()["roots"] == []  # settings 已移除
    # 数据保留（files 记录仍在，可搜索）
    db = Database(db_path(Path(os.environ["OMNISEARCH_DEV_DATA"])))
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) n FROM files WHERE is_deleted=0").fetchone()["n"]
        assert n == 1


def test_multi_roots_sequential_scan(client, tmp_path):
    """多 Root 顺序扫描：两个目录各自 scan job，文件均可搜索。

    注：TestClient 环境无 AI Worker（正文 chunks 不生成），用文件名命中验证（M1 语义）。
    """
    c, _repo = client
    ra = _mkroot(tmp_path, "root-a", {"AlphaContent2026.txt": "x"})
    rb = _mkroot(tmp_path, "root-b", {"BetaContent2026.txt": "x"})
    assert c.post("/api/v1/index/roots/add", json={"path": str(ra)}).status_code == 200
    assert c.post("/api/v1/index/roots/add", json={"path": str(rb)}).status_code == 200
    st = c.get("/api/v1/index/status").json()
    assert len(st["jobs"]) == 2  # 每个 Root 一个 job（顺序扫描）
    assert st["jobs"][0]["status"] == "DONE" and st["jobs"][1]["status"] == "DONE"
    for q, expect in (("AlphaContent", 1), ("BetaContent", 1)):
        body = c.post("/api/v1/search", json={"query": q, "topK": 10}).json()
        assert body["total"] == expect, f"{q}: {body['total']}"


def test_roots_persist_after_restart(tmp_path):
    """Root 持久化：settings KV 重启后仍在（同一 data dir 两次 lifespan，非嵌套 TestClient）。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    root = _mkroot(tmp_path, "persist", {"p.txt": "x"})
    try:
        with TestClient(create_app()) as c:
            assert c.post("/api/v1/index/roots/add", json={"path": str(root)}).status_code == 200
        # 重启（同 data dir 新 TestClient）
        with TestClient(create_app()) as c2:
            listing = c2.get("/api/v1/index/roots").json()
            assert [r["path"] for r in listing["roots"]] == [canonical_root(str(root))]
            assert listing["roots"][0]["enabled"] is True
    finally:
        os.environ.pop("OMNISEARCH_DEV_DATA", None)


def test_legacy_string_roots_auto_upgraded(tmp_path):
    """旧格式 list[str] 自动升级为 [{path, enabled, created_at}] 并持久化。"""
    os.environ["OMNISEARCH_DEV_DATA"] = str(tmp_path)
    repo = SettingsRepository(Database(db_path(tmp_path)))
    migrate(Database(db_path(tmp_path)))
    repo.set("index_roots", ["D:\\old-root"])
    roots = repo.get_index_roots()
    assert roots == [{"path": "D:\\old-root", "enabled": True, "created_at": roots[0]["created_at"]}]
    assert isinstance(repo.get("index_roots")[0], dict)  # 已持久化为新结构
    os.environ.pop("OMNISEARCH_DEV_DATA", None)


# ================= 多 Root Watchdog =================

def test_multi_root_watchdog_remove_stops_listening(client, tmp_path):
    """多 Root Watchdog：删除 root-a 后不再监听（写入不触发索引），root-b 仍监听。"""
    c, _repo = client
    ra = _mkroot(tmp_path, "watch-a", {"wa.txt": "WatchAlpha"})
    rb = _mkroot(tmp_path, "watch-b", {"wb.txt": "WatchBeta"})
    assert c.post("/api/v1/index/roots/add", json={"path": str(ra)}).status_code == 200
    assert c.post("/api/v1/index/roots/add", json={"path": str(rb)}).status_code == 200
    assert c.post("/api/v1/index/roots/remove", json={"path": str(ra)}).status_code == 200

    # root-a 已移除：新文件写入 → 防抖 2s + 处理 → 索引不应新增
    (ra / "new-a.txt").write_text("NewAlpha2026", encoding="utf-8")
    # root-b 仍监听：新文件写入 → 索引应新增
    (rb / "new-b.txt").write_text("NewBeta2026", encoding="utf-8")
    time.sleep(3.5)  # watchdog 防抖 2s + 处理余量

    db = Database(db_path(Path(os.environ["OMNISEARCH_DEV_DATA"])))
    with db.connect() as conn:
        rows = {
            r["filename"]: 1
            for r in conn.execute("SELECT filename FROM files WHERE is_deleted=0").fetchall()
        }
    assert "new-b.txt" in rows, "root-b 仍监听的 root 应索引新文件"
    assert "new-a.txt" not in rows, "已移除的 root-a 不应再索引新文件"


# ================= canonical_root / root_covers 单元 =================

def test_canonical_root_normalization():
    assert canonical_root("d:/photos/") == "d:\\photos"
    assert canonical_root("D:\\photos\\\\") == "D:\\photos"
    assert canonical_root("C:") == "C:\\"
    assert canonical_root("C:\\") == "C:\\"  # 盘符根保留
    assert root_key("D:\\Photos") == root_key("d:/photos/")
    assert root_covers("D:\\Photos\\2026", "D:\\Photos")
    assert root_covers("D:\\Photos", "D:\\")
    assert not root_covers("D:\\Photos", "D:\\PhotoX")
    assert root_covers("D:\\", "D:\\")  # 自身
