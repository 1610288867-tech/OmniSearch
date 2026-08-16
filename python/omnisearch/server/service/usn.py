"""NTFS USN Journal 读取器（P2.1，纯 ctypes + Win32 API，无新依赖）。

USN 是启动恢复/加速机制，不是搜索正确性的唯一来源（architecture.md Phase 2）：
- 卷非 NTFS / Journal 不可用 / 权限不足 / Journal 回绕 → 调用方降级增量扫描
- 事件路径解析：RENAME_NEW_NAME/FILE_CREATE 记录只含「文件名最后一段 + 父目录 FRN」，
  通过 FSCTL_GET_NTFS_FILE_RECORD 读取 MFT 记录解析 $FILE_NAME 属性递归得到完整路径
  （父 FRN → 路径缓存，避免重复解析）。

仅 Windows 可用；其他平台 is_available()=False。
"""
from __future__ import annotations

import ctypes
import logging
import platform
import struct
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger("omnisearch.usn")

# USN_REASON 位（winnt.h）
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_EXTEND = 0x00000400
USN_REASON_OVERWRITE = 0x00000800
USN_REASON_DATA_TRUNCATION = 0x00040000
USN_REASON_RENAME_OLD_NAME = 0x00010000
USN_REASON_RENAME_NEW_NAME = 0x00020000

# DeviceIoControl 控制码（NTFS 卷，文档化常量）
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_GET_NTFS_FILE_RECORD = 0x00090098

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILETIME_EPOCH_OFFSET = 11644473600  # 1601-01-01 → 1970-01-01（秒）

ROOT_FRN = 5  # NTFS 卷根目录的 FileReferenceNumber
MAX_PATH_DEPTH = 64
MAX_EVENTS_PER_BATCH = 500


@dataclass(frozen=True)
class UsnRecord:
    """一条 USN Journal 记录（V2/V3 布局相同）。"""

    usn: int
    file_ref: int
    parent_ref: int
    timestamp: int  # unix epoch（秒）
    reason: int
    filename: str  # 事件文件名（最后一段，UTF-16）


