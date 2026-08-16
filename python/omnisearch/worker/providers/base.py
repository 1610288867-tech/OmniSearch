"""Provider 抽象（architecture.md §10.7 极简版）。

MVP 仅：ImageCaptionProvider（视觉标签文本生成）+ EmbeddingProvider（common/embedding.py）。
不做 Provider Registry/路由框架；云端 Provider 为 P3。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CaptionResult:
    text: str          # 中文标签/描述文本
    model: str         # 来源模型标识
    confidence: float  # 最高置信度


class ImageCaptionProvider(Protocol):
    def caption(self, image_path: Path) -> CaptionResult: ...
