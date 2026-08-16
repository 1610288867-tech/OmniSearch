"""路径规范化（修复 M1 发现的路径一致性缺陷）。

问题：scandir（反斜杠）与 watchdog 事件（可能正斜杠）产生的路径分隔符不一致，
导致 get_by_path 精确匹配失败（rename 误判 conflict、删除路径失配）。
统一：所有 files.path 写入与查询入口先 os.path.normpath（Windows 输出反斜杠）。
"""
from __future__ import annotations

import os
import re

_DRIVE_ROOT = re.compile(r"^[A-Za-z]:$")


def normalize(path: str) -> str:
    """统一路径分隔符（Windows 上 normpath → 反斜杠）。"""
    if not path:
        return path
    return os.path.normpath(path)


def canonical_root(path: str) -> str:
    """扫描 Root 规范化（扫描位置管理）：normpath + trailing slash 统一 + 盘符根保留。

    - 'd:/photos/' → 'd:\\photos'（分隔符统一、去尾部斜杠）
    - 'D:' → 'D:\\'（裸盘符补根斜杠）；'D:\\' → 'D:\\'（盘符根保留尾部斜杠）
    比较（重复/父子检测）一律用小写 key（Windows 大小写不敏感）。
    """
    p = os.path.normpath(path)
    if _DRIVE_ROOT.match(p):  # 'C:' → 'C:\\'
        return p + "\\"
    if re.match(r"^[A-Za-z]:\\$", p):  # 盘符根保留尾部斜杠
        return p
    return p.rstrip("\\/")


def root_key(path: str) -> str:
    """Root 比较键（Windows 大小写不敏感 + 统一斜杠）。"""
    return canonical_root(path).lower()


def root_covers(child: str, parent: str) -> bool:
    """parent 是否覆盖 child（父子 Root 检测）：'D:\\' 覆盖 'D:\\Photos'；'D:\\Photos' 覆盖 'D:\\Photos\\2026'。"""
    c = root_key(child)
    p = root_key(parent).rstrip("\\")
    return c == p or c.startswith(p + "\\")
