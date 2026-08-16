"""端口分配工具（architecture.md §4.3）。

Qdrant 的 HTTP/gRPC 必须作为端口组成对分配：6333/6334 → 6335/6336 → …
禁止只顺延 HTTP 而 gRPC 固定。实际端口由 Electron Main / dev.py 探测后注入 FastAPI 配置。
"""
from __future__ import annotations

import socket


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_free_port(base: int) -> int:
    """从 base 起探测第一个空闲端口。"""
    port = base
    while _port_in_use(port) and port < base + 100:
        port += 1
    if _port_in_use(port):
        raise RuntimeError(f"no free port near {base}")
    return port


def find_free_port_pair(base_http: int, base_grpc: int) -> tuple[int, int]:
    """成对分配 Qdrant HTTP/gRPC 端口（成对顺延，绝不拆对）。"""
    http, grpc = base_http, base_grpc
    while (_port_in_use(http) or _port_in_use(grpc)) and http < base_http + 100:
        http += 2
        grpc += 2
    if _port_in_use(http) or _port_in_use(grpc):
        raise RuntimeError(f"no free port pair near {base_http}/{base_grpc}")
    return http, grpc
