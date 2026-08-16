"""模型管理工具（architecture.md §10.8）：manifest 驱动下载 + sha256 校验。

- 模型下载到用户数据目录（OMNISEARCH_DEV_DATA/models 或 %LOCALAPPDATA%/OmniSearch/models），不进入 Git
- manifest 位于仓库 models/manifest.json（id/name/source/sha256/size_mb/format/required_by）
- sha256 为空 → 下载后计算并回填 manifest；非空 → 下载后校验
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from pathlib import Path

from omnisearch.common.config import dev_data_dir

logger = logging.getLogger("omnisearch.models")

REPO_MANIFEST = Path(__file__).resolve().parents[4] / "models" / "manifest.json"


def models_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or dev_data_dir()) / "models"


def load_manifest() -> dict:
    return json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_model(model_id: str, data_dir: Path | None = None, verify: bool = True) -> Path:
    """下载模型到用户数据目录；返回本地路径。校验失败抛 RuntimeError。"""
    manifest = load_manifest()
    entry = next((m for m in manifest["models"] if m["id"] == model_id), None)
    if entry is None:
        raise RuntimeError(f"model not in manifest: {model_id}")

    dest_dir = models_dir(data_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 文件名取 URL 最后一段（model.onnx / tokenizer.json / config.json）
    filename = entry["source"].rsplit("/", 1)[-1]
    dest = dest_dir / filename
    if dest.exists():
        if verify and entry.get("sha256") and sha256_file(dest) != entry["sha256"]:
            raise RuntimeError(f"model {model_id}: sha256 mismatch (existing file)")
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")  # 断点续传标记（P2 完整实现；M4 简单 .part）
    logger.info("downloading %s (%s MB)...", model_id, entry.get("size_mb"))
    urllib.request.urlretrieve(entry["source"], tmp)  # noqa: S310 —— manifest 受控来源
    tmp.replace(dest)

    actual = sha256_file(dest)
    if entry.get("sha256") and verify and actual != entry["sha256"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"model {model_id}: sha256 mismatch (downloaded {actual[:12]}...)")
    if not entry.get("sha256"):
        # 首次下载：回填 manifest（便于后续校验）
        entry["sha256"] = actual
        manifest["models"] = [entry if m["id"] == model_id else m for m in manifest["models"]]
        REPO_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("model %s sha256 recorded: %s", model_id, actual)
    return dest
