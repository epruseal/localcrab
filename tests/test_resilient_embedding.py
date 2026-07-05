"""Tests for multi-remote embedding chain: OPENAI_API_BASE comma-separated
parsing (config.py) + ResilientEmbeddingFunction sequential primary fallback
(resilient_embedding.py). See feat/multi-remote-embedding design.

No real network calls — all EFs here are in-process mocks.
"""

from __future__ import annotations

import pytest

from opencrab.config import Settings
from opencrab.stores.resilient_embedding import ResilientEmbeddingFunction

# ----------------------------------------------------------------------
# config.py: openai_api_bases comma parsing
# ----------------------------------------------------------------------


def test_single_url_default() -> None:
    settings = Settings(OPENAI_API_BASE="http://localhost:1234/v1")
    assert settings.openai_api_bases == ["http://localhost:1234/v1"]


def test_multiple_urls_comma_separated() -> None:
    settings = Settings(
        OPENAI_API_BASE="http://a:1234/v1,http://b:1234/v1"
    )
    assert settings.openai_api_bases == ["http://a:1234/v1", "http://b:1234/v1"]


def test_multiple_urls_with_whitespace() -> None:
    settings = Settings(
        OPENAI_API_BASE=" http://a:1234/v1 , http://b:1234/v1 "
    )
    assert settings.openai_api_bases == ["http://a:1234/v1", "http://b:1234/v1"]


def test_empty_entries_removed() -> None:
    settings = Settings(
        OPENAI_API_BASE="http://a:1234/v1,,http://b:1234/v1,"
    )
    assert settings.openai_api_bases == ["http://a:1234/v1", "http://b:1234/v1"]


def test_default_settings_single_base() -> None:
    """OPENAI_API_BASE 미설정 시 기존 기본값(단일 URL) 그대로."""
    settings = Settings()
    assert settings.openai_api_bases == [settings.openai_api_base]
    assert len(settings.openai_api_bases) == 1


# ----------------------------------------------------------------------
# ResilientEmbeddingFunction: mock EFs
# ----------------------------------------------------------------------


class _MockEF:
    """Minimal EF protocol mock: callable, name(), ping(), api_base."""

    def __init__(self, label: str, *, fail: bool = False, unavailable: bool = False):
        self.label = label
        self.fail = fail
        self.unavailable = unavailable
        self.calls: list[list[str]] = []
        self.api_base = f"http://{label}/v1"

    def __call__(self, input: list[str]) -> list[list[float]]:
        self.calls.append(input)
        if self.fail:
            raise RuntimeError(f"{self.label} down")
        return [[1.0, 0.0] for _ in input]

    def name(self) -> str:
        return "kure_v1"

    def ping(self) -> bool:
        return not self.unavailable


class _MockFallback:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, input: list[str]) -> list[list[float]]:
        self.calls.append(input)
        return [[0.0, 1.0] for _ in input]

    def name(self) -> str:
        return "kure_v1_gguf"


# ----------------------------------------------------------------------
# Backward compatibility: single EF (not a list)
# ----------------------------------------------------------------------


def test_single_primary_backward_compat_success() -> None:
    primary = _MockEF("only")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=primary, fallback=fallback)

    result = ef(["hello"])

    assert result == [[1.0, 0.0]]
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 0
    assert ef.name() == "kure_v1"


def test_single_primary_backward_compat_failure_falls_back() -> None:
    primary = _MockEF("only", fail=True)
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=primary, fallback=fallback)

    result = ef(["hello"])

    assert result == [[0.0, 1.0]]
    assert len(fallback.calls) == 1


# ----------------------------------------------------------------------
# Sequential multi-primary chain
# ----------------------------------------------------------------------


def test_primary1_fails_primary2_succeeds_no_fallback() -> None:
    p1 = _MockEF("p1", fail=True)
    p2 = _MockEF("p2")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback)

    result = ef(["hello"])

    assert result == [[1.0, 0.0]]
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1
    assert len(fallback.calls) == 0


