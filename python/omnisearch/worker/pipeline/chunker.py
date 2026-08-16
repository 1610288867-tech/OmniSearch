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
            if estimate_tokens(sentence) > max_tokens:
                # 超长单句（无句内边界，如长 URL/日志/无标点中文）：字符窗口硬切（W3 修正）
                if current.strip():
                    chunks.append(current.strip())
                    current = ""
                for piece in _hard_split(sentence, max_tokens, overlap):
                    chunks.append(piece)
                continue
            if current and estimate_tokens(current + sentence) > max_tokens:
                chunks.append(current.strip())
                current = current[-overlap:] + sentence  # 重叠 32 token（近似字符重叠）
            else:
                current += sentence
        if current.strip():
            chunks.append(current.strip())
    return [c for c in chunks if c.strip()]


def _hard_split(text: str, max_tokens: int, overlap: int) -> list[str]:
    """字符窗口硬切（W3）：无标点边界的超长文本按 token 上限切分，相邻窗口重叠。

    窗口 = max_tokens 字符（中文 1 字 ≈ 1 token 近似；英文整词不回退导致超限时
    按窗口截断——无空格超长串（URL/哈希）本来无词边界可循）。
    """
    pieces: list[str] = []
    window = max_tokens
    start = 0
    while start < len(text):
        end = min(start + window, len(text))
        piece = text[start:end]
        if end < len(text):
            # 非末尾：尽量回退到空格避免截断词（回退不少于一半窗口）
            space = piece.rfind(" ")
            if space >= window // 2:
                end = start + space
                piece = text[start:end]
            pieces.append(piece)
            start = end - overlap if end - overlap > start else end
        else:
            pieces.append(piece)
            break
    return pieces
