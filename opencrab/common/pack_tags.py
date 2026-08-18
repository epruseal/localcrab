"""팩 소유 태그(`pack_id`)를 찍고 폐기 별칭(`pack`)을 다루는 단일 자리.

`properties.pack` 은 `properties.pack_id` 의 **사본**이었다 — 생산자
(`pack.normalize.transform_node`)가 언제나 같은 값으로 썼고, 읽는 자리는
노드 벡터 메타의 `source` 를 만드는 `ontology.builder.add_node` 하나뿐이었다.
그런데 `pack_id` 만 덮고 `pack` 은 보존하는 writer 들이 있어 한 행이 서로 다른
두 소유 태그를 갖는 상태가 만들어질 수 있었고, 그 행이 builder 를 지나면 벡터
`source` 가 **옛 이름**으로 찍혔다(#159, #171).

여기서 축 자체를 없앤다. `pack_id` 가 유일한 소유 키다.

두 함수의 역할이 다르다 — 섞지 마라.

* :func:`apply_pack_tag` — **팩 권위 writer** 전용. `pack_id` 가 구성상 권위인
  자리(지금 적재 중인 팩)에서 쓴다. 입력에 실려 온 `pack` 은 정규화 대상인 낡은
  값이므로 조용히 버리고, 값이 달랐을 때만 그 값을 돌려준다. 여기서 예외를
  던지면 `transform_node` 의 순수 결정적 변환 계약이 깨지고 레거시 팩의 노드가
  로더의 skip 으로 유실된다.
* :func:`canonicalize_pack_alias` — **범용 write funnel** 전용 불변식. 소유
  권위가 없는 자리(임의 properties/metadata 를 받는 진입점)에서 쓴다. 두 태그가
  다르면 어느 쪽이 소유인지 판정할 수 없는 호출자 오류이므로 거부한다.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

LEGACY_PACK_ALIAS_KEY = "pack"

# 폐기된 키. 라이브 행에는 남아 있지만 새 변환은 만들지 않는다. 증분 대조에서
# 빼지 않으면 전 행이 매 런 chg 로 잡히고, neo4j 는 전달된 키만 SET 하므로
# 재기록해도 이 키가 사라지지 않아 그 재기록이 영구히 반복된다.
RETIRED_KEYS = frozenset({LEGACY_PACK_ALIAS_KEY})


def apply_pack_tag(tags: MutableMapping[str, Any], pack_id: str) -> str | None:
    """``tags`` 에 소유 태그를 찍고 폐기 별칭을 버린다(제자리 변경).

    Returns
    -------
    버려진 별칭 값이 ``pack_id`` 와 **달랐을 때** 그 값, 아니면 ``None``.
    알릴지 말지는 호출자 정책이다 — 이 함수는 판단하지 않는다.
    """
    tags["pack_id"] = pack_id
    dropped = tags.pop(LEGACY_PACK_ALIAS_KEY, None)
    if dropped is None or dropped == pack_id:
        return None
    return str(dropped)


def canonicalize_pack_alias(tags: MutableMapping[str, Any]) -> None:
    """범용 write funnel 의 소유 태그 불변식(제자리 변경).

    한 행이 ``pack`` 과 truthy ``pack_id`` 를 동시에 가지면서 값이 다를 수 없다.

    - 값이 다르면 :class:`ValueError`. 이 funnel 에는 소유 권위가 없으므로
      어느 쪽이 참인지 고를 수 없다 — 조용히 한쪽을 버리면 그것이 #171 이다.
    - 값이 같으면 중복 별칭을 버린다.
    - ``pack`` 만 있고 ``pack_id`` 가 없으면 **건드리지 않는다.** 모순이 아니고,
      임의 속성을 그대로 저장한다는 진입점 계약을 깰 이유가 없다. 소비자가 0이라
      무해하며, 나중에 ``pack_id`` 가 붙는 순간 위 규칙이 잡는다.
    """
    if LEGACY_PACK_ALIAS_KEY not in tags:
        return
    pack_id = tags.get("pack_id")
    if not pack_id:
        return
    alias = tags[LEGACY_PACK_ALIAS_KEY]
    if alias != pack_id:
        raise ValueError(
            f"properties.{LEGACY_PACK_ALIAS_KEY} is a retired alias of pack_id and "
            f"cannot disagree with it (got {alias!r} vs pack_id {pack_id!r}); "
            "drop it and set pack_id alone"
        )
    del tags[LEGACY_PACK_ALIAS_KEY]


def strip_retired_keys(tags: Mapping[str, Any]) -> dict[str, Any]:
    """폐기 키를 뺀 사본. 증분 대조가 라이브 쪽에 적용한다."""
    return {k: v for k, v in tags.items() if k not in RETIRED_KEYS}
