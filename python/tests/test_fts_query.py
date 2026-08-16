"""FTS Query Sanitizer 测试（M1 收尾 + M2 分词一致性修复）。"""
from __future__ import annotations

from omnisearch.common.utils.seg import fts_query_for


def test_single_token_prefix():
    assert fts_query_for("resume") == "resume*"
    assert fts_query_for("自由女神") == "自由女神*"  # jieba 专名整体


def test_query_segmented_like_index():
    """M2：查询与写入同分词器——'机器学习' → '机器* 学习*'（前缀 AND 命中正文 seg 列）。"""
    assert fts_query_for("机器学习") == "机器* 学习*"
    assert fts_query_for("resume.pdf") == "resume* pdf*"  # '.' 标点 token 过滤


def test_multi_word_prefix_and():
    assert fts_query_for("report final") == "report* final*"
    assert fts_query_for("live-test") == "live* test*"
    assert fts_query_for("2026-08-14") == "2026* 08* 14*"


def test_reserved_chars_never_syntax_error():
    for q in ['a"b', "(x)", "a:b", "a*b", "a^b", "a~b", "a+b", "a'b", "a[b]c", "a{b}c"]:
        out = fts_query_for(q)
        assert out, f"query {q!r} 不应产生空查询"
        assert not set(out.replace("*", "").replace(" ", "")) & set('"\'(){}[]:^+-~'), f"{q!r} → {out!r}"


def test_operator_words_filtered():
    assert fts_query_for("report and final") == "report* final*"
    assert fts_query_for("a or b") == "a* b*"
    assert fts_query_for("and") == ""  # 纯运算符词 → 空查询（调用方返回空结果，不报错）


def test_empty_and_whitespace():
    assert fts_query_for("") == ""
    assert fts_query_for("   ") == ""
    assert fts_query_for("---") == ""  # 纯保留字符（'-' 替换为空格后为空）


def test_trailing_prefix_semantics_kept():
    """M1 前缀语义保持：单 token 仍为 token*。"""
    assert fts_query_for("resume") == "resume*"
    assert fts_query_for("自由女神") == "自由女神*"
