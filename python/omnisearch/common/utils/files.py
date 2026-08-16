"""文件类型判定、过滤黑名单（architecture.md §11.1）。

- file_type 判定：扩展名 → image/doc/video/audio/archive/other（files.file_type）
- 黑名单：系统目录 / 临时扩展名（扫描跳过）
- 与 M1 扫描、M2 Worker 入队共用（common 唯一共享层）
"""
from __future__ import annotations

from pathlib import Path

from omnisearch.common.models import FileType

# 扩展名 → file_type 映射（MVP 覆盖常用类型）
_TYPE_MAP: dict[str, FileType] = {
    # image
    ".jpg": FileType.IMAGE, ".jpeg": FileType.IMAGE, ".png": FileType.IMAGE,
    ".gif": FileType.IMAGE, ".bmp": FileType.IMAGE, ".webp": FileType.IMAGE,
    ".heic": FileType.IMAGE, ".tiff": FileType.IMAGE, ".svg": FileType.IMAGE,
    # doc
    ".txt": FileType.DOC, ".md": FileType.DOC, ".markdown": FileType.DOC,
    ".pdf": FileType.DOC, ".doc": FileType.DOC, ".docx": FileType.DOC,
    ".xls": FileType.DOC, ".xlsx": FileType.DOC, ".ppt": FileType.DOC,
    ".pptx": FileType.DOC, ".csv": FileType.DOC, ".json": FileType.DOC,
    ".html": FileType.DOC, ".htm": FileType.DOC, ".rtf": FileType.DOC,
    ".epub": FileType.DOC,
    # video
    ".mp4": FileType.VIDEO, ".avi": FileType.VIDEO, ".mkv": FileType.VIDEO,
    ".mov": FileType.VIDEO, ".wmv": FileType.VIDEO, ".flv": FileType.VIDEO,
    ".webm": FileType.VIDEO,
    # audio
    ".mp3": FileType.AUDIO, ".wav": FileType.AUDIO, ".flac": FileType.AUDIO,
    ".aac": FileType.AUDIO, ".ogg": FileType.AUDIO, ".m4a": FileType.AUDIO,
    # archive
    ".zip": FileType.ARCHIVE, ".rar": FileType.ARCHIVE, ".7z": FileType.ARCHIVE,
    ".tar": FileType.ARCHIVE, ".gz": FileType.ARCHIVE, ".bz2": FileType.ARCHIVE,
}

# 扫描黑名单：临时/缓存类扩展名（直接跳过）
EXTENSION_BLACKLIST = {
    ".tmp", ".temp", ".bak", ".log", ".cache", ".swp", ".lock",
    ".part", ".crdownload", ".dll", ".exe", ".msi", ".sys", ".ini",
}

# 扫描黑名单：系统/隐藏目录名（跳过，不递归）
DIR_BLACKLIST = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "recovery", "perflogs", "appdata",
    "node_modules", ".git", ".svn", ".hg", ".venv", "venv", "__pycache__",
}


def file_type_for(extension: str) -> FileType:
    """扩展名（小写，含点）→ file_type；未知返回 other。"""
    return _TYPE_MAP.get(extension.lower(), FileType.OTHER)


def mime_type_for(filename: str) -> str | None:
    """M1 极简 MIME 判定（按 file_type 给出通用类型；精确判定后续阶段补齐）。"""
    ext = Path(filename).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tiff", ".svg"}:
        return f"image/{ext[1:]}"
    if ext in {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".rtf"}:
        return "text/plain"
    if ext in {".pdf"}:
        return "application/pdf"
    if ext in {".doc", ".docx"}:
        return "application/msword"
    if ext in {".xls", ".xlsx"}:
        return "application/vnd.ms-excel"
    if ext in {".ppt", ".pptx"}:
        return "application/vnd.ms-powerpoint"
    if ext in {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".flv"}:
        return f"video/{ext[1:]}"
    if ext in {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}:
        return f"audio/{ext[1:]}"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}:
        return "application/zip"
    return None


def should_skip_dir(dir_name: str) -> bool:
    """目录是否应跳过（黑名单，不区分大小写）。"""
    return dir_name.lower() in DIR_BLACKLIST


def should_skip_extension(extension: str) -> bool:
    """扩展名（含点）是否应跳过。"""
    return extension.lower() in EXTENSION_BLACKLIST
