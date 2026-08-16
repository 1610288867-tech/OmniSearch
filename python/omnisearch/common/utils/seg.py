"""中文预分词（architecture.md §8.3：jieba 预分词 + unicode61）。

- 写入：文本 → jieba 分词 → 空格拼接（存 *_seg 列）
- 查询：raw query → jieba 分词 → 空格拼接 → FTS MATCH（token 间默认 AND）
- M1 不做 phrase→AND→OR 降级（M1 边界：raw query 直接进 FTS）
"""
from __future__ import annotations

import re

import jieba

_jieba_initialized = False


def _ensure_initialized() -> None:
    global _jieba_initialized
    if not _jieba_initialized:
        jieba.initialize()
        _jieba_initialized = True


def seg_text(text: str) -> str:
    """jieba 预分词，以空格拼接（FTS5 *_seg 列格式）。"""
    if not text:
        return ""
    _ensure_initialized()
    tokens = [t.strip() for t in jieba.cut(text) if t.strip()]
    return " ".join(tokens)


# FTS5 保留字符（unicode61 下）：替换为空格（保留其分词作用），避免 MATCH 语法错误
_FTS_RESERVED = re.compile(r'["\'(){}[\]*:^+\-~]')
# FTS5 运算符词（大小写不敏感）：作为用户 token 会破坏查询语法，过滤
_FTS_OPERATOR_WORDS = {"and", "or", "not"}

# 英文粘连拆词（OCR rec 模型不输出空格：NewYork2026 → New York 2026）
_ENGLISH_SPLIT = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"      # 小写/数字 → 大写边界（New|York）
    r"|(?<=[A-Za-z])(?=[0-9])"     # 字母 → 数字边界（York|2026）
    r"|(?<=[0-9])(?=[A-Za-z])"     # 数字 → 字母边界（2026|AI）
)


def split_english_terms(text: str) -> str:
    """英文粘连拆词（OCR 标准化 + 查询侧共用；中文不受影响）。"""
    return _ENGLISH_SPLIT.sub(" ", text)


def _fts_tokens(query: str) -> list[str]:
    """sanitize + 英文拆词 + jieba 分词（与 *_seg 写入一致）；空查询返回 []。"""
    q = query.strip()
    if not q:
        return []
    cleaned = _FTS_RESERVED.sub(" ", q)
    cleaned = split_english_terms(cleaned)  # 英文粘连拆词（与 OCR 标准化一致）
    tokens = [t for t in seg_text(cleaned).split() if t and t.isalnum()]
    return [t for t in tokens if t.lower() not in _FTS_OPERATOR_WORDS]


def fts_query_for(query: str) -> str:
    """构造 FTS 查询（M2 修复：查询与写入同分词器，architecture.md §8.3）。

    流程：sanitize 保留字符 → jieba 分词（与 *_seg 写入一致）→ 每 token 加 `*`
    （前缀 AND，如 '机器学习' → '机器* 学习*'）。
    前缀 AND 理由：jieba 对查询与上下文的分词可能不一致（如 '自由女神' 在句中切为
    '自由 女神像'），前缀放宽每个 token 的匹配；文件名原始列（未分词整串）仍可被
    单 token 前缀命中（M1 语义保持）。过滤纯标点 token（jieba 会输出 '.' 等）。
    M5 起短语/降级查询用 fts_query_forms（phrase → AND → OR）。
    """
    tokens = _fts_tokens(query)
    if not tokens:
        return ""
    return " ".join(f"{t}*" for t in tokens)


def fts_query_forms(query: str) -> list[str]:
    """FTS 查询形式（phrase → AND → OR 逐级降级，architecture.md §8.3）。

    返回有序候选（调用方按召回量依次尝试）：
    - 单 token → [token*]（短语与前缀等价）
    - 多 token → ['"seg1 seg2"', 'seg1* AND seg2*', 'seg1* OR seg2*']
    短语须与写入侧 seg 列 token 流连续（'机器 学习' 写入 ↔ '"机器 学习"' 查询）。
    """
    tokens = _fts_tokens(query)
    if not tokens:
        return []
    if len(tokens) == 1:
        return [f"{tokens[0]}*"]
    forms = [
        f'"{" ".join(tokens)}"',          # phrase：原序连续命中（highest precision）
        " AND ".join(f"{t}*" for t in tokens),  # AND：全 token 前缀（默认语义）
        " OR ".join(f"{t}*" for t in tokens),   # OR：任一 token（召回不足兜底）
    ]
    return forms
