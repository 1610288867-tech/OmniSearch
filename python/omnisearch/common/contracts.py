"""共享 DTO（Pydantic）—— 与 desktop/src/shared/contracts.ts 对齐（架构 §4.2）。

M0 仅健康检查相关；Search DTO 在 M5 补齐。
"""
from __future__ import annotations

from pydantic import BaseModel


class ComponentHealth(BaseModel):
    """单个组件健康状态。"""

    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    """GET /health 响应（architecture.md §13）。"""

    status: str  # "ok" | "degraded"
    version: str
    components: dict[str, ComponentHealth]
