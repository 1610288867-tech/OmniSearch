"""图片 OCR（architecture.md §10.4：PaddleOCR zh+en）。

- 引擎惰性单例加载（首次调用初始化，模型下载在首次运行）
- decode + OCR 全部在 SQLite 事务外
- 返回 OcrResult(text, confidence)；无文字图片 → 空文本（不视为失败）
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger("omnisearch.worker.ocr")


def normalize_ocr_text(text: str) -> str:
    """OCR 后处理：英文粘连拆词（标准化搜索输入，中文不受影响）。

    实现位于 common/utils/seg.py（worker → common 依赖方向，查询侧复用）。
    """
    from omnisearch.common.utils.seg import split_english_terms

    return split_english_terms(text)

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """PaddleOCR 惰性单例（zh+en；首次加载下载模型）。

    注意：paddleocr 3.x（paddle 3.x）在 Windows 上存在 oneDNN/PIR 兼容问题
    （ConvertPirAttribute2RuntimeAttribute），M3 冻结使用 paddleocr 2.8.x + paddle 2.6.x。
    """
    global _engine
    with _engine_lock:
        if _engine is None:
            from paddleocr import PaddleOCR

            logger.info("loading PaddleOCR (zh+en)...")
            _engine = PaddleOCR(use_angle_cls=True, lang="ch")  # ch 包含中英文
            logger.info("PaddleOCR ready")
        return _engine


@dataclass(frozen=True)
class OcrResult:
    text: str          # 识别文本（多行空格分隔拼接）
    confidence: float  # 平均置信度（无文字 → 0.0）


class OcrError(Exception):
    """OCR 失败（图片损坏/解码失败）。"""


def ocr_image(image_path: str) -> OcrResult:
    """识别图片文字（zh+en）。失败抛 OcrError；无文字返回空文本。"""
    engine = _get_engine()
    try:
        result = engine.ocr(image_path, cls=True)
    except Exception as exc:  # noqa: BLE001 —— 解码/推理失败
        raise OcrError(f"ocr failed: {exc}") from exc

    # paddleocr 2.x：result = [[[box, (text, score)], ...]] 或 [None]（无文字）
    texts: list[str] = []
    scores: list[float] = []
    for line in result or []:
        if not line:
            continue
        for _box, (text, score) in line:
            t = str(text).strip()
            if t:
                texts.append(t)
                scores.append(float(score))

    text = " ".join(texts)
    confidence = sum(scores) / len(scores) if scores else 0.0
    return OcrResult(text=text, confidence=confidence)
