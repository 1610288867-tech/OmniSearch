"""模型下载（architecture.md §10.8：manifest 驱动 + sha256 校验）。

M0 仅占位：模型清单结构在 models/manifest.json；
下载、断点续传（.part）、校验实现在 M4（模型下载向导）之前补齐。
"""
from __future__ import annotations

import sys


def main() -> None:
    print(
        "download_models: 占位实现（M4 前完成）。"
        "请先将模型信息登记到 models/manifest.json（id/name/source/sha256/size_mb/format）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
