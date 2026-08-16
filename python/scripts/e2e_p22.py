"""P2.2 真实 Windows E2E：content_hash AI 结果复用（spec §十三）。

root-a/: original.txt + image.png
1. 初次扫描 → Worker 完成 AI（OCR/Caption/Embedding）
2. rename original.txt（os.replace 真 rename）→ AI 不重跑 / file_id 保留
3. copy image.png → copy.png → 新 file_id + 相同 hash + AI 复用 + 新 point_id（向量相等）
4. 修改 renamed.txt → hash 变化 → AI pipeline 重新执行

用法：python python/scripts/e2e_p22.py --root dev-data [--url ...] [--token ...]
（Qdrant 端口经 OMNISEARCH_QDRANT_HTTP_PORT 或默认 6333）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from e2e_http import base_url, get, post, read_ports, wait_idle

E2E_ROOT = "p2-hash-test"


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


def _query(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="默认读取 dev.py 落盘端口（.omnisearch-ports.json）")
    ap.add_argument("--token", default="")
    ap.add_argument("--root", default="dev-data")
    args = ap.parse_args()
    args.url = args.url or base_url(args.root)  # T5：端口顺延后仍指向真实 FastAPI

    data_dir = Path(args.root).resolve()
    root = data_dir / E2E_ROOT
    root.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "db" / "omnisearch.db"
    # T5：Qdrant 端口遵循顺延规则——优先 dev.py 落盘端口文件，其次 env，最后 6333
    qdrant_port = os.environ.get("OMNISEARCH_QDRANT_HTTP_PORT") or read_ports(args.root).get("qdrant_http") or "6333"

    # ---- 阶段 1：初次扫描 + AI 完成 ----
    (root / "original.txt").write_text("机器学习与深度学习的关系", encoding="utf-8")
    _make_image(root / "image.png", "New York 2026")
    print("[1] 初次扫描...", flush=True)
    post(args.url, args.token, "/api/v1/index/roots/add", {"path": str(root)})
    st = wait_idle(args.url, args.token)
    n_ai_1 = st["success"]
    print(f"    初次 AI 完成: tasks={st}", flush=True)

    # ---- 阶段 2：真 rename（os.replace → moved 事件 → handle_rename）→ AI 不重跑 / file_id 保留 ----
    print("[2] rename original.txt → renamed.txt（os.replace）", flush=True)
    os.replace(root / "original.txt", root / "renamed.txt")
    time.sleep(4)  # watchdog 防抖（2s）+ 处理窗口
    n_ai_2 = wait_idle(args.url, args.token)["success"]
    print(f"    rename 后任务数: {n_ai_1} → {n_ai_2}（期望不变：rename 不触发 AI）", flush=True)

    # ---- 阶段 3：copy image → 复用 ----
    print("[3] copy image.png → copy.png", flush=True)
    shutil.copy2(root / "image.png", root / "copy.png")
    time.sleep(4)
    n_ai_3 = wait_idle(args.url, args.token)["success"]
    print(f"    copy 后任务数: {n_ai_3}（期望 +1：复制文件一次任务，无 OCR/Caption 重算）", flush=True)
    hashes = dict(_query(db_path, "SELECT filename, content_hash FROM files WHERE filename IN ('image.png','copy.png')"))
    print(f"    hash: image.png={str(hashes.get('image.png'))[:10]} "
          f"copy.png={str(hashes.get('copy.png'))[:10]}（期望相同）", flush=True)
    copy_chunks = _query(db_path, "SELECT source_type, chunk_index, embedding_status FROM chunks WHERE file_id = "
                                  "(SELECT id FROM files WHERE filename='copy.png')")
    print(f"    copy.png chunks: {copy_chunks}（期望 embedding_status=1 复用）", flush=True)
    # Qdrant 强验证：copy.png 新 point_id 存在且 vector == image.png（未重新 BGE inference）
    os.environ["OMNISEARCH_DEV_DATA"] = str(data_dir)
    from omnisearch.common.utils.point_id import point_id
    from omnisearch.common.vector import VectorStore

    vs = VectorStore(f"http://127.0.0.1:{qdrant_port}", 512)
    ids = dict(_query(db_path, "SELECT filename, id FROM files WHERE filename IN ('image.png','copy.png')"))
    vecs_ok = True
    for src_type, idx in (("ocr", 0), ("image_caption", 0)):
        pid_a = point_id(ids["image.png"], src_type, idx)
        pid_b = point_id(ids["copy.png"], src_type, idx)
        a = vs.get_vectors([pid_a])
        b = vs.get_vectors([pid_b])
        same = bool(a and b and a[pid_a][0] == b[pid_b][0])
        vecs_ok = vecs_ok and same
        print(f"    vector {src_type}: image.png==copy.png → {same}（新 point_id 复制，向量相等）", flush=True)

    # ---- 阶段 4：修改 renamed.txt → 重跑 ----
    print("[4] 修改 renamed.txt 内容", flush=True)
    (root / "renamed.txt").write_text("完全不同的新内容关于软件架构设计", encoding="utf-8")
    time.sleep(4)
    n_ai_4 = wait_idle(args.url, args.token)["success"]
    print(f"    修改后任务数: {n_ai_4}（期望 +1：内容变化重新 AI）", flush=True)
    row = _query(db_path, "SELECT chunk_text FROM chunks WHERE file_id=(SELECT id FROM files WHERE filename='renamed.txt')")
    print(f"    renamed.txt 新正文: {row[0][0][:30] if row else None}（期望含『软件架构』）", flush=True)

    ok = (
        n_ai_2 == n_ai_1  # rename：任务数不变（AI 不重跑）
        and n_ai_3 == n_ai_1 + 1  # copy：1 次复用任务
        and n_ai_4 == n_ai_1 + 2  # modify：1 次重新 AI
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
