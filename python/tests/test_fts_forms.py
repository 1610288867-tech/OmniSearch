"""域 C：FTS 查询形式（M5 §18C）—— phrase → AND → OR 逐级降级（architecture.md §8.3）。"""
from __future__ import annotations

from omnisearch.common.utils.seg import fts_query_forms, fts_query_for


def test_single_token_forms():
    assert fts_query_forms("resume") == ["resume*"]
    assert fts_query_forms("自由女神") == ["自由女神*"]  # jieba 专名整体


def test_multi_token_forms_ordered():
    forms = fts_query_forms("机器学习")  # jieba → 机器 学习
    assert forms[0] == '"机器 学习"'  # phrase 优先
    assert forms[1] == "机器* AND 学习*"  # AND
    assert forms[2] == "机器* OR 学习*"  # OR 兜底


def test_english_phrase():
    forms = fts_query_forms("New York")
    assert forms[0] == '"New York"'
    assert forms[1] == "New* AND York*"
    assert forms[2] == "New* OR York*"


def test_forms_sanitize_reserved():
    """保留字符不产生语法错误（与 fts_query_for 同一 sanitize）。"""
    for q in ['a"b', "(x)", "a:b", "a*b", "a~b", "report and final"]:
        forms = fts_query_forms(q)
        assert forms, f"{q!r} 不应产生空 forms"
        assert not set(forms[-1].replace("*", "").replace(" ", "").replace('"', "")) & set("'()[]:^+-~")


def test_empty_query():
    assert fts_query_forms("") == []
    assert fts_query_forms("   ") == []
    assert fts_query_forms("and") == []  # 纯运算符词


def test_fts_query_for_backward_compat():
    """M1/M2 查询语义保持（既有测试不回归）。"""
    assert fts_query_for("机器学习") == "机器* 学习*"
    assert fts_query_for("resume") == "resume*"
