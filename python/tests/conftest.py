"""pytest 公共 fixture。"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from omnisearch.common.database import Database
from omnisearch.server.database.migrations.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[2]
QDRANT_BIN = REPO_ROOT / "qdrant" / "bin" / "qdrant.exe"


@pytest.fixture()
def db(tmp_path) -> Database:
    """迁移完成的临时数据库（WAL 主库）。"""
    db = Database(tmp_path / "test.db")
    migrate(db)
    return db


@pytest.fixture(scope="session")
def qdrant_server(tmp_path_factory) -> str:
    """真实 Qdrant Sidecar 进程（临时端口 6335/6336，session 级）；返回 REST url。

    若二进制缺失 → 跳过依赖 Qdrant 的测试（环境原因 SKIP）。
    """
    if not QDRANT_BIN.exists():
        pytest.skip(f"qdrant binary not found: {QDRANT_BIN}")
    http_port, grpc_port = 6335, 6336
    storage = tmp_path_factory.mktemp("qdrant")
    env = {
        **os.environ,
        "QDRANT__SERVICE__HTTP_PORT": str(http_port),
        "QDRANT__SERVICE__GRPC_PORT": str(grpc_port),
        "QDRANT__STORAGE__STORAGE_PATH": str(storage),
        "QDRANT__TELEMETRY_DISABLED": "true",
    }
    proc = subprocess.Popen([str(QDRANT_BIN)], env=env)
    try:
        import urllib.request

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.3)
        else:
            pytest.fail("qdrant not ready in time")
        yield f"http://127.0.0.1:{http_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def bge() -> "BGEEmbeddingProvider":
    """真实 BGE-small-zh ONNX（模型位于 dev-data/models，首次运行需先下载）。"""
    from omnisearch.common.embedding import BGEEmbeddingProvider
    from omnisearch.common.utils.models import models_dir

    provider = BGEEmbeddingProvider(models_dir(REPO_ROOT / "dev-data"))
    provider.dim  # 触发加载
    return provider