def parse_usn_records(data: bytes) -> list[UsnRecord]:
    """解析 READ_USN_JOURNAL 输出缓冲（USN_RECORD_V2/V3 流）。纯函数，可单测。"""
    records: list[UsnRecord] = []
    off = 0
    while off + 62 <= len(data):
        record_length = struct.unpack_from("<I", data, off)[0]
        if record_length < 62 or off + record_length > len(data):
            break
        major = struct.unpack_from("<H", data, off + 4)[0]
        if major not in (2, 3):
            break  # 未知版本：停止解析
        file_ref, parent_ref, usn, ts_ft, reason = struct.unpack_from("<QQQQI", data, off + 8)
        name_len = struct.unpack_from("<H", data, off + 56)[0]
        name_off = struct.unpack_from("<H", data, off + 58)[0]
        filename = ""
        if name_off + name_len <= record_length:
            filename = data[off + name_off : off + name_off + name_len].decode("utf-16-le", errors="replace")
        records.append(
            UsnRecord(
                usn=usn,
                file_ref=file_ref,
                parent_ref=parent_ref,
                timestamp=int(ts_ft // 10**7 - FILETIME_EPOCH_OFFSET),
                reason=reason,
                filename=filename,
            )
        )
        off += record_length
    return records


class _UsnJournalData(ctypes.Structure):
    """USN_JOURNAL_DATA_V0（FSCTL_QUERY_USN_JOURNAL 输出）。"""

    _fields_ = [
        ("UsnJournalID", ctypes.c_ulonglong),
        ("FirstUsn", ctypes.c_ulonglong),
        ("NextUsn", ctypes.c_ulonglong),
        ("LowestValidUsn", ctypes.c_ulonglong),
        ("MaxUsn", ctypes.c_ulonglong),
        ("MaximumSize", ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


class _ReadUsnJournalData(ctypes.Structure):
    """READ_USN_JOURNAL_DATA_V0（FSCTL_READ_USN_JOURNAL 输入）。"""

    _fields_ = [
        ("StartUsn", ctypes.c_ulonglong),
        ("ReasonMask", wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout", ctypes.c_ulonglong),
        ("BytesToWaitFor", wintypes.DWORD),
        ("UsnJournalID", ctypes.c_ulonglong),
    ]


class _NtfsFileRecordInput(ctypes.Structure):
    _fields_ = [("FileReferenceNumber", ctypes.c_ulonglong)]


def _is_windows() -> bool:
    return platform.system() == "Windows"


class UsnReader:
    """NTFS 卷 USN Journal 读取器。

    卷句柄按需打开（缓存）；非 Windows / 打开失败 → 各方法返回 None/False（调用方降级）。
    路径解析（父 FRN → 路径）经 MFT $FILE_NAME 属性递归，结果缓存。
    """

    def __init__(self) -> None:
        self._handles: dict[str, int] = {}  # 卷路径 'D:\\' → 句柄
        self._path_cache: dict[int, str] = {}  # FRN → 目录路径
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True) if _is_windows() else None
        if self._k32 is not None:
            # 64 位句柄必须显式声明（否则默认 int 截断为 32 位）
            self._k32.CreateFileW.restype = ctypes.c_void_p
            self._k32.DeviceIoControl.restype = wintypes.BOOL
            self._k32.GetVolumePathNameW.restype = wintypes.BOOL
            self._k32.GetVolumeInformationW.restype = wintypes.BOOL
            self._k32.CloseHandle.restype = wintypes.BOOL

    # ---------------- 卷 / 能力检测 ----------------

    def volume_for(self, root: str) -> str | None:
        """root 所在卷根路径（'D:\\'）；UNC/无盘符 → None。"""
        drive = ctypes.create_unicode_buffer(4)
        if self._k32 is None:
            return None
        if not self._k32.GetVolumePathNameW(ctypes.c_wchar_p(root), drive, 4):
            return None
        vol = drive.value
        return vol if vol.endswith("\\") else vol + "\\"

    def filesystem_of(self, root: str) -> str | None:
        """root 所在卷的文件系统名（'NTFS'/'FAT32'…）；失败 → None。"""
        if self._k32 is None:
            return None
        name = ctypes.create_unicode_buffer(32)
        vol = self.volume_for(root)
        if vol is None:
            return None
        if not self._k32.GetVolumeInformationW(
            ctypes.c_wchar_p(vol),  # 卷根路径需保留尾部反斜杠（'d:\'）
            None, 0, None, None, None,
            name, ctypes.sizeof(name),
        ):
            return None
        return name.value

    def is_available(self, root: str) -> bool:
        """NTFS 且 Journal 可查询（权限不足 → False → 调用方降级增量扫描）。"""
        try:
            return self.filesystem_of(root) == "NTFS" and self.query_journal(root) is not None
        except Exception:  # noqa: BLE001
            return False

    # ---------------- Journal 查询 / 读取 ----------------

    def query_journal(self, vol: str) -> _UsnJournalData | None:
        """FSCTL_QUERY_USN_JOURNAL；失败（无 Journal/权限）→ None。"""
        handle = self._volume_handle(vol)
        if handle is None:
            return None
        out = _UsnJournalData()
        returned = wintypes.DWORD(0)
        ok = self._k32.DeviceIoControl(
            ctypes.c_void_p(handle), FSCTL_QUERY_USN_JOURNAL,
            None, 0, ctypes.byref(out), ctypes.sizeof(out),
            ctypes.byref(returned), None,
        )
        return out if ok else None

    def read_batch(self, vol: str, start_usn: int, journal_id: int, max_events: int = MAX_EVENTS_PER_BATCH) -> tuple[list[UsnRecord], int, bool]:
        """读取 start_usn 之后的事件批次。

        返回 (records, next_usn, ok)；ok=False（权限/句柄失败）→ 调用方降级。
        next_usn = 本次返回记录的最大 USN（下次 StartUsn）。
        """
        handle = self._volume_handle(vol)
        if handle is None:
            return [], start_usn, False
        req = _ReadUsnJournalData(
            StartUsn=start_usn, ReasonMask=0xFFFFFFFF, ReturnOnlyOnClose=0,
            Timeout=0, BytesToWaitFor=0, UsnJournalID=journal_id,
        )
        # 输出缓冲：每条记录 ≤ 62B 头 + 255W 文件名 ≈ 572B；按 max_events 放大
        buf = ctypes.create_string_buffer(max(4096, max_events * 1024))
        returned = wintypes.DWORD(0)
        ok = self._k32.DeviceIoControl(
            ctypes.c_void_p(handle), FSCTL_READ_USN_JOURNAL,
            ctypes.byref(req), ctypes.sizeof(req),
            buf, ctypes.sizeof(buf), ctypes.byref(returned), None,
        )
        if not ok:
            return [], start_usn, False
        records = parse_usn_records(buf.raw[: returned.value])
        next_usn = records[-1].usn if records else start_usn
        return records, next_usn, True

    # ---------------- 路径解析（父 FRN → 完整路径） ----------------

    def resolve_path(self, vol: str, parent_ref: int, filename: str) -> str | None:
        """父目录 FRN → 完整路径（MFT $FILE_NAME 递归解析 + 缓存）；失败 → None。"""
        parent = self._resolve_dir(vol, parent_ref)
        if parent is None:
            return None
        return parent.rstrip("\\") + "\\" + filename

    def _resolve_dir(self, vol: str, frn: int) -> str | None:
        if frn in self._path_cache:
            return self._path_cache[frn]
        if frn == ROOT_FRN:
            self._path_cache[frn] = vol
            return vol
        parts: list[str] = []
        seen: set[int] = set()
        current = frn
        while current != ROOT_FRN and current not in seen and len(parts) < MAX_PATH_DEPTH:
            seen.add(current)
            if current in self._path_cache:
                base = self._path_cache[current]
                return base.rstrip("\\") + ("\\" + "\\".join(reversed(parts)) if parts else "")
            name, parent = self._read_file_name(vol, current)
            if name is None:
                return None  # 记录不可读（已释放等）→ 调用方丢弃该事件
            parts.append(name)
            current = parent
        if current != ROOT_FRN:
            return None  # 环/超深
        full = vol.rstrip("\\") + "\\" + "\\".join(reversed(parts))
        self._path_cache[frn] = full
        return full

    def _read_file_name(self, vol: str, frn: int) -> tuple[str | None, int | None]:
        """读取 MFT 记录的 $FILE_NAME 属性 → (文件名, 父 FRN)；失败 → (None, None)。

        仅解析驻留属性（$FILE_NAME 恒驻留）；不处理更新序列（USA 只影响 sector 尾 2 字节，
        $FILE_NAME 不跨 sector 边界即可安全忽略——解析失败视为不可读）。
        """
        handle = self._volume_handle(vol)
        if handle is None:
            return None, None
        req = _NtfsFileRecordInput(FileReferenceNumber=frn)
        buf = ctypes.create_string_buffer(4096)  # MFT 记录通常 1024B，缓冲放大
        returned = wintypes.DWORD(0)
        ok = self._k32.DeviceIoControl(
            ctypes.c_void_p(handle), FSCTL_GET_NTFS_FILE_RECORD,
            ctypes.byref(req), ctypes.sizeof(req),
            buf, ctypes.sizeof(buf), ctypes.byref(returned), None,
        )
        if not ok:
            return None, None
        # 输出缓冲：NTFS_FILE_RECORD_OUTPUT_BUFFER {FileReferenceNumber(8), FileNameLength(4), FileName[]}
        data = buf.raw[16 : 16 + struct.unpack_from("<I", buf.raw, 8)[0]]
        if len(data) < 24 or data[:4] != b"FILE":
            return None, None
        first_attr = struct.unpack_from("<H", data, 20)[0]
        off = first_attr
        while off + 16 <= len(data):
            attr_type = struct.unpack_from("<I", data, off)[0]
            attr_len = struct.unpack_from("<I", data, off + 4)[0]
            if attr_len < 24 or off + attr_len > len(data):
                break
            if attr_type == 0x30:  # $FILE_NAME（驻留）
                value_len = struct.unpack_from("<I", data, off + 16)[0]
                value_off = struct.unpack_from("<H", data, off + 20)[0]
                v = off + value_off
                if value_len >= 66 and v + value_len <= len(data):
                    parent = struct.unpack_from("<Q", data, v)[0]
                    name_len = data[v + 64]
                    name = data[v + 66 : v + 66 + name_len * 2].decode("utf-16-le", errors="replace")
                    return name, parent
                return None, None
            off += attr_len
        return None, None

    # ---------------- 句柄管理 ----------------

    def _volume_handle(self, vol: str) -> int | None:
        if self._k32 is None:
            return None
        handle = self._handles.get(vol)
        if handle:
            return handle
        path = f"\\\\.\\{vol.rstrip('\\\\')}"  # 卷路径 'D:\\' → '\\.\D:' 设备路径
        handle = self._k32.CreateFileW(
            ctypes.c_wchar_p(path), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle in (INVALID_HANDLE_VALUE, 0):
            logger.debug("volume handle open failed: %s (access denied? → fallback)", vol)
            return None
        self._handles[vol] = handle
        return handle

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                self._k32.CloseHandle(ctypes.c_void_p(handle))
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()
        self._path_cache.clear()
