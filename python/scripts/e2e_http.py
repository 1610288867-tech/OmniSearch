"""E2E 共享 HTTP 客户端（审查去重：e2e_p21/p22/m5 与 benchmark 原各持一份拷贝）。

用法：from e2e_http import post, get, wait_idle, base_url
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

PORTS_FILE = ".omnisearch-ports.json"
DEFAULT_FASTAPI = "http://127.0.0.1:8734"


def read_ports(root_dir: str | Path = "dev-data") -> dict:
    """读取 dev.py 落盘的端口文件（不存在/损坏 → {}）。"""
    ports = Path(root_dir).resolve() / PORTS_FILE
    if ports.exists():
        try:
            data = json.loads(ports.read_text(encoding="utf-8"))
            if data.get("fastapi"):
                return data
        except (ValueError, OSError):
            pass
    return {}


def base_url(root_dir: str | Path = "dev-data") -> str:
    """T5：读取 dev.py 落盘的真实端口（8734 被占用时会顺延），避免 e2e 硬编码。

    优先 `--root <dir>/.omnisearch-ports.json`；文件缺失（手动 dev.py 时 8734 空闲）→ 默认 8734。
    """
    data = read_ports(root_dir)
    return f"http://127.0.0.1:{data['fastapi']}" if data.get("fastapi") else DEFAULT_FASTAPI


def post(url: str, token: str, path: str, body: dict, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Omni-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url: str, token: str, path: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(url + path, headers={"X-Omni-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_idle(url: str, token: str, timeout_s: float = 600.0) -> dict:
    """等待任务队列与扫描均空闲，返回最终 /task/status。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = get(url, token, "/api/v1/task/status")
        idx = get(url, token, "/api/v1/index/status")
        if st["queue_length"] == 0 and st["processing"] == 0 and not idx["running"]:
            return st
        time.sleep(1)
    raise TimeoutError("tasks/index not idle")
