-- ============================================================
-- OmniSearch schema v002（M5：时间过滤 exact 语义，architecture.md §12.7）
-- exif.datetime_original 为 EXIF 原始字符串（设备本地时间）；
-- 新增 epoch 列（按本地时区换算，common/utils/time.py 单一实现），
-- 供 canonical WHERE 直接区间过滤（EXIF → mtime → ctime 链）。
-- ============================================================
ALTER TABLE exif ADD COLUMN datetime_original_epoch INTEGER;
