"""Task Dashboard API（architecture.md §13：/task/status、/task/{id}/retry、/task/{id}/reindex）。

retry：复用 FAILED 任务置回 PENDING；attempt >= max_attempts → MAX_ATTEMPTS_EXCEEDED。
reindex：创建新任务（历史保留；活跃任务存在 → ALREADY_ACTIVE，partial unique index 兜底）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from omnisearch.server.api.schemas import FailedTaskItem, TaskRetryResponse, TaskStatsResponse
from omnisearch.server.repository.tasks import TaskRepository

router = APIRouter(prefix="/api/v1/task", tags=["tasks"])

_TASKS: TaskRepository | None = None


def configure_tasks(repo: TaskRepository) -> None:
    global _TASKS
    _TASKS = repo


@router.get("/status", response_model=TaskStatsResponse)
def status() -> TaskStatsResponse:
    assert _TASKS is not None
    return TaskStatsResponse(**_TASKS.stats())


@router.get("/failed", response_model=list[FailedTaskItem])
def failed() -> list[FailedTaskItem]:
    assert _TASKS is not None
    return [FailedTaskItem(**t) for t in _TASKS.failed_tasks()]


@router.post("/{task_id}/retry", response_model=TaskRetryResponse)
def retry(task_id: int) -> TaskRetryResponse:
    assert _TASKS is not None
    result = _TASKS.retry(task_id)
    if result in ("MAX_ATTEMPTS_EXCEEDED", "TASK_NOT_FOUND"):
        raise HTTPException(status_code=409, detail=result)
    return TaskRetryResponse(status=result)


@router.post("/{task_id}/reindex", response_model=TaskRetryResponse)
def reindex(task_id: int) -> TaskRetryResponse:
    assert _TASKS is not None
    _fid, result = _TASKS.reindex(task_id)
    if result == "TASK_NOT_FOUND":
        raise HTTPException(status_code=404, detail=result)
    return TaskRetryResponse(status=result)
