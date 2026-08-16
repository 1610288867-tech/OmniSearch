"""文本切分测试（M2：architecture.md §10.4 chunker）。"""
from __future__ import annotations

from omnisearch.worker.pipeline.chunker import chunk_text, estimate_tokens


def test_short_text_single_chunk():
    assert chunk_text("hello world") == ["hello world"]
    assert chunk_text("短文本") == ["短文本"]


def test_empty_text():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_paragraphs_become_chunks():
    text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
    chunks = chunk_text(text)
    assert len(chunks) == 3
    assert "第一段内容" in chunks[0] and "第三段内容" in chunks[2]


def test_long_paragraph_split_by_sentences():
    """超长段落按句子边界切分（256 token 上限）。"""
    sentence = "这是第%d个测试句子，用于验证切分逻辑。" % 1
    text = "".join(f"第{i}个句子内容用于填充长度。" for i in range(60))
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    for c in chunks:
        assert estimate_tokens(c) <= 256


def test_overlap_preserved():
    """重叠 32 字符：下一个 chunk 头部包含上一 chunk 尾部（跨 chunk 边界词可检索）。"""
    text = "".join(f"段落填充内容第{i}号。" for i in range(80))
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    tail = chunks[0][-32:]
    assert tail in chunks[1]  # 重叠段出现在相邻 chunk 中


def test_ordering_preserved():
    text = "开头内容。" + "中间填充。" * 80 + "结尾内容。"
    chunks = chunk_text(text)
    assert "开头内容" in chunks[0]
    assert "结尾内容" in chunks[-1]


def test_hard_split_unbounded_sentence():
    """W3：超长单句（无标点边界）按字符窗口硬切（相邻窗口重叠）。"""
    long_text = "无标点内容" * 200  # 1000 字符，无任何句子边界
    chunks = chunk_text(long_text)
    assert len(chunks) > 1  # 被硬切
    assert all(len(c) <= 300 for c in chunks)  # 窗口 + 重叠余量
    assert "".join(chunks)  # 内容保留（重叠导致拼接略长，至少不丢失首尾）
    assert chunks[0].startswith("无标点内容")
    assert chunks[-1].endswith("内容")
