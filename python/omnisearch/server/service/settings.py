"""SettingsService —— 搜索设置 / 模型状态 / 存储信息（M5，architecture.md §13）。"""
from __future__ import annotations

from pathlib import Path

from omnisearch.common.database import Database
from omnisearch.server.repository.settings import SettingsRepository

# 默认值（settings 表未写时生效；Watchdog debounce 2s 保持内部常量不暴露）
DEFAULTS: dict[str, object] = {"search_mode": "hybrid", "w_kw": 1.0, "w_sem": 1.0, "topK": 50}

# 模型文件探针：{key: (子目录, 需存在的文件名)}
_MODEL_PROBES: dict[str, tuple[str, tuple[str, ...]]] = {
    "bge": ("", ("model.onnx", "tokenizer.json", "config.json")),
    "caption": ("chinese-clip", ("vision_model.onnx", "text_model.onnx")),
}


class SettingsService:
    def __init__(self, db: Database, repo: SettingsRepository, models_dir: Path | None = None):
        self._db = db
        self._repo = repo
        self._models_dir = models_dir  # None → 模型状态标 missing（不探测）

    # ---- 搜索设置 ----
    def get(self) -> dict:
        out = {}
        for key, default in DEFAULTS.items():
            out[key] = self._repo.get(key, default)
        out["index_roots"] = self._repo.get_index_roots()
        out["models"] = self._model_status()
        out["storage"] = self._storage()
        return out

    def update(self, patch: dict) -> dict:
        for key, value in patch.items():
            if key in DEFAULTS and value is not None:
                self._repo.set(key, value)
        return self.get()

    # ---- 模型状态（只读探针：文件存在性，不加载） ----
    def _model_status(self) -> dict[str, str]:
        if self._models_dir is None:
            return {"bge": "missing", "caption": "missing"}
        status = {}
        for key, (sub, probes) in _MODEL_PROBES.items():
            base = self._models_dir / sub if sub else self._models_dir
            status[key] = "ok" if all((base / p).exists() for p in probes) else "missing"
        return status

    # ---- 基础存储信息 ----
    def _storage(self) -> dict[str, int]:
        db_bytes = self._db.path.stat().st_size if self._db.path.exists() else 0
        models_bytes = 0
        if self._models_dir is not None and self._models_dir.exists():
            models_bytes = sum(
                p.stat().st_size for p in self._models_dir.rglob("*") if p.is_file()
            )
        return {"db_bytes": db_bytes, "models_bytes": models_bytes}