def test_primary1_unhealthy_ttl_skips_straight_to_primary2() -> None:
    p1 = _MockEF("p1", fail=True)
    p2 = _MockEF("p2")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback, health_ttl=60.0)

    ef(["first call"])  # p1 fails once, marks unhealthy, p2 serves
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1

    ef(["second call"])  # p1 should be skipped now (within TTL)
    assert len(p1.calls) == 1  # unchanged — not retried
    assert len(p2.calls) == 2


def test_all_primaries_fail_uses_fallback() -> None:
    p1 = _MockEF("p1", fail=True)
    p2 = _MockEF("p2", fail=True)
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback)

    result = ef(["hello"])

    assert result == [[0.0, 1.0]]
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1
    assert len(fallback.calls) == 1


def test_all_primaries_unhealthy_skips_to_fallback_without_calling() -> None:
    p1 = _MockEF("p1", fail=True)
    p2 = _MockEF("p2", fail=True)
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback, health_ttl=60.0)

    ef(["warm up"])  # both fail, both marked unhealthy
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1

    ef(["second call"])  # both should be skipped (within TTL), straight to fallback
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1
    assert len(fallback.calls) == 2


def test_name_returns_first_primary_name() -> None:
    p1 = _MockEF("p1")
    p2 = _MockEF("p2")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback)
    assert ef.name() == "kure_v1"


def test_embed_query_delegates_to_call() -> None:
    p1 = _MockEF("p1")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1], fallback=fallback)
    assert ef.embed_query(["q"]) == [[1.0, 0.0]]


def test_empty_input_returns_empty_without_calling_anything() -> None:
    p1 = _MockEF("p1")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1], fallback=fallback)
    assert ef([]) == []
    assert len(p1.calls) == 0


def test_force_check_clears_ttl_for_recovered_primary() -> None:
    p1 = _MockEF("p1", fail=True)
    p2 = _MockEF("p2")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback, health_ttl=60.0)

    ef(["call"])  # p1 marked unhealthy
    assert len(p1.calls) == 1

    p1.fail = False  # simulate recovery
    healthy = ef.force_check()
    assert healthy is True

    ef(["after recovery"])
    assert len(p1.calls) == 2  # p1 retried since TTL cleared


def test_force_check_returns_false_when_all_unavailable() -> None:
    p1 = _MockEF("p1", unavailable=True)
    p2 = _MockEF("p2", unavailable=True)
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1, p2], fallback=fallback)
    assert ef.force_check() is False


def test_empty_primary_list_raises() -> None:
    fallback = _MockFallback()
    with pytest.raises(ValueError):
        ResilientEmbeddingFunction(primary=[], fallback=fallback)


# ----------------------------------------------------------------------
# factory._make_kure_embedding_function: multi-base wiring (no network — EF
# construction is pure, only __call__/ping hit the network).
# ----------------------------------------------------------------------


def test_factory_wires_one_ef_per_comma_separated_base() -> None:
    from opencrab.stores.factory import _make_kure_embedding_function
    from opencrab.stores.openai_embedding import OpenAIEmbeddingFunction

    settings = Settings(
        OPENAI_API_BASE="http://a:1234/v1,http://b:1234/v1",
        EMBEDDING_BACKEND="openai",
    )
    ef = _make_kure_embedding_function(settings)

    assert isinstance(ef, ResilientEmbeddingFunction)
    assert len(ef._primaries) == 2
    assert all(isinstance(p, OpenAIEmbeddingFunction) for p in ef._primaries)
    assert ef._primaries[0].api_base == "http://a:1234/v1"
    assert ef._primaries[1].api_base == "http://b:1234/v1"


def test_factory_single_base_wires_one_ef() -> None:
    from opencrab.stores.factory import _make_kure_embedding_function

    settings = Settings(
        OPENAI_API_BASE="http://localhost:1234/v1",
        EMBEDDING_BACKEND="openai",
    )
    ef = _make_kure_embedding_function(settings)
    assert len(ef._primaries) == 1
