"""模型下载（architecture.md §10.8：manifest 驱动 + sha256 校验，T3 修正：实现真实下载）。

下载 models/manifest.json 中全部模型到用户数据目录（OMNISEARCH_DEV_DATA/models 或
%LOCALAPPDATA%/OmniSearch/models）；sha256 非空 → 校验，失败抛错退出非零。
用法：python python/scripts/download_models.py [--model bge-small-zh-v1.5-onnx]
"""
from __future__ import annotations

import argparse
import logging
import sys

from omnisearch.common.utils.models import download_model, load_manifest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="只下载指定 model id（默认全部）")
    args = ap.parse_args()

    manifest = load_manifest()
    models = manifest["models"]
    if args.model:
        models = [m for m in models if m["id"] == args.model]
        if not models:
            print(f"model not in manifest: {args.model}", file=sys.stderr)
            return 1

    ok = True
    for m in models:
        try:
            path = download_model(m["id"])
            print(f"ok: {m['id']} → {path} ({m.get('size_mb')} MB)")
        except Exception as exc:  # noqa: BLE001 —— 单模型失败不影响其余
            print(f"FAIL: {m['id']}: {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
