"""P2.2 真实 Windows E2E：content_hash AI 结果复用（spec §十三）。

root-a/: original.txt + image.png
1. 初次扫描 → Worker 完成 AI（OCR/Caption/Embedding）
2. rename original.txt → AI 不重新执行（无新任务）
3. copy image.png → copy.png → 新 file_id + 相同 hash + AI 复用（无新 inference）+ 新 point_id
4. 修改 original → hash 变化 → AI pipeline 重新执行

用法：需 dev.py 已运行（或本脚本自管理 --manage）。python python/scripts/e2e_p22.py --root dev-data
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
E2E_ROOT = "p2-hash-test"


def _post(url, token, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Omni-Token": token}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _get(url, token, path):
    req = urllib.request.Request(url + path, headers={"X-Omni-Token": token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _wait_idle(url, token, timeout_s=600):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = _get(url, token, "/api/v1/task/status")
        idx = _get(url, token, "/api/v1/index/status")
        if st["queue_length"] == 0 and st["processing"] == 0 and not idx["running"]:
            return st
        time.sleep(1)
    raise TimeoutError("not idle")


def _make_image(path: Path, text: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (480, 320), (210, 220, 235))
    d = ImageDraw.Draw(img)
    font = None
    for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(fp).exists():
            font = ImageFont.truetype(fp, 32)
            break
    d.rectangle([16, 16, 464, 304], outline=(30, 60, 140), width=5)
    d.text((60, 130), text, font=font, fill=(25, 25, 70))
    img.save(path, "PNG")


def _hashes(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT path, content_hash, is_deleted FROM files").fetchall()
    conn.close()
    return {p: h for p, h, _ in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8734")
    ap.add_argument("--token", default="")
    ap.add_argument("--root", default="dev-data")
    args = ap.parse_args()

    data_dir = Path(args.root).resolve()
    root = data_dir / E2E_ROOT
    root.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "db" / "omnisearch.db"

    # ---- 阶段 1：初次扫描 + AI 完成 ----
    (root / "original.txt").write_text("机器学习与深度学习的关系", encoding="utf-8")
    _make_image(root / "image.png", "New York 2026")
    print("[1] 初次扫描...", flush=True)
    _post(args.url, args.token, "/api/v1/index/roots/add", {"path": str(root)})
    st = _wait_idle(args.url, args.token)
    print(f"    初次 AI 完成: tasks={st}", flush=True)
    n_ai_1 = st["success"]

    # ---- 阶段 2：rename → AI 不重跑（hash 相同 → 复用，仅 1 次任务） ----
    print("[2] rename original.txt → renamed.txt", flush=True)
    (root / "renamed.txt").write_text("机器学习与深度学习的关系", encoding="utf-8")
    (root / "original.txt").unlink()
    time.sleep(4)  # watchdog 防抖（2s）+ 处理窗口
    _wait_idle(args.url, args.token)
    st2 = _get(args.url, args.token, "/api/v1/task/status")
    n_ai_2 = st2["success"]
    print(f"    rename 后任务数: {n_ai_1} → {n_ai_2}（期望 +1：renamed.txt 一次任务，复用 AI）", flush=True)

    # ---- 阶段 3：copy image → 复用 ----
    print("[3] copy image.png → copy.png", flush=True)
    shutil.copy2(root / "image.png", root / "copy.png")
    time.sleep(4)  # watchdog 防抖 + 处理
    _wait_idle(args.url, args.token)
    st3 = _get(args.url, args.token, "/api/v1/task/status")
    n_ai_3 = st3["success"]
    print(f"    copy 后任务数: {n_ai_3}（期望 +1：复制文件一次任务，无 OCR/Caption 重算）", flush=True)
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT filename, content_hash FROM files WHERE filename IN ('image.png','copy.png')"
    ).fetchall()
    conn.close()
    hashes = dict(rows)
    print(f"    hash: image.png={hashes.get('image.png')[:10] if hashes.get('image.png') else None} "
          f"copy.png={hashes.get('copy.png')[:10] if hashes.get('copy.png') else None}（期望相同）", flush=True)
    conn = sqlite3.connect(db_path)
    copy_chunks = conn.execute(
        "SELECT source_type, chunk_index, embedding_status FROM chunks WHERE file_id = "
        "(SELECT id FROM files WHERE filename='copy.png')"
    ).fetchall()
    conn.close()
    print(f"    copy.png chunks: {copy_chunks}（期望 embedding_status=1 复用）", flush=True)
    # Qdrant 强验证：copy.png 新 point_id 存在且 vector == image.png（未重新 BGE inference）
    import os as _os

    _os.environ["OMNISEARCH_DEV_DATA"] = str(data_dir)
    from omnisearch.common.utils.point_id import point_id
    from omnisearch.common.vector import VectorStore

    vs = VectorStore("http://127.0.0.1:6333", 512)
    conn = sqlite3.connect(db_path)
    ids = dict(conn.execute("SELECT filename, id FROM files WHERE filename IN ('image.png','copy.png')").fetchall())
    conn.close()
    vecs_ok = True
    for st_, idx in (("ocr", 0), ("image_caption", 0)):
        a = vs.get_vectors([point_id(ids["image.png"], st_, idx)])
        b = vs.get_vectors([point_id(ids["copy.png"], st_, idx)])
        same = bool(a and b and a[point_id(ids["image.png"], st_, idx)][0] == b[point_id(ids["copy.png"], st_, idx)][0])
        vecs_ok = vecs_ok and same
        print(f"    vector {st_}: image.png==copy.png → {same}（新 point_id 复制，向量相等）", flush=True)

    # ---- 阶段 4：修改 original → 重跑 ----
    print("[4] 修改 renamed.txt 内容", flush=True)
    (root / "renamed.txt").write_text("完全不同的新内容关于软件架构设计", encoding="utf-8")
    time.sleep(4)
    _wait_idle(args.url, args.token)
    st4 = _get(args.url, args.token, "/api/v1/task/status")
    n_ai_4 = st4["success"]
    print(f"    修改后任务数: {n_ai_4}（期望 +1：内容变化重新 AI）", flush=True)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT chunk_text FROM chunks WHERE file_id=(SELECT id FROM files WHERE filename='renamed.txt')").fetchone()
    conn.close()
    print(f"    renamed.txt 新正文: {row[0][:30] if row else None}（期望含『软件架构』）", flush=True)

    ok = (
        n_ai_2 == n_ai_1 + 1  # rename：1 次复用任务（无重新 inference）
        and n_ai_3 == n_ai_1 + 2  # copy：1 次复用任务
        and n_ai_4 == n_ai_1 + 3  # modify：1 次重新 AI
        and hashes.get("image.png") == hashes.get("copy.png")
        and all(c[2] == 1 for c in copy_chunks)
        and vecs_ok
        and row and "软件架构" in row[0]
    )
    print(f"\n=== P2.2 E2E {'PASS' if ok else 'FAIL'} ===", flush=True)
    print(f"任务计数: 初扫={n_ai_1} rename后={n_ai_2} copy后={n_ai_3} modify后={n_ai_4}", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
