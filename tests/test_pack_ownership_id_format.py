"""``opencrab.pack.ownership.validate_pack_id_format`` 단위 테스트 (#180).

이 파일은 순수 함수 하나만 대상으로 한다 -- 레지스트리/그래프 어느 것도 건드리지
않는다(``validate_pack_id_format`` 자체가 어떤 조회도 하지 않는다는 설계 요구
2를 그대로 반영). ``PACK_ID_COLUMN_LIMIT``/``PACK_ID_BUDGET`` 은
``opencrab.pack.fork_remap.NODE_ID_COLUMN_LIMIT`` (256) 에서 파생된 값이며,
``opencrab.pack.fork``의 기존 ``_PACK_ID_COLUMN_LIMIT``/``_PACK_ID_BUDGET`` 과
동일한 값이어야 한다(정의 위치만 옮겨졌을 뿐 파생식은 바뀌지 않았다 -- #180
design v2 §3).
"""

from __future__ import annotations

import pytest

from opencrab.pack.ownership import (
    PACK_ID_BUDGET,
    PACK_ID_COLUMN_LIMIT,
    PACK_ID_RE,
    validate_pack_id_format,
)


def test_column_limit_and_budget_match_fork_derivation():
    from opencrab.pack.fork import _PACK_ID_BUDGET, _PACK_ID_COLUMN_LIMIT

    assert PACK_ID_COLUMN_LIMIT == _PACK_ID_COLUMN_LIMIT == 256
    assert PACK_ID_BUDGET == _PACK_ID_BUDGET == 239


@pytest.mark.parametrize(
    "pack_id",
    [
        "coffee",
        "coffee-shop",
        "coffee_shop",
        "coffee.v2",
        "a",
        "A1",
        "9start-with-digit",
    ],
)
def test_valid_pack_id_accepted(pack_id):
    assert validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET) is None


@pytest.mark.parametrize(
    "pack_id",
    [
        "coffee/shop",
        "coffee shop",
        "coffee\tshop",
        "카페",
        ".coffee",
        "-coffee",
        "coffee..shop",
        "..",
        "",
    ],
)
def test_malformed_pack_id_rejected(pack_id):
    reason = validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET)
    assert reason is not None
    assert "invalid pack_id" in reason


def test_length_at_exactly_max_len_is_accepted():
    pack_id = "a" * PACK_ID_BUDGET
    assert validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET) is None


def test_length_one_over_max_len_is_rejected():
    pack_id = "a" * (PACK_ID_BUDGET + 1)
    reason = validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET)
    assert reason is not None
    assert f"{PACK_ID_BUDGET}-character" in reason


def test_over_length_noun_default_is_limit():
    pack_id = "a" * (PACK_ID_BUDGET + 1)
    reason = validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET)
    assert "limit" in reason
    assert "budget" not in reason


def test_over_length_noun_can_be_overridden():
    pack_id = "a" * (PACK_ID_BUDGET + 1)
    reason = validate_pack_id_format(pack_id, max_len=PACK_ID_BUDGET, over_length_noun="budget")
    assert "budget" in reason
    assert "limit" not in reason


def test_pack_id_re_object_is_shared_with_assembler():
    """assembler.py 는 이 정규식 객체를 재수출해 재사용한다(#180 design §2) --
    로컬 재정의가 아니라 같은 객체인지까지 확인한다."""
    from opencrab.pack.assembler import _PACK_ID_RE

    assert _PACK_ID_RE is PACK_ID_RE
