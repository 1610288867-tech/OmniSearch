"""统一配置与路径（architecture.md §2.3 / §4.3 / §10.2）。

- 数据目录：开发期经 --dev-data 注入 OMNISEARCH_DEV_DATA；生产为 %LOCALAPPDATA%/OmniSearch
- 端口：Qdrant HTTP/gRPC 必须成对顺延（见 utils.ports）
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "OmniSearch"

# FastAPI 端口（architecture.md §4.3）
FASTAPI_PORT = 8734

# Qdrant Sidecar 端口（HTTP + gRPC 成对，6333/6334 → 6335/6336 → …）
QDRANT_HTTP_PORT = 6333
QDRANT_GRPC_PORT = 6334

# Worker 轮询间隔（可调参数，非架构约束；architecture.md §10.2）
POLL_INTERVAL_MS = 500

# Qdrant 就绪探针超时（秒）
QDRANT_READY_TIMEOUT_S = 30.0


def dev_data_dir() -> Path:
    """数据目录：OMNISEARCH_DEV_DATA（开发）→ %LOCALAPPDATA%/OmniSearch（生产）。"""
    override = os.environ.get("OMNISEARCH_DEV_DATA")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or dev_data_dir()) / "db" / "omnisearch.db"


def log_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or dev_data_dir()) / "logs"


def qdrant_http_port() -> int:
    """Qdrant HTTP 端口（Main / dev.py 探测后注入）。"""
    return int(os.environ.get("OMNISEARCH_QDRANT_HTTP_PORT", QDRANT_HTTP_PORT))


def qdrant_url() -> str:
    """Qdrant REST 地址（Worker 写入 + Server 查询共用）。"""
    return f"http://127.0.0.1:{qdrant_http_port()}"
