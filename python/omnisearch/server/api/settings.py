"""Settings API（architecture.md §13：GET/PUT /api/v1/settings）。"""
from __future__ import annotations

from fastapi import APIRouter

from omnisearch.server.api.schemas import SettingsResponse, SettingsUpdate
from omnisearch.server.service.settings import SettingsService

router = APIRouter(prefix="/api/v1", tags=["settings"])

_SETTINGS: SettingsService | None = None


def configure_settings(service: SettingsService) -> None:
    global _SETTINGS
    _SETTINGS = service


@router.get("/settings", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
    assert _SETTINGS is not None
    return SettingsResponse(**_SETTINGS.get())


@router.put("/settings", response_model=SettingsResponse)
def put_settings(patch: SettingsUpdate) -> SettingsResponse:
    assert _SETTINGS is not None
    return SettingsResponse(**_SETTINGS.update(patch.model_dump(exclude_none=True)))
