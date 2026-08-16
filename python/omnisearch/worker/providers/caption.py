"""LocalImageCaptionProvider —— MVP fallback：Zero-shot Visual Tagging（architecture.md §10.6）。

- Chinese-CLIP（OFA-Sys/chinese-clip-vit-base-patch16，ONNX 导出）视觉端
- 中文标签词表零样本分类 → 输出中文标签文本（只生成文本标签）
- 不保存 CLIP image embedding、不建立 image-vector collection（Phase 3 以图搜图另立）
- 标签文本 → BGE → Qdrant（统一 BGE 文本语义空间，MVP 唯一链路）
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort

from omnisearch.worker.providers.base import CaptionResult

logger = logging.getLogger("omnisearch.worker.caption")

# 中文标签词表（场景/物体/属性；覆盖常见图片语义）
LABELS = [
    "山脉", "湖泊", "海滩", "森林", "城市天际线", "街道", "公园", "雪景", "日落", "星空",
    "人物肖像", "一群人", "儿童", "老人", "运动员", "上班族", "合影",
    "高楼", "桥梁", "寺庙", "古建筑", "现代建筑", "教堂", "塔",
    "汽车", "自行车", "飞机", "火车", "轮船", "食物", "餐桌", "电脑", "手机", "书籍",
    "咖啡", "花", "树", "动物", "猫", "狗", "鸟", "马",
    "室内", "办公室", "厨房", "卧室", "教室", "会议室", "户外", "夜景", "白天",
    "文字", "标志", "公告牌", "截图", "文档", "菜单", "海报",
    "中式建筑", "灯笼", "汉字", "餐馆",
    "彩色", "黑白", "模糊", "明亮",
]

_TOP_K = 3
_SCORE_THRESHOLD = 0.20


def _pool(outs: list[np.ndarray]) -> np.ndarray:
    """ONNX 输出取池化向量：[B, hidden] 直接用；序列输出取 [CLS]（index 0）。"""
    if outs[0].ndim == 2:
        return outs[0]
    if len(outs) > 1 and outs[1].ndim == 2:
        return outs[1]
    return outs[0][:, 0, :]


class LocalImageCaptionProvider:
    """Chinese-CLIP 零样本视觉标签（ONNX CPU）。"""

    def __init__(self, model_dir: Path):
        self._model_dir = model_dir
        self._vision: ort.InferenceSession | None = None
        self._processor = None
        self._label_embeds: np.ndarray | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._vision is not None:
            return
        with self._lock:
            if self._vision is not None:
                return
            model_path = self._model_dir / "vision_model.onnx"
            if not model_path.exists():
                raise RuntimeError(f"caption model not found: {model_path}")
            from transformers import ChineseCLIPProcessor

            self._processor = ChineseCLIPProcessor.from_pretrained(str(self._model_dir))
            self._vision = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            self._label_embeds = self._embed_labels()
            logger.info("caption provider ready (labels=%d)", len(LABELS))

    def _embed_labels(self) -> np.ndarray:
        """标签词表文本向量（text_model.onnx，预计算一次）。"""
        assert self._processor is not None
        text_onnx = self._model_dir / "text_model.onnx"
        if not text_onnx.exists():
            raise RuntimeError(f"text model not found: {text_onnx}")
        session = ort.InferenceSession(str(text_onnx), providers=["CPUExecutionProvider"])
        inputs = self._processor(text=LABELS, return_tensors="np", padding=True)
        outs = session.run(
            None,
            {
                "input_ids": inputs["input_ids"].astype(np.int64),
                "attention_mask": inputs["attention_mask"].astype(np.int64),
            },
        )
        # 输出为序列 [B, seq, hidden]（或含 pooler）；统一取 [CLS]（index 0）
        embeds = _pool(outs)
        embeds = embeds / np.linalg.norm(embeds, axis=1, keepdims=True)
        return embeds.astype(np.float32)

    def caption(self, image_path: Path) -> CaptionResult:
        """图片 → 中文标签文本（top-k 标签拼接；置信度低于阈值 → 空标签文本）。"""
        self._ensure_loaded()
        assert self._vision is not None and self._processor is not None and self._label_embeds is not None
        from PIL import Image

        image = Image.open(image_path).convert("RGB")
        inputs = self._processor(images=image, return_tensors="np")
        outs = self._vision.run(
            None, {"pixel_values": inputs["pixel_values"].astype(np.float32)}
        )
        img_embed = _pool(outs)[0]
        img_embed = img_embed / np.linalg.norm(img_embed)
        scores = self._label_embeds @ img_embed

        order = np.argsort(-scores)[: _TOP_K]
        picked = [(LABELS[i], float(scores[i])) for i in order if scores[i] >= _SCORE_THRESHOLD]
        text = "，".join(label for label, _ in picked)
        confidence = picked[0][1] if picked else 0.0
        return CaptionResult(text=text, model="chinese-clip-tagging", confidence=confidence)
