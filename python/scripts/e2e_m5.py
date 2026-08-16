"""M5 真实 E2E（§19）：真实图片（自由女神/纽约街景/建筑，PIL 绘制）+ 文档（机器学习/深度学习/软件架构）→ 扫描 → AI 处理 → 4 条验收查询。

用法：FastAPI 已运行（dev.py）。python python/scripts/e2e_m5.py [--url http://127.0.0.1:8734] [--token XXX] [--root dev-data]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def _post(url: str, token: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Omni-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(url: str, token: str, path: str) -> dict:
    req = urllib.request.Request(url + path, headers={"X-Omni-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _make_image(path: Path, text: str, size=(640, 400), exif_dt: str | None = None) -> None:
    """PIL 绘制带中文文字的图片（OCR 可识别；Chinese-CLIP 生成标签）。

    exif_dt：写入 EXIF DateTimeOriginal（'2026:08:15 10:00:00'），
    E2E 用例 1 需要「昨天」时间过滤 + exact 可信度（§12.7）。
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", size, (200, 220, 240))
    d = ImageDraw.Draw(img)
    font = None
    for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if Path(fp).exists():
            font = ImageFont.truetype(fp, 36)
            break
    d.rectangle([20, 20, size[0] - 20, size[1] - 20], outline=(40, 80, 160), width=6)
    d.text((60, size[1] // 2 - 30), text, font=font, fill=(30, 30, 80))
    exif = None
    if exif_dt:
        exif = Image.Exif()
        exif[0x9003] = exif_dt  # DateTimeOriginal
    img.save(path, "JPEG", exif=exif)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8734")
    ap.add_argument("--token", default="")
    ap.add_argument("--root", default="dev-data")
    args = ap.parse_args()

    root = Path(args.root).resolve() / "verify-m5"
    root.mkdir(parents=True, exist_ok=True)
    (Path(args.root).resolve() / "verify-root").mkdir(parents=True, exist_ok=True)

    # ---- 1) 真实素材（§19：图片 + 文档 + OCR） ----
    print("[1] 生成素材...")
    # 照片设「昨天」EXIF（拍摄时间，exact）+ mtime 昨天（fallback 一致）——用例 1 时间过滤
    yesterday = "2026:08:15 10:00:00"
    for name, text in (("statue-of-liberty.jpg", "自由女神像 2026 拍摄于纽约"),
                       ("ny-street.jpg", "New York 2026 街景"),
                       ("building.jpg", "建筑 会议纪要")):
        _make_image(root / name, text, exif_dt=yesterday)
        import os as _os, time as _t
        _os.utime(root / name, (_t.time() - 86400, _t.time() - 86400))
    (root / "机器学习.txt").write_text("机器学习是人工智能的重要分支，核心是让系统从数据中学习规律。", encoding="utf-8")
    (root / "深度学习.md").write_text("深度学习基于多层神经网络，在图像识别与自然语言处理中表现出色。", encoding="utf-8")
    (root / "软件架构.md").write_text("软件架构设计关注模块划分、依赖方向与可维护性，是工程质量的基石。", encoding="utf-8")
    # GUI smoke 数据（verify-omnisearch Skill 步骤 6/5 依赖）
    (Path(args.root).resolve() / "verify-root" / "verify-alpha.pdf").write_bytes(b"%PDF-1.4 fake pdf for gui smoke")

    # ---- 2) 扫描 ----
    print("[2] 扫描 verify-m5 + verify-root...")
    for r in (root, Path(args.root).resolve() / "verify-root"):
        job = _post(args.url, args.token, "/api/v1/index/scan", {"root_paths": [str(r)], "scan_type": "full"})
        for _ in range(300):
            st = _get(args.url, args.token, "/api/v1/index/status")
            if not st.get("running"):
                break
            time.sleep(1)
        print(f"    scan {r.name} job={job['job_id']} done")

    # ---- 3) 等待 AI 任务完成（OCR + Caption + Embedding） ----
    print("[3] 等待 AI 任务...")
    for _ in range(600):
        st = _get(args.url, args.token, "/api/v1/task/status")
        if st["queue_length"] == 0 and st["processing"] == 0:
            break
        time.sleep(1)
    print(f"    task stats: {st}")

    # ---- 4) 四条验收查询（§19） ----
    queries = [
        "昨天的自由女神照片",   # 1: type=image + semantic + RRF + match reasons
        "包含 New York 的图片",  # 2: OCR 命中
        "机器学习架构",          # 3: keyword + semantic 双通道
        "关于神经网络的文档",     # 4: 自然语言 → semantic
    ]
    failed = 0
    for q in queries:
        body = _post(args.url, args.token, "/api/v1/search", {"query": q, "topK": 5})
        print(f"\n[4] query: {q!r}")
        print(f"    parsed: {json.dumps(body['parsed'], ensure_ascii=False)}  degraded={body['degraded_channels']}  total={body['total']}")
        for r in body["results"][:3]:
            reasons = ", ".join(f"{x['channel']}:{x['text'][:26]}" for x in r["match_reasons"][:3])
            print(f"    - {r['filename']}  rrf={r['rrf_score']:.4f} kw={r['keyword_score']} sem={r['semantic_score']:.3f}  [{reasons}]")
        if body["total"] == 0:
            failed += 1
            print(f"    !! 无结果（不应发生）")

    print(f"\nE2E {'PASS' if failed == 0 else 'FAIL'}（{4 - failed}/4 查询有结果）")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
