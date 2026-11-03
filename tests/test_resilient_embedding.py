"""Tests for multi-remote embedding chain: OPENAI_API_BASE comma-separated
parsing (config.py) + ResilientEmbeddingFunction sequential primary fallback
(resilient_embedding.py). See feat/multi-remote-embedding design.

No real network calls — all EFs here are in-process mocks.
"""

from __future__ import annotations

import logging

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


def test_fallback_calls_counted() -> None:
    """폴백 사용을 집계한다.

    2026-08-04 적재에서 primary 가 통째로 오배선돼 1,218건 전량이 CPU 폴백으로
    갔는데, 신호가 WARNING 로그뿐이라 43분이 지나서야 발견됐다. 호출부가 종료
    요약에 "몇 배치가 폴백이었나"를 찍을 수 있어야 한다.
    """
    p1 = _MockEF("p1", fail=True)
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1], fallback=fallback)

    assert ef.fallback_calls == 0
    assert ef.primary_calls == 0

    ef(["a"])
    assert ef.fallback_calls == 1
    assert ef.primary_calls == 0

    ef(["b"])          # TTL 안이라 primary 를 건너뛰고 바로 폴백
    assert ef.fallback_calls == 2
    assert ef.primary_calls == 0


def test_primary_calls_counted_and_no_fallback_on_success() -> None:
    """정상 경로에서는 폴백 카운터가 0이어야 한다 — 카운터가 늘 켜져 있으면
    경보로서 의미가 없다."""
    p1 = _MockEF("p1")
    fallback = _MockFallback()
    ef = ResilientEmbeddingFunction(primary=[p1], fallback=fallback)

    ef(["a"])
    ef(["b"])

    assert ef.primary_calls == 2
    assert ef.fallback_calls == 0
    assert len(fallback.calls) == 0


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


# ----------------------------------------------------------------------
# #581: 연속 실패 백오프, 호출당 실패 시도 시간 예산, 예산에 밀린 primary 회전.
# 가상 시계만 쓴다 — 실제 대기와 네트워크가 없다. 설계: docs/issue-581-embed-backoff.md
# ----------------------------------------------------------------------


class _VClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Ledger:
    """호출 하나 동안 실패한 원격 시도가 쓴 가상 시간의 합."""

    def __init__(self) -> None:
        self.failed = 0.0


class _TimedEF:
    """호출하면 alive 이면 즉시 성공하고, 아니면 가상 시간을 cost 만큼 쓰고 TimeoutError."""

    def __init__(
        self, clock: _VClock, ledger: _Ledger, label: str, *, cost: float = 8.0, alive: bool = False
    ) -> None:
        self.clock = clock
        self.ledger = ledger
        self.api_base = f"http://{label}/v1"
        self.cost = cost
        self.alive = alive
        self.attempts: list[float] = []  # 시도 시작 시각
        self.fail_ends: list[float] = []  # 실패 종료 시각
        self.successes = 0

    def __call__(self, input: list[str]) -> list[list[float]]:
        self.attempts.append(self.clock())
        if self.alive:
            self.successes += 1
            return [[1.0, 0.0] for _ in input]
        self.clock.advance(self.cost)
        self.ledger.failed += self.cost
        self.fail_ends.append(self.clock())
        raise TimeoutError("timeout")

    def name(self) -> str:
        return "kure_v1"

    def ping(self) -> bool:
        return self.alive


class _TimedFallback:
    def __init__(self, clock: _VClock, cost: float = 0.7) -> None:
        self.clock = clock
        self.cost = cost
        self.calls = 0

    def __call__(self, input: list[str]) -> list[list[float]]:
        self.calls += 1
        self.clock.advance(self.cost)
        return [[0.0, 1.0] for _ in input]

    def name(self) -> str:
        return "kure_v1_gguf"


