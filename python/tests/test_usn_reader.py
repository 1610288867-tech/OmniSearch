"""P2.1 UsnReader 纯解析单元测试（字节流构造，不依赖真实 NTFS/权限）。

真实 DeviceIoControl 路径由真实 Windows E2E 覆盖（权限不足时自动降级增量扫描）。
"""
from __future__ import annotations

import struct

from omnisearch.server.service.usn import (
    USN_REASON_FILE_CREATE,
    USN_REASON_FILE_DELETE,
    UsnRecord,
    parse_usn_records,
)

# FSCTL_GET_NTFS_FILE_RECORD 输出里的 MFT 记录构造（仅用于 _read_file_name 的解析逻辑测试）
_FILE_RECORD_HEADER_FIRST_ATTR = 56  # 常规 FILE_RECORD_HEADER 后接属性


def _make_usn_record(usn: int, file_ref: int, parent_ref: int, reason: int, name: str,
                     ts: int = 132000000000000000) -> bytes:
    name_b = name.encode("utf-16-le")
    rec_len = 62 + len(name_b)
    rec = struct.pack(
        "<IHHQQQQIIIIHHH",
        rec_len, 2, 0, file_ref, parent_ref, usn, ts, reason, 0, 0, 0, len(name_b), 62, 0,
    )
    return rec + name_b


def _make_file_record(name: str, parent_frn: int) -> bytes:
    """构造含单个驻留 $FILE_NAME(0x30) 属性的 MFT 记录（不含 USA）。

    ATTRIBUTE_RECORD_HEADER（resident，24 字节）：<IIBBHHHIHBB
    = TypeCode(4) RecordLength(4) FormCode(1) NameLength(1) NameOffset(2)
      Flags(2) AttributeId(2) ValueLength(4) ValueOffset(2) Reserved(1) IndexedFlag(1)
    """
    name_b = name.encode("utf-16-le")
    attr_value = struct.pack("<Q", parent_frn) + b"\x00" * 56 + bytes([len(name_b) // 2, 0]) + name_b
    attr = struct.pack("<IIBBHHHIHBB", 0x30, 24 + len(attr_value), 0, 0, 0, 0, 1,
                       len(attr_value), 24, 0, 0) + attr_value
    # 记录头：FILE + 22B 字段（FirstAttributeOffset @20）+ 补齐到 first_attr 偏移
    header = b"FILE" + b"\x00" * 16 + struct.pack("<HH", _FILE_RECORD_HEADER_FIRST_ATTR, 0)
    header += b"\x00" * (_FILE_RECORD_HEADER_FIRST_ATTR - len(header))
    return header + attr


def test_parse_single_record():
    data = _make_usn_record(100, 0x111, 0x222, USN_REASON_FILE_CREATE, "新建文件.txt")
    recs = parse_usn_records(data)
    assert len(recs) == 1
    r = recs[0]
    assert r.usn == 100 and r.file_ref == 0x111 and r.parent_ref == 0x222
    assert r.filename == "新建文件.txt"
    assert r.timestamp == 132000000000000000 // 10**7 - 11644473600  # FILETIME → epoch


def test_parse_multiple_records_and_reasons():
    data = (
        _make_usn_record(1, 1, 5, USN_REASON_FILE_CREATE, "a.txt")
        + _make_usn_record(2, 2, 5, USN_REASON_FILE_DELETE, "b.txt")
        + _make_usn_record(3, 3, 5, 0x10000 | 0x20000, "c.txt")  # RENAME_OLD|NEW 同记录
    )
    recs = parse_usn_records(data)
    assert [r.usn for r in recs] == [1, 2, 3]
    assert recs[2].reason == 0x10000 | 0x20000


def test_parse_unicode_filename_and_v3():
    data = _make_usn_record(4, 4, 5, USN_REASON_FILE_CREATE, "自由女神像 2026.jpg")
    assert parse_usn_records(data)[0].filename == "自由女神像 2026.jpg"
    # V3（MajorVersion=3）布局相同
    name_b = "v3.txt".encode("utf-16-le")
    rec = struct.pack("<IHHQQQQIIIIHHH", 62 + len(name_b), 3, 0, 9, 5, 5, 132000000000000000,
                      0x100, 0, 0, 0, len(name_b), 62, 0) + name_b
    assert parse_usn_records(rec)[0].filename == "v3.txt"


def test_parse_truncated_and_garbage_safe():
    assert parse_usn_records(b"") == []
    assert parse_usn_records(b"\x00" * 10) == []  # 不足记录头
    # 记录长度超缓冲 → 停止
    bad = struct.pack("<I", 9999) + b"\x00" * 100
    assert parse_usn_records(bad) == []
    # 未知 major version → 停止
    name_b = "x".encode("utf-16-le")
    rec = struct.pack("<IHHQQQQIIIIHHH", 62 + len(name_b), 99, 0, 1, 5, 1, 1, 0x100, 0, 0, 0, 2, 62, 0) + name_b
    assert parse_usn_records(rec) == []


def test_file_name_attribute_parsing():
    """MFT 记录 $FILE_NAME 解析——直接测试生产纯函数 parse_file_name_attr。

    审查修正（U1/T6）：原测试复刻解析段（生产偏移错误时照样绿）；
    现构造真实 NTFS_FILE_RECORD_OUTPUT_BUFFER（记录起点偏移 12）并调用真实函数。
    """
    from omnisearch.server.service.usn import parse_file_name_attr

    record = _make_file_record("子目录", 0xABCD)
    name, parent = parse_file_name_attr(record)
    assert name == "子目录" and parent == 0xABCD


def test_file_record_output_buffer_offset_12():
    """U1 回归：NTFS_FILE_RECORD_OUTPUT_BUFFER 中 MFT 记录起点 = 偏移 12（非 16）。

    若生产切片偏移错误（16），'FILE' 魔数匹配失败 → parse 返回 None —— 该测试锁定偏移。
    """
    from omnisearch.server.service.usn import parse_file_name_attr

    record = _make_file_record("locked.txt", 0x1234)
    # 构造完整输出缓冲：FileReferenceNumber(8) + FileRecordLength(4) + FileRecordBuffer@12
    buf = b"\x00" * 8 + struct.pack("<I", len(record)) + record
    # 偏移 16 的错误切片会破坏 'FILE' 魔数（读取到 UsaOffset/UsaCount）
    assert buf[16:20] != b"FILE"  # 16 处是记录内字段，非魔数
    name, parent = parse_file_name_attr(buf[12 : 12 + len(record)])  # 正确偏移 12
    assert name == "locked.txt" and parent == 0x1234
