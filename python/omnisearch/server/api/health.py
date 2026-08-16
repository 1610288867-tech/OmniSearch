"""GET /health —— 存活+就绪探针（architecture.md §13；Electron Main 每 5s 探测）。

M5 收口 4：明确 readiness 区分 —— sqlite / qdrant / worker / semantic。
- semantic_ready=false 不代表 FastAPI 崩溃（status 仍由 sqlite/qdrant/worker 决定）
- BGE/Qdrant 语义初始化失败 → 仅 semantic=false，搜索按降级矩阵工作（§12.8）
"""
from __future__ import annotations

import time
import urllib.request

from fastapi import APIRouter

from omnisearch.common.config import qdrant_http_port
from omnisearch.common.contracts import ComponentHealth, HealthResponse
from omnisearch.common.database import Database

router = APIRouter(tags=["health"])

VERSION = "0.1.0"
_DB: Database | None = None
_SEMANTIC_READY = False  # lifespan 注入：BGE + Qdrant 语义通道就绪状态
WORKER_DEAD_S = 15.0     # worker 心跳超过该时长未更新 → worker_ready=false


def configure_health(db: Database, version: str = VERSION) -> None:
    """main.py 在应用启动（lifespan）时调用，注入依赖。"""
    global _DB, VERSION
    _DB = db
    VERSION = version


def configure_semantic_ready(ready: bool) -> None:
    """lifespan 在语义通道初始化后调用（成功/降级都要设置，M5 收口 4）。"""
    global _SEMANTIC_READY
    _SEMANTIC_READY = ready


def _probe_qdrant(port: int, timeout: float = 0.5) -> ComponentHealth:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=timeout
        ) as resp:
            ok = resp.status == 200
            return ComponentHealth(ok=ok, detail=None if ok else f"status={resp.status}")
    except Exception as exc:  # noqa: BLE001 —— 探测失败即视为不可用
        return ComponentHealth(ok=False, detail=f"unreachable: {exc}")


def _probe_worker() -> ComponentHealth:
    """worker_ready：SQLite 心跳表最近 15s 内有 Worker 心跳。"""
    assert _DB is not None
    try:
        with _DB.connect() as c:
            row = c.execute(
                "SELECT last_seen FROM worker_heartbeat WHERE worker_id='worker'"
            ).fetchone()
        if row is not None and row["last_seen"] >= int(time.time()) - WORKER_DEAD_S:
            return ComponentHealth(ok=True)
        return ComponentHealth(ok=False, detail="no recent worker heartbeat")
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(ok=False, detail=str(exc))


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    assert _DB is not None, "configure_health() 未在应用启动时调用"
    sqlite_ok = True
    sqlite_detail: str | None = None
    try:
        with _DB.connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        sqlite_ok = False
        sqlite_detail = str(exc)

    components = {
        "sqlite": ComponentHealth(ok=sqlite_ok, detail=sqlite_detail),
        "qdrant": _probe_qdrant(qdrant_http_port()),
        "worker": _probe_worker(),
        "semantic": ComponentHealth(
            ok=_SEMANTIC_READY,
            detail=None if _SEMANTIC_READY else "BGE/Qdrant 未就绪（关键词搜索可用，语义自动降级）",
        ),
    }
    # 语义模型不可用 ≠ 服务崩溃：status 只看 sqlite/qdrant/worker（§12 降级矩阵）
    return HealthResponse(
        status="ok" if all(components[k].ok for k in ("sqlite", "qdrant", "worker")) else "degraded",
        version=VERSION,
        components=components,
    )