def _build(
    n: int,
    *,
    cost: float = 8.0,
    alive: tuple[int, ...] = (),
    budget: float | None = 16.0,
    fb_cost: float = 0.7,
    **kw: float,
) -> tuple[ResilientEmbeddingFunction, _VClock, _Ledger, list[_TimedEF], _TimedFallback]:
    clk, led = _VClock(), _Ledger()
    prims = [_TimedEF(clk, led, f"p{i}", cost=cost, alive=i in alive) for i in range(n)]
    fb = _TimedFallback(clk, fb_cost)
    ef = ResilientEmbeddingFunction(
        primary=list(prims), fallback=fb, attempt_budget=budget, clock=clk, **kw
    )
    return ef, clk, led, prims, fb


def _call(ef: ResilientEmbeddingFunction, clk: _VClock, led: _Ledger, gap: float) -> float:
    """호출 하나를 하고 그 호출의 원격 실패 시간을 돌려준다. 이어서 gap 만큼 기다린다."""
    led.failed = 0.0
    ef(["x"])
    failed = led.failed
    clk.advance(gap)
    return failed


def _drive_until(
    ef: ResilientEmbeddingFunction,
    clk: _VClock,
    led: _Ledger,
    cond,
    *,
    gap: float = 1.0,
    limit: int = 5000,
) -> int:
    for n in range(limit):
        if cond():
            return n
        _call(ef, clk, led, gap=gap)
    raise AssertionError("조건에 도달하지 못했다")


def test_dead_primaries_call_cost_bounded_and_retry_backs_off() -> None:
    ef, clk, led, prims, _ = _build(2)
    t_start = clk()
    costs: list[float] = []
    while clk() < t_start + 600:
        costs.append(_call(ef, clk, led, gap=1.0))

    # 호출당 실패 시도 시간: 예산 + 시도 하나(T) 미만.
    assert max(costs) < 16.0 + 8.0
    # 같은 primary 의 연속 시도 간격이 k번째에 min(15*2**(k-1), 120) 이상이다.
    for p in prims:
        starts = p.attempts
        assert len(starts) >= 3
        for k, (a, b) in enumerate(zip(starts, starts[1:], strict=False), start=1):
            assert b - a >= min(15.0 * 2 ** (k - 1), 120.0)
    # 대조군: 백오프를 끈 인스턴스(max_backoff == health_ttl)보다 시도가 적다.
    ef0, clk0, led0, prims0, _ = _build(2, max_backoff=15.0)
    t0 = clk0()
    while clk0() < t0 + 600:
        _call(ef0, clk0, led0, gap=1.0)
    assert len(prims[0].attempts) < len(prims0[0].attempts)


def test_three_dead_primaries_call_cost_bounded_by_budget() -> None:
    ef, clk, led, prims, _ = _build(3)
    t_start = clk()
    costs: list[float] = []
    while clk() < t_start + 600:
        costs.append(_call(ef, clk, led, gap=1.0))
    # 첫 호출: 한 번도 실패하지 않은 primary 는 예산과 무관하다 → 세 시간 초과를 전부 치른다.
    assert costs[0] == 24.0
    # 그 뒤 세 primary 는 재시도 대상이다: 호출당 실패 시간이 예산 + T 미만이다.
    assert max(costs[1:]) < 16.0 + 8.0
    assert all(len(p.attempts) >= 2 for p in prims)  # 예산에 밀린 셋째도 재시도된다

    # 대조군: 예산이 없으면 첫 호출 뒤에도 세 시간 초과를 전부 치르는 호출이 있다.
    ef0, clk0, led0, _, _ = _build(3, budget=None)
    t0 = clk0()
    costs0: list[float] = []
    while clk0() < t0 + 600:
        costs0.append(_call(ef0, clk0, led0, gap=1.0))
    assert max(costs0[1:]) >= 24.0


