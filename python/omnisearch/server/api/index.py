"""Index API（architecture.md §13：/index/scan、/index/status）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from omnisearch.server.api.schemas import IndexStatusResponse, ScanRequest, ScanResponse
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.settings import SettingsRepository
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.watch import WatchService

router = APIRouter(prefix="/api/v1/index", tags=["index"])

_INDEX: IndexService | None = None
_JOBS: IndexJobRepository | None = None
_SETTINGS: SettingsRepository | None = None
_WATCH: WatchService | None = None


def configure_index(
    index: IndexService,
    jobs: IndexJobRepository,
    settings: SettingsRepository,
    watch: WatchService,
) -> None:
    global _INDEX, _JOBS, _SETTINGS, _WATCH
    _INDEX, _JOBS, _SETTINGS, _WATCH = index, jobs, settings, watch


@router.post("/scan", response_model=ScanResponse)
def scan(req: ScanRequest, background: BackgroundTasks) -> ScanResponse:
    """创建扫描作业并后台执行；立即返回 job_id（UI 轮询 /index/status 看进度）。"""
    assert _INDEX is not None and _SETTINGS is not None and _WATCH is not None
    root = req.root_paths[0]  # M1：单 root 顺序扫描（多 root 由 UI 逐次调用）
    job_id = _INDEX.start_scan(root, req.scan_type)
    _SETTINGS.add_index_root(root)
    _WATCH.add_roots([root])  # 实时监听（首次添加时启动；已启动则追加 schedule）
    background.add_task(_INDEX.run_scan, job_id, root)
    return ScanResponse(job_id=job_id, root_path=root, status="RUNNING")


@router.get("/status", response_model=IndexStatusResponse)
def status() -> IndexStatusResponse:
    assert _JOBS is not None
    job = _JOBS.latest()
    return IndexStatusResponse(running=bool(job and job["status"] == "RUNNING"), jobs=[job] if job else [])
