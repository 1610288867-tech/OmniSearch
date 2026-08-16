"""统一日志配置（architecture.md §4.3）：文件轮转（5MB × 3）+ 控制台。"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(name: str, log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger
