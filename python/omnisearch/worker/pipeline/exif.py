"""图片 EXIF 提取（M5：architecture.md §7.1/§12.7 时间过滤 exact 语义）。

- datetime_original：EXIF 原始字符串（DateTimeOriginal, tag 0x9003）
- datetime_original_epoch：按本地时区换算（设备本地时间语义，common/utils/time.py
  单一实现——Worker 侧禁止自行实现日期计算，architecture.md §8）
- 其余字段（尺寸/相机/ISO 等）MVP 暂不提取（P2/详情面板扩展）
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("omnisearch.worker.exif")

_TAG_DATETIME_ORIGINAL = 0x9003


def extract_exif(path: str) -> dict | None:
    """读取图片 EXIF DateTimeOriginal；无 EXIF/解析失败返回 None（不阻塞流水线）。"""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        with Image.open(path) as im:
            raw = im.getexif()
        dt = raw.get(_TAG_DATETIME_ORIGINAL)
        if not dt:
            return None
        from omnisearch.common.utils.time import exif_str_to_epoch

        epoch = exif_str_to_epoch(str(dt))
        if epoch is None:
            logger.warning("unparseable exif datetime: %r (%s)", dt, Path(path).name)
            return None
        return {"datetime_original": str(dt), "datetime_original_epoch": epoch}
    except Exception as exc:  # noqa: BLE001 —— 图片损坏/无 EXIF → None（OCR/Caption 仍继续）
        # W5：异常必须留痕——静默吞掉会掩盖「字段误用/EXIF 解析器行为变化」等真实问题
        logger.warning("exif extract failed for %s: %s", path, exc, exc_info=True)
        return None
