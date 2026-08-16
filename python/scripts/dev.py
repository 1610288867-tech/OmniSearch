"""一键拉起开发环境（architecture.md §2.3 / §4.3）。

启动 FastAPI + AI Worker + Qdrant（Sidecar）三子进程；Electron 由 desktop/ 的 ProcessManager 负责。
- 端口：FastAPI 8734（顺延）；Qdrant HTTP/gRPC 成对顺延（6333/6334 → 6335/6336 → …）
- Qdrant 二进制：OMNISEARCH_QDRANT_BIN 或 qdrant/bin/qdrant.exe；缺失时跳过并告警
- token：自动生成并注入 X-Omni-Token 鉴权
- Ctrl+C 优雅退出：kill 子进程 + WAL checkpoint

用法：python python/scripts/dev.py [--dev-data dev-data]
"""
from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(__file__).resolve().parents[1]  # python/

sys.path.insert(0, str(PYTHON))

from omnisearch.common.config import (  # noqa: E402
    FASTAPI_PORT,
    POLL_INTERVAL_MS,
    QDRANT_GRPC_PORT,
    QDRANT_HTTP_PORT,
    QDRANT_READY_TIMEOUT_S,
    dev_data_dir,
)
from omnisearch.common.utils.ports import find_free_port, find_free_port_pair  # noqa: E402


def _python_bin() -> str:
    """Python 解释器发现（M0 Review 修正）：

    1. 优先自动使用 python/.venv/Scripts/python.exe（仓库内 venv）
    2. 不存在则读取 OMNISEARCH_PYTHON 环境变量
    3. 两者都不存在 → 明确报错退出
    """
    venv_py = PYTHON / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    env_py = os.environ.get("OMNISEARCH_PYTHON")
    if env_py:
        return env_py
    raise SystemExit(
        "未找到 Python 解释器：请创建 python/.venv（python -m venv .venv 并安装依赖），"
        "或设置 OMNISEARCH_PYTHON 环境变量"
    )


def _spawn(cmd: list[str], env: dict[str, str], label: str) -> subprocess.Popen:
    print(f"[dev] starting {label}: {' '.join(cmd)}", flush=True)
    return subprocess.Popen(cmd, env=env, cwd=str(PYTHON))


def _wait_http(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniSearch dev launcher")
    parser.add_argument("--dev-data", type=str, default=None)
    args = parser.parse_args()

    # 解析为绝对路径：子进程 cwd 不同（python/），相对路径会落错位置
    data_dir = Path(args.dev_data).resolve() if args.dev_data else dev_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OMNISEARCH_DEV_DATA"] = str(data_dir)

    token = secrets.token_hex(16)
    print(f"[dev] token={token}", flush=True)

    # ---- 端口（architecture.md §4.3：Qdrant 成对顺延）----
    fastapi_port = find_free_port(FASTAPI_PORT)
    qdrant_http, qdrant_grpc = find_free_port_pair(QDRANT_HTTP_PORT, QDRANT_GRPC_PORT)
    print(f"[dev] ports: fastapi={fastapi_port} qdrant_http={qdrant_http} qdrant_grpc={qdrant_grpc}", flush=True)
    # T5：实际端口落盘供 e2e 脚本读取（8734/6333 被占用时顺延，e2e 不得硬编码）。
    # 写入早于进程启动，e2e 轮询该文件即可拿到真实地址。
    import json as _json

    (data_dir / ".omnisearch-ports.json").write_text(
        _json.dumps({"fastapi": fastapi_port, "qdrant_http": qdrant_http, "qdrant_grpc": qdrant_grpc}),
        encoding="utf-8",
    )

    procs: list[subprocess.Popen] = []

    # ---- Qdrant Sidecar（缺失二进制则跳过，/health 显示 qdrant=false）----
    qdrant_bin = os.environ.get("OMNISEARCH_QDRANT_BIN") or str(ROOT / "qdrant" / "bin" / "qdrant.exe")
    if Path(qdrant_bin).exists():
        qdrant_env = os.environ.copy()
        qdrant_env["QDRANT__SERVICE__HTTP_PORT"] = str(qdrant_http)
        qdrant_env["QDRANT__SERVICE__GRPC_PORT"] = str(qdrant_grpc)
        qdrant_env["QDRANT__STORAGE__STORAGE_PATH"] = str(data_dir / "qdrant")
        qdrant_env["QDRANT__TELEMETRY_DISABLED"] = "true"
        procs.append(_spawn([qdrant_bin], qdrant_env, "qdrant"))
        if _wait_http(f"http://127.0.0.1:{qdrant_http}/healthz", QDRANT_READY_TIMEOUT_S):
            print(f"[dev] qdrant ready at http://127.0.0.1:{qdrant_http}", flush=True)
        else:
            print("[dev] WARNING: qdrant not ready in time", flush=True)
    else:
        print(
            f"[dev] WARNING: qdrant binary not found at {qdrant_bin}; "
            "set OMNISEARCH_QDRANT_BIN or place qdrant/bin/qdrant.exe (Sidecar 骨架已就绪)"
        )

    base_env = os.environ.copy()
    base_env.update(
        {
            "OMNISEARCH_DEV_DATA": str(data_dir),
            "OMNISEARCH_TOKEN": token,
            "OMNISEARCH_FASTAPI_PORT": str(fastapi_port),
            "OMNISEARCH_QDRANT_HTTP_PORT": str(qdrant_http),
            "OMNISEARCH_QDRANT_GRPC_PORT": str(qdrant_grpc),
            "OMNISEARCH_POLL_INTERVAL_MS": str(POLL_INTERVAL_MS),
        }
    )

    # ---- FastAPI（先启动并等待就绪，再启动 Worker——架构 §2.3 启动顺序）----
    py = _python_bin()
    procs.append(
        _spawn(
            [py, "-m", "uvicorn", "omnisearch.server.main:app",
             "--host", "127.0.0.1", "--port", str(fastapi_port), "--log-level", "info"],
            base_env,
            "fastapi",
        )
    )
    if _wait_http(f"http://127.0.0.1:{fastapi_port}/health", 30.0):
        print(f"[dev] FastAPI ready: http://127.0.0.1:{fastapi_port}/health", flush=True)
    else:
        print("[dev] WARNING: FastAPI not ready in time", flush=True)

    # ---- AI Worker（server 就绪后启动：schema 已迁移，避免 claim 竞态）----
    procs.append(_spawn([py, "-m", "omnisearch.worker"], base_env, "worker"))

    print("[dev] all processes started. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[dev] shutting down...", flush=True)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[dev] done", flush=True)


if __name__ == "__main__":
    main()
