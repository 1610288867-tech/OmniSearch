"""Hybrid Search Benchmark（M5 §20：architecture.md §16 Benchmark Framework 首个报告）。

记录 p50/p95：parser / FTS / vector / hybrid / embedding / total latency（ms）。
不设硬性性能承诺（§14 性能纪律）——只记录，供 P2 调优基线。

用法（FastAPI 需已运行，token 可从 dev.py 输出获取）：
  python python/scripts/benchmark.py [--url http://127.0.0.1:8734] [--token XXX] [--n 20] [--queries 'q1|q2|q3']
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from collections import defaultdict

DEFAULT_QUERIES = [
    "机器学习",
    "昨天的自由女神照片",
    "纽约城市风景",
    "深度学习与神经网络",
    "关于神经网络的文档",
]


def _post(url: str, token: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Omni-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8734")
    ap.add_argument("--token", default="")
    ap.add_argument("--n", type=int, default=20, help="每查询重复次数")
    ap.add_argument("--queries", default="|".join(DEFAULT_QUERIES))
    args = ap.parse_args()

    queries = [q for q in args.queries.split("|") if q]
    lat: dict[str, list[float]] = defaultdict(list)
    hits: dict[str, list[int]] = defaultdict(list)

    stage_keys = ("parser", "fts", "semantic", "finalize", "total")
    stage_lat: dict[str, list[float]] = defaultdict(list)

    # warmup（模型已预热则无成本；BGE 会话常驻）
    _post(args.url, args.token, "/api/v1/search", {"query": queries[0], "topK": 10})

    for q in queries:
        for _ in range(args.n):
            started = time.perf_counter()
            body = _post(args.url, args.token, "/api/v1/search", {"query": q, "topK": 10, "stages": True})
            lat["total"].append((time.perf_counter() - started) * 1000)
            lat["api_reported"].append(body["latency_ms"])
            hits["count"].append(body["total"])
            if body.get("stages"):
                for k in stage_keys:
                    stage_lat[k].append(body["stages"].get(k, 0.0))
        time.sleep(0.2)

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    print("=== Hybrid Search Benchmark（本地，Windows；仅记录，无硬性承诺） ===")
    print(f"queries={queries} n={args.n}")
    print(f"{'stage':12s} {'p50':>8s} {'p95':>8s} {'min':>7s} {'max':>7s}")
    for key in ("total", "api_reported"):
        xs = lat[key]
        print(f"{key:12s} {pct(xs, 0.5):8.1f} {pct(xs, 0.95):8.1f} {min(xs):7.1f} {max(xs):7.1f}")
    for key in stage_keys:
        xs = stage_lat[key]
        if xs:
            print(f"{key:12s} {pct(xs, 0.5):8.1f} {pct(xs, 0.95):8.1f} {min(xs):7.1f} {max(xs):7.1f}")
    print(f"{'hits':12s} p50={pct([float(h) for h in hits['count']], 0.5):.1f}  max={max(hits['count'])}")
    print("（semantic 阶段 = BGE embed + Qdrant search + 回表校验；embedding 在语义阶段内）")


if __name__ == "__main__":
    main()
