"""
Characterization tests for opencrab.ontology.text_cues.

Pins the previously-duplicated Hangul-aware tokenizer (was defined
identically in bm25.py and reranker.py) and confirms both modules now
delegate to the single opencrab.ontology.text_cues.tokenize implementation.
Normal / Error / Edge cases per stage policy.
"""

from __future__ import annotations

import pytest

from opencrab.ontology import bm25, reranker, text_cues

# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------

def test_tokenize_ascii_lowercases_and_splits() -> None:
    assert text_cues.tokenize("Hello, World!") == ["hello", "world"]


def test_tokenize_ascii_strips_punctuation() -> None:
    assert text_cues.tokenize("foo-bar baz_qux") == ["foo", "bar", "baz_qux"]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

def test_tokenize_none_input_raises() -> None:
    """Matches pre-extraction behavior: text.lower() on None raises AttributeError."""
    with pytest.raises(AttributeError):
        text_cues.tokenize(None)  # type: ignore[arg-type]


def test_tokenize_non_str_input_raises() -> None:
    with pytest.raises(AttributeError):
        text_cues.tokenize(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------

def test_tokenize_empty_string() -> None:
    assert text_cues.tokenize("") == []


def test_tokenize_hangul_adds_ngrams() -> None:
    tokens = text_cues.tokenize("변경 이유")
    assert "변경" in tokens
    assert "이유" in tokens
    # 2/3-char n-grams contributed for tokens with len >= 3... but these are
    # exactly length 2, so no n-grams are added (matches len(token) >= 3 gate).
    assert tokens == ["변경", "이유"]


def test_tokenize_hangul_long_token_adds_ngrams() -> None:
    tokens = text_cues.tokenize("적용불가능")
    assert tokens[0] == "적용불가능"
    # 2-char n-grams
    assert "적용" in tokens
    assert "용불" in tokens
    assert "불가" in tokens
    assert "가능" in tokens
    # 3-char n-grams
    assert "적용불" in tokens
    assert "용불가" in tokens
    assert "불가능" in tokens


def test_tokenize_mixed_cjk_ascii_emoji() -> None:
    tokens = text_cues.tokenize("check 개정사항 🚀 now")
    assert "check" in tokens
    assert "now" in tokens
    assert "개정사항" in tokens
    # emoji is punctuation-stripped, contributes no standalone token
    assert "🚀" not in tokens


# ---------------------------------------------------------------------------
# Equivalence: bm25._tokenize and reranker._tokenize both alias text_cues.tokenize
# ---------------------------------------------------------------------------

_CASES = [
    "",
    "Hello, World!",
    "foo-bar baz_qux",
    "변경 이유 배경",
    "적용불가능 상태",
    "check 개정사항 🚀 now  MIXED_Case",
]


@pytest.mark.parametrize("text", _CASES)
def test_bm25_and_reranker_tokenize_are_identical(text: str) -> None:
    assert bm25._tokenize(text) == reranker._tokenize(text) == text_cues.tokenize(text)


def test_bm25_and_reranker_tokenize_is_the_same_function_object() -> None:
    assert bm25._tokenize is text_cues.tokenize
    assert reranker._tokenize is text_cues.tokenize
