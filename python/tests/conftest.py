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

# T7：测试用「手动 os.environ[set] → 断言失败时 pop 不执行」会泄漏幽灵路径到后续用例。
# 这里做全局兜底清理（autouse teardown）；正常路径下各测试自管 set/pop 不变。
_ENV_KEYS = ("OMNISEARCH_DEV_DATA", "OMNISEARCH_TOKEN", "OMNISEARCH_QDRANT_HTTP_PORT", "OMNISEARCH_QDRANT_GRPC_PORT")


@pytest.fixture(autouse=True)
def _clean_omnisearch_env():
    yield
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


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
    """真实 BGE-small-zh ONNX（模型位于 dev-data/models）。

    T3 修正：模型缺失 → skip（与 qdrant_server 的 skip 策略一致），
    而非抛 error 让整批用例红——新检出可先跑 download_models.py。
    """
    from omnisearch.common.embedding import BGEEmbeddingProvider
    from omnisearch.common.utils.models import models_dir

    model_dir = models_dir(REPO_ROOT / "dev-data")
    if not (model_dir / "model.onnx").exists():
        pytest.skip("BGE model not found — run python python/scripts/download_models.py")
    provider = BGEEmbeddingProvider(model_dir)
    provider.dim  # 触发加载
    return provider