@pytest.mark.parametrize(
    ("cost", "gap", "fb_cost"),
    [(8.0, 121.0, 0.7), (8.0, 1.0, 121.0), (17.0, 121.0, 0.7)],
    ids=["sparse-calls", "slow-fallback", "attempt-longer-than-budget"],
)
def test_recovered_third_retry_target_is_not_starved_by_dead_ones(
    cost: float, gap: float, fb_cost: float
) -> None:
    ef, clk, led, prims, _ = _build(3, cost=cost, fb_cost=fb_cost)
    _call(ef, clk, led, gap=gap)  # 셋 다 한 번 실패한다(이후 모두 재시도 대상)
    prims[2].alive = True  # 셋째만 복구
    served: list[int] = []
    total = 40
    for idx in range(total):
        before = prims[2].successes
        _call(ef, clk, led, gap=gap)
        if prims[2].successes > before:
            served.append(idx)
    n = 3
    assert served, "셋째 primary 가 한 번도 처리하지 못했다"
    assert served[0] < n
    assert all(b - a <= n for a, b in zip(served, served[1:], strict=False))
    assert total - 1 - served[-1] < n


def test_skipped_retry_target_keeps_its_turn_even_when_a_later_primary_succeeds() -> None:
    # A, B 는 실패에 17초(예산 16 이상), C 는 처음부터 정상이다.
    ef, clk, led, prims, _ = _build(3, cost=17.0, alive=(2,))
    _call(ef, clk, led, gap=121.0)  # A, B 가 한 번씩 실패하고 C 가 처리한다
    assert [len(p.attempts) for p in prims] == [1, 1, 1]
    prims[1].alive = True  # B 복구
    served: list[int] = []
    for idx in range(30):
        before = prims[1].successes
        _call(ef, clk, led, gap=121.0)
        if prims[1].successes > before:
            served.append(idx)
    assert served and served[0] < 3
    assert all(b - a <= 3 for a, b in zip(served, served[1:], strict=False))


@pytest.mark.parametrize(
    "scenario",
    [
        "dead-pair-then-alive",
        "attempt-longer-than-budget",
        "bulk-call-dies-midway",
        "skipped-retry-target-before-fresh-primary",
    ],
)
def test_alive_primary_that_never_failed_is_not_skipped_by_budget(scenario: str) -> None:
    if scenario == "dead-pair-then-alive":
        ef, clk, led, prims, fb = _build(3, alive=(2,))
        t_end = clk() + 300
        while clk() < t_end:
            _call(ef, clk, led, gap=1.0)
        assert fb.calls == 0
        assert prims[2].successes == ef.primary_calls
    elif scenario == "attempt-longer-than-budget":
        ef, clk, led, prims, fb = _build(2, cost=17.0, alive=(1,))
        _call(ef, clk, led, gap=1.0)
        assert fb.calls == 0
        assert prims[1].successes == 1
    elif scenario == "skipped-retry-target-before-fresh-primary":
        # 재시도 대상 A, B 뒤에 한 번도 실패하지 않은 C: B 는 예산으로 건너뛰어지고 C 가 처리한다.
        ef, clk, led, prims, fb = _build(3, cost=17.0, alive=(2,))
        for _ in range(6):
            _call(ef, clk, led, gap=121.0)
        assert fb.calls == 0
        assert prims[2].successes == 6
        assert len(prims[0].attempts) > 1  # A 는 재시도 대상으로 계속 시도된다
    else:
        # 라이브형 primary 2개: 첫 primary 가 20초 뒤(여러 배치를 처리한 뒤) 실패한다.
        ef, clk, led, prims, fb = _build(2, cost=20.0, alive=(1,))
        _call(ef, clk, led, gap=1.0)
        assert fb.calls == 0
        assert prims[1].successes == 1


def test_recovered_primary_is_reused_after_backoff_expires() -> None:
    ef, clk, led, prims, _ = _build(1)
    p = prims[0]
    _drive_until(ef, clk, led, lambda: len(p.fail_ends) >= 8)
    p.alive = True  # 복구
    expiry = p.fail_ends[-1] + 120.0  # 마지막 실패 종료 + max_backoff
    _drive_until(ef, clk, led, lambda: p.successes >= 1)
    first_success = p.attempts[-1]
    assert first_success >= p.fail_ends[-1] + 15.0  # 만료 전에는 쓰이지 않는다
    assert first_success <= expiry + 2.0  # 만료 뒤 첫 호출(호출 주기 안)


