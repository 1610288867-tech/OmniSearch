"""文本切分（architecture.md §10.4：256 token、重叠 32、按段落边界回退，表格/代码块不切）。

- 先按段落（\\n\\n）切：段落 ≤ 上限 → 单 chunk（保持语义完整）
- 超长段落：按句子边界回退 → 仍超长 → 按字符窗口硬切（重叠 32 token）
- token 估算（MVP 简化）：英文按词、中文按字，token ≈ 字数 + 词数
"""
from __future__ import annotations

import re

MAX_TOKENS = 256
OVERLAP_TOKENS = 32

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?.\n])")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：英文词 + 中文/其他字符。"""
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    chars = len(re.sub(r"[A-Za-z0-9_\s]", "", text))
    return words + chars


def _split_by_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_BOUNDARY.split(text) if p.strip()]
    return parts or [text]


def chunk_text(text: str, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[str]:
    """正文 → chunk 列表（顺序保持；空文本返回空列表）。

    始终按段落切（语义单元，architecture.md §10.4「尊重段落边界」）：
    段落 ≤ 上限 → 独立 chunk；超长段落 → 句子边界回退 → 字符窗口硬切（重叠 32）。
    """
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimate_tokens(paragraph) <= max_tokens:
            chunks.append(paragraph)
            continue
        # 超长段落：句子边界回退 → 字符窗口硬切
        sentences = _split_by_sentences(paragraph)
        current = ""
        for sentence in sentences:
            if current and estimate_tokens(current + sentence) > max_tokens:
                chunks.append(current.strip())
                current = current[-overlap:] + sentence  # 重叠 32 token（近似字符重叠）
            else:
                current += sentence
        if current.strip():
            chunks.append(current.strip())
    return [c for c in chunks if c.strip()]
