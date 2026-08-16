"""E2E 共享 HTTP 客户端（审查去重：e2e_p21/p22/m5 与 benchmark 原各持一份拷贝）。

用法：from e2e_http import post, get, wait_idle
"""
from __future__ import annotations

import json
import time
import urllib.request


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