def test_recovered_last_of_many_instantly_failing_primaries_is_reused() -> None:
    ef, clk, led, prims, _ = _build(200, cost=0.0)
    _call(ef, clk, led, gap=1.0)  # 200개가 모두 즉시 실패한다
    assert all(p.attempts for p in prims)
    last = prims[-1]
    last.alive = True
    _drive_until(ef, clk, led, lambda: last.successes >= 1)
    # 즉시 실패는 예산을 쓰지 않는다: 만료(첫 실패 15초) 뒤 첫 호출에서 쓰인다.
    assert last.attempts[-1] - last.attempts[0] <= 15.0 + 2.0


def test_success_resets_failure_count_so_next_backoff_starts_at_health_ttl() -> None:
    ef, clk, led, prims, _ = _build(1)
    p = prims[0]
    _drive_until(ef, clk, led, lambda: len(p.fail_ends) >= 8)
    p.alive = True
    _drive_until(ef, clk, led, lambda: p.successes >= 1)
    p.alive = False
    _call(ef, clk, led, gap=1.0)  # 복구 뒤 첫 실패
    fail_end = p.fail_ends[-1]
    n_before = len(p.attempts)
    _drive_until(ef, clk, led, lambda: len(p.attempts) > n_before)
    assert 15.0 <= p.attempts[-1] - fail_end < 30.0  # 15초 간격(30초 이상으로 이어지지 않는다)


def test_recovered_after_many_slow_dead_primaries_is_reused_within_n_calls() -> None:
    n = 20
    ef, clk, led, prims, _ = _build(n)
    last = prims[-1]
    _drive_until(ef, clk, led, lambda: len(last.fail_ends) >= 4)
    last.alive = True
    expiry = last.fail_ends[-1] + 120.0
    ordinal = 0
    for _ in range(5000):
        if clk() >= expiry:
            ordinal += 1
        before = last.successes
        _call(ef, clk, led, gap=1.0)
        if last.successes > before:
            break
    else:
        raise AssertionError("복구한 primary 가 다시 쓰이지 않았다")
    assert ordinal <= n


def test_budget_skip_makes_unattempted_primary_first_in_next_call() -> None:
    ef, clk, led, prims, _ = _build(3)
    _call(ef, clk, led, gap=121.0)  # 셋 다 한 번 실패한다(한 번도 실패하지 않은 primary 는 예산과 무관)
    assert [len(p.attempts) for p in prims] == [1, 1, 1]
    _call(ef, clk, led, gap=121.0)  # p0, p1 이 재시도돼 예산을 쓰고 p2 는 건너뛰어진다
    assert [len(p.attempts) for p in prims] == [2, 2, 1]
    _call(ef, clk, led, gap=121.0)  # 백오프가 모두 만료됐어도 p2 가 맨 앞이다
    assert len(prims[2].attempts) == 2
    assert prims[2].attempts[1] < prims[0].attempts[2]


def test_one_dead_primary_never_causes_fallback_when_another_is_alive() -> None:
    ef, clk, led, prims, fb = _build(2, alive=(1,))
    t_start = clk()
    while clk() < t_start + 600:
        _call(ef, clk, led, gap=1.0)
    assert fb.calls == 0
    assert ef.fallback_calls == 0
    assert prims[1].successes == ef.primary_calls


def test_instant_failures_do_not_consume_budget() -> None:
    ef, clk, led, prims, fb = _build(5, cost=0.0, budget=1.0)
    _call(ef, clk, led, gap=1.0)
    assert [len(p.attempts) for p in prims] == [1] * 5
    assert fb.calls == 1


def test_force_check_resets_consecutive_failures() -> None:
    ef, clk, led, prims, _ = _build(1)
    p = prims[0]
    _drive_until(ef, clk, led, lambda: len(p.fail_ends) >= 6)
    p.alive = True  # ping 이 성공하지만 아직 호출은 실패하는 경계: 복구 직후
    assert ef.force_check() is True
    p.alive = False
    _drive_until(ef, clk, led, lambda: len(p.fail_ends) >= 7)
    # force_check 직후 첫 실패의 건너뜀은 health_ttl(15초)이다.
    assert ef._unhealthy_until[0] - p.fail_ends[-1] == 15.0


