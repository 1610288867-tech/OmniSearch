"""路径规范化（修复 M1 发现的路径一致性缺陷）。

问题：scandir（反斜杠）与 watchdog 事件（可能正斜杠）产生的路径分隔符不一致，
导致 get_by_path 精确匹配失败（rename 误判 conflict、删除路径失配）。
统一：所有 files.path 写入与查询入口先 os.path.normpath（Windows 输出反斜杠）。
"""
from __future__ import annotations

import os


def normalize(path: str) -> str:
    """统一路径分隔符（Windows 上 normpath → 反斜杠）。"""
    if not path:
        return path
    return os.path.normpath(path)
