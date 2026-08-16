"""Index API（architecture.md §13：/index/scan、/index/status）+ 扫描位置管理（Roots）。

多 Root 规则（扫描位置管理）：
- canonicalize：大小写 / slash / trailing slash 统一（common/utils/paths.py）
- 重复 Root → 400 ROOT_ALREADY_EXISTS；父子 Root（双向）→ 400 ROOT_ALREADY_COVERED
- 多 Root 顺序扫描：每个 Root 一个 index_jobs，后台串行执行（无并行、无 DAG）
- 移除 Root：settings 移除 + 停止监听 + 不再扫描；已索引数据保留（默认语义，不 DELETE）
"""
from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException

from omnisearch.common.database import Database
from omnisearch.common.utils.paths import canonical_root, root_covers, root_key
from omnisearch.server.api.schemas import (
    IndexStatusResponse,
    RootInfo,
    RootPathRequest,
    RootsResponse,
    RootToggleRequest,
    ScanRequest,
    ScanResponse,
)
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.settings import SettingsRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.watch import WatchService

router = APIRouter(prefix="/api/v1/index", tags=["index"])

_INDEX: IndexService | None = None
_JOBS: IndexJobRepository | None = None
_SETTINGS: SettingsRepository | None = None
_WATCH: WatchService | None = None
_DB: Database | None = None


def configure_index(
    index: IndexService,
    jobs: IndexJobRepository,
    settings: SettingsRepository,
    watch: WatchService,
    db: Database,
) -> None:
    global _INDEX, _JOBS, _SETTINGS, _WATCH, _DB
    _INDEX, _JOBS, _SETTINGS, _WATCH, _DB = index, jobs, settings, watch, db


# ================= 扫描 =================

@router.post("/scan", response_model=ScanResponse)
def scan(req: ScanRequest, background: BackgroundTasks) -> ScanResponse:
    """创建扫描作业并后台执行；立即返回 job_id（UI 轮询 /index/status 看进度）。"""
    assert _INDEX is not None and _SETTINGS is not None and _WATCH is not None
    root = req.root_paths[0]  # M1：单 root 顺序扫描（多 root 由 Root 管理逐个添加）
    job_id = _INDEX.start_scan(root, req.scan_type)
    _SETTINGS.add_index_root(root)
    _WATCH.add_roots([root])  # 实时监听（首次添加时启动；已启动则追加 schedule）
    background.add_task(_INDEX.run_scan, job_id, root)
    return ScanResponse(job_id=job_id, root_path=root, status="RUNNING")


@router.get("/status", response_model=IndexStatusResponse)
def status() -> IndexStatusResponse:
    assert _JOBS is not None
    jobs = _JOBS.recent(20)
    return IndexStatusResponse(running=any(j["status"] == "RUNNING" for j in jobs), jobs=jobs)


# ================= 扫描位置管理 =================

def _file_count(root: str) -> int:
    """该 root 下已索引且未删除的文件数（统计展示，非过滤语义）。"""
    assert _DB is not None
    prefix = canonical_root(root).rstrip("\\") + "\\"
    with _DB.connect() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM files WHERE is_deleted = 0 AND (path = ? OR path LIKE ?)",
            (canonical_root(root), prefix + "%"),
        ).fetchone()
        return row["n"]


@router.get("/roots", response_model=RootsResponse)
def roots() -> RootsResponse:
    assert _SETTINGS is not None
    items = [_RootInfo(r) for r in _SETTINGS.get_index_roots()]
    return RootsResponse(roots=items)


def _RootInfo(r: dict) -> RootInfo:
    return RootInfo(path=r["path"], enabled=bool(r.get("enabled", True)),
                    created_at=int(r.get("created_at", 0)), file_count=_file_count(r["path"]))


@router.post("/roots/add", response_model=RootInfo)
def add_root(req: RootPathRequest, background: BackgroundTasks) -> RootInfo:
    """添加扫描位置：canonicalize → 重复/父子校验 → 持久化 → 监听 → 后台 full scan。"""
    assert _INDEX is not None and _SETTINGS is not None and _WATCH is not None
    path = canonical_root(req.path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="INVALID_ROOT: 目录不存在或不可访问")
    for r in _SETTINGS.get_index_roots():
        if root_key(r["path"]) == root_key(path):
            raise HTTPException(status_code=400, detail="ROOT_ALREADY_EXISTS")
        if root_covers(path, r["path"]) or root_covers(r["path"], path):
            raise HTTPException(status_code=400, detail="ROOT_ALREADY_COVERED")
    _SETTINGS.add_index_root(path)
    _WATCH.add_roots([path])  # 立即开始监听（不阻塞 UI）
    job_id = _INDEX.start_scan(path, "full")
    background.add_task(_INDEX.run_scan, job_id, path)  # 后台顺序扫描
    return _RootInfo({"path": path, "enabled": True, "created_at": _now()})


def _now() -> int:
    import time

    return int(time.time())


@router.post("/roots/remove", response_model=RootsResponse)
def remove_root(req: RootPathRequest) -> RootsResponse:
    """移除扫描位置：停止监听 + 不再扫描；已索引数据保留（默认语义，不 DELETE 记录）。"""
    assert _SETTINGS is not None and _WATCH is not None
    path = canonical_root(req.path)
    _SETTINGS.remove_index_root(path)
    _WATCH.remove_root(path)
    return RootsResponse(roots=[_RootInfo(r) for r in _SETTINGS.get_index_roots()])


@router.post("/roots/toggle", response_model=RootInfo)
def toggle_root(req: RootToggleRequest) -> RootInfo:
    """启用/禁用扫描位置：enabled=false → 停止监听（数据保留）；true → 恢复监听（不自动重扫）。"""
    assert _SETTINGS is not None and _WATCH is not None
    path = canonical_root(req.path)
    item = next((r for r in _SETTINGS.get_index_roots() if root_key(r["path"]) == root_key(path)), None)
    if item is None:
        raise HTTPException(status_code=404, detail="ROOT_NOT_FOUND")
    _SETTINGS.update_index_root(path, enabled=req.enabled)
    if req.enabled:
        _WATCH.add_roots([path])
    else:
        _WATCH.remove_root(path)
    return _RootInfo({**item, "enabled": req.enabled})
