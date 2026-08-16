"""EXIF 提取与时间链（M5 §12.7/§18H）：exif 字符串 → epoch（本地时区单一实现）。"""
from __future__ import annotations

from datetime import datetime

from omnisearch.common.utils.time import epoch_to_local_iso, exif_str_to_epoch
from omnisearch.worker.pipeline.exif import extract_exif


def test_exif_str_to_epoch_roundtrip():
    """EXIF 字符串（设备本地时间）→ epoch → RFC3339 往返一致。"""
    epoch = exif_str_to_epoch("2026:08:14 19:23:11")
    assert epoch is not None
    iso = epoch_to_local_iso(epoch)
    # 本地时区下应为 2026-08-14T19:23:11+<offset>（round-trip 不依赖具体时区）
    assert iso.startswith("2026-08-14T19:23:11")


def test_exif_date_only():
    epoch = exif_str_to_epoch("2026:08:14")
    assert epoch is not None
    assert epoch_to_local_iso(epoch).startswith("2026-08-14T00:00:00")


def test_exif_invalid_returns_none():
    assert exif_str_to_epoch("not-a-date") is None
    assert exif_str_to_epoch("2026:13:99 99:99:99") is None  # 非法月日时分
    assert exif_str_to_epoch("") is None


def test_exif_epoch_matches_local_interpretation():
    """epoch 按本地时区换算：RFC3339 恢复出的墙钟时间与 EXIF 字符串一致。"""
    epoch = exif_str_to_epoch("2026:08:14 19:23:11")
    dt = datetime.fromtimestamp(epoch)
    assert dt.strftime("%Y:%m:%d %H:%M:%S") == "2026:08:14 19:23:11"


def test_extract_exif_none_for_broken_file(tmp_path):
    """损坏图片/无 EXIF → None（不阻塞流水线）。"""
    f = tmp_path / "broken.jpg"
    f.write_bytes(b"not an image")
    assert extract_exif(str(f)) is None