def test_without_budget_every_eligible_primary_is_attempted() -> None:
    ef, clk, led, prims, _ = _build(4, budget=None)
    assert _call(ef, clk, led, gap=1.0) == 32.0
    assert [len(p.attempts) for p in prims] == [1] * 4


def test_attempt_budget_must_be_positive() -> None:
    fb = _MockFallback()
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            ResilientEmbeddingFunction(primary=[_MockEF("p")], fallback=fb, attempt_budget=bad)


def test_health_ttl_zero_retries_every_call_and_huge_failure_count_is_safe() -> None:
    ef, clk, led, prims, _ = _build(1, budget=None, health_ttl=0.0)
    for _ in range(5):
        _call(ef, clk, led, gap=0.0)
    assert len(prims[0].attempts) == 5

    ef2, clk2, led2, prims2, _ = _build(1, budget=None, cost=0.0)
    for _ in range(3000):
        _call(ef2, clk2, led2, gap=200.0)  # 매번 백오프가 만료된다
    assert len(prims2[0].attempts) == 3000
    assert ef2._unhealthy_until[0] - clk2() <= 120.0  # 간격은 상한을 넘지 않는다


def test_factory_sets_attempt_budget_from_openai_timeout() -> None:
    from opencrab.stores.factory import _make_kure_embedding_function

    settings = Settings(OPENAI_API_BASE="http://a:1234/v1", OPENAI_TIMEOUT="5")
    assert _make_kure_embedding_function(settings)._attempt_budget == 10.0
    settings0 = Settings(OPENAI_API_BASE="http://a:1234/v1", OPENAI_TIMEOUT="0")
    assert _make_kure_embedding_function(settings0)._attempt_budget is None


def test_first_attempt_of_a_call_ignores_budget_even_if_time_passes_before_it() -> None:
    # 시계를 읽을 때마다 가상 시간이 흐른다(잠금 대기와 스케줄링 지연의 모사).
    # 재시도 대상의 호출 첫 시도는 예산이 지연보다 작아도 일어나야 한다.
    class _TickClock(_VClock):
        def __call__(self) -> float:
            self.t += 0.01
            return self.t

    clk, led = _TickClock(), _Ledger()
    prim = _TimedEF(clk, led, "p0")
    fb = _TimedFallback(clk)
    ef = ResilientEmbeddingFunction(
        primary=[prim], fallback=fb, attempt_budget=0.001, clock=clk
    )
    ef(["x"])  # 한 번 실패한다 → 재시도 대상
    assert fb.calls == 1
    clk.t += 100.0  # 백오프 만료
    prim.alive = True
    ef(["x"])
    assert prim.successes == 1
    assert fb.calls == 1


def test_fallback_log_names_budget_exhaustion_only_when_budget_cut_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger_name = "opencrab.stores.resilient_embedding"

    def fallback_reasons() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == logger_name and r.getMessage().startswith("임베딩 폴백")
        ]

    ef, clk, led, prims, _ = _build(3)
    # 셋 다 처음 실패한 호출: 예산이 아니라 모든 primary 실패가 폴백 사유다.
    with caplog.at_level(logging.INFO, logger=logger_name):
        _call(ef, clk, led, gap=121.0)
    reasons = fallback_reasons()
    assert len(reasons) == 1
    assert "모든 primary 장애" in reasons[0]
    assert "예산" not in reasons[0]

    # 재시도 대상 둘이 예산을 쓰고 셋째가 건너뛰어진 호출.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger_name):
        _call(ef, clk, led, gap=121.0)
    assert [len(p.attempts) for p in prims] == [2, 2, 1]
    reasons = fallback_reasons()
    assert len(reasons) == 1
    assert "예산" in reasons[0]
    assert "모든 primary 장애" not in reasons[0]

    # 모든 primary 가 백오프 중인 호출은 종전 문구다.
    ef2, clk2, led2, _, _ = _build(2)
    _call(ef2, clk2, led2, gap=1.0)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger_name):
        _call(ef2, clk2, led2, gap=1.0)
    reasons = fallback_reasons()
    assert len(reasons) == 1
    assert "모든 primary 장애" in reasons[0]
