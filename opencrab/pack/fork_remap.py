"""`pack_fork`(#201)의 결정적 id 재매핑 — 순수 함수만, I/O·스토어 접근 없음.

**이 모듈은 fork용 정체성 변환 계층이다.** 그래프와 벡터의 콘텐츠 id는 전역
유일하므로 fork는 원본과 같은 id로 콘텐츠를 복사할 수 없다. 같은 id를 쓰면
그래프의 global-key upsert와 벡터의 global slot이 원본을 덮거나 충돌한다. 그래서
fork 호출마다 하나의 salt로 모든 콘텐츠 id를 결정적으로 재매핑한다. 앵커도 원본과
다른 노드로 생성되므로 매핑에 포함하되 복사 대상에서는 제외한다.

스토어에 쓰기 전 순수 변환 단계에서만 쓰인다. 여기 있는 어떤 함수도 DB·그래프·
벡터 스토어를 만지지 않는다 — 그래야 `pack_fork` 오케스트레이터(`fork.py`)가
검증(§5-1 preflight)과 부작용(§5-3 쓰기)을 분리해 부를 수 있고, 이 모듈 자체는
스토어 없이 단위 테스트할 수 있다.

## 재매핑 규칙 (설계 §4-A, 정본)

1. 콘텐츠 id(`node_id`/`source_id`)는 `{old}{REMAP_SEP}{salt}` 로 재매핑한다.
   salt 는 fork 호출당 하나(:func:`new_salt`). pack_id 를 접미사로 쓰지 않는다 —
   salt 는 고정 12-hex 라 `packs.pack_id VARCHAR(256)` 길이 예산 문제를 애초에
   만들지 않는다.

2. **원본 앵커는 복사하지 않는다.** :func:`build_mapping` 이 넣는
   `mapping[src_anchor] = dst_anchor` 항목은 **엣지 끝점 재지정 전용**이다.
   앵커 노드 자체와 그 벡터는 앵커 생성 경로(`add_node(..., pack_anchor=True)`)가
   새로 만든다. **호출자가 이 매핑 항목을 "앵커를 복사하라"는 뜻으로 읽고
   `mapping[src_anchor]` 를 새 노드 id 로 써서 앵커를 한 번 더 쓰면, 벡터
   임포트에서 같은 id 로 두 번째 ADD 가 걸려 배치 전체가 거부된다**(chroma 는
   배치 거부, sqlite-vec 은 트랜잭션 롤백, pgvector 는 IntegrityError) — 새
   앵커는 제목·설명이 달라 원본 앵커 벡터를 재사용하는 것도 의미상 틀리다.

3. 참조 키 재작성(H3). 순회 도메인은 **최상위 scalar `str` 값**으로 못박는다 —
   재작성 규칙과 사후 검증 술어가 같은 도메인을 봐야 하기 때문이다(그렇지 않으면
   `metadata["refs"] = {"node_id": old}` 같은 중첩 참조를 규칙은 못 고치고
   사후 검증만 잡아, 원본 데이터 형상이 영구 `partial` 을 유발한다).
   `REFERENCE_KEYS` 각각에 대해 그 값이 `str` 일 때만 순서대로:
   - 매핑의 키에 있으면 매핑값으로 치환
   - `src_pack` 과 같으면 `dst_pack` 으로 치환(`source` 는 빌더가 pack_id 를
     넣는 자리다)
   - 둘 다 아니면 그대로 두고 **unverified 로 집계**(원본에서 이미 팩 밖을
     가리키던 참조를 새로 만들어 낼 근거가 없다)

   **앞의 두 갈래가 서로소라는 것은 호출자가 세우는 전제다**(설계 §14). 콘텐츠
   id 하나가 `src_pack` 문자열과 같으면 그것이 매핑 키가 되어 두 갈래가 같은
   값에 걸리고, 그때는 어느 순서도 옳지 않다 — 첫 갈래를 앞에 두면 pack 태그
   자리가 재매핑 id 를 받고, 뒤에 두면 그 노드를 가리키던 진짜 참조가 팩 id 를
   받는다. 사후 검증(H4)은 둘 중 어느 쪽도 못 본다(쓰인 값이 매핑의 **값**이라
   "매핑 키가 남았다"에도 "`src_pack` 이 남았다"에도 안 걸린다). 그래서
   `pack_fork` 의 preflight 가 그런 팩을 아예 거부하고(§5-1 step 6c), 목적지
   쪽 짝은 예약 직후에 본다(step 10). 그 두 가드가 이 규칙의 전제조건이다.
   값이 `dict`/`list` 면 재귀하지 않고 그대로 두되 **똑같이 unverified 로
   집계**한다(pgvector 는 중첩 JSON metadata 를 그대로 보존하므로 실제로 도달
   가능한 형상이다). 합성 문자열(`"node:" + old_id`)은 값 전체가 매핑 키와
   같지 않으므로 세 번째 갈래(unverified)로 떨어진다 — 부분 문자열 치환은
   무관한 값을 훼손할 수 있어(흔한 접두어면 오탐이 확실하다) 하지 않는다.

4. **`pack_id` 는 재작성한다.** `validate_import_records`(#200)는
   "absent/equal → assign, present and different → reject" 이고
   `export_pack_vectors` 는 모든 record 에 `pack_id=src` 를 달아 내보내므로,
   재작성하지 않으면 record 0 에서 전량 거부된다. `remap_vector_metadata` 가
   무조건 `dst_pack` 으로 덮는다(원본 값을 보고 분기하지 않는다 — 잘못 태깅된
   record 를 거를지 말지는 오케스트레이터의 preflight 분류(§5-1-6b,
   `skipped.vector_mistagged`) 몫이고, 이 함수는 그 판정이 끝난 record 만
   받는다는 전제로 순수 변환만 한다).

5. **소유 키 재작성.** `OWNER_KEYS`(`user_id`/`owner_id`)가 metadata 에 있으면
   forker 의 `owner_id` 로 치환한다. `write_source` 가 스탬프한 `user_id` 가
   벡터 metadata 까지 실려 가므로, 재작성하지 않으면 사본 벡터가 원 소유자
   id 를 영구 보유한다. **사후 검증 술어(H4)는 이 결함을 잡지 못한다** —
   owner 값은 매핑의 키가 아니므로 "매핑 키가 안 남아 있다"는 검사로는 안
   보인다. 이 규칙을 빠뜨려도 죽는 유일한 안전장치가 `tests/test_fork_remap.py`
   의 역-변이 테스트다.

6. **구조적 엣지 끝점도 재매핑 대상이다.** `add_edge` 는 `from_id`/`to_id` 를
   properties 가 아니라 별도 구조 필드로 받는다. properties 만 훑는
   :func:`remap_props` 는 이 둘을 보지 못하므로, 호출자(`fork.py`)가
   `build_mapping` 이 돌려준 매핑 dict 를 `from_id`/`to_id` 에 **직접**
   적용해야 한다(별도 헬퍼 없음 — 매핑은 노드 id 공간 전체를 이미 담고 있다).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from typing import Any

# 12 hex 문자(6 bytes). 콘텐츠 id 재매핑 접미사로 쓰기에 충분히 짧아 pack_id 를
# salt 로 쓸 때 생기는 VARCHAR(256) 길이 예산 문제를 만들지 않는다(§3, 코멘트 7항).
FORK_SALT_BYTES = 6

# 원본 id 와 salt 를 잇는 구분자. `~` 는 pack_id/node_id 어느 쪽 alphabet 에도
# 관례적으로 쓰이지 않아 재매핑된 id 와 원본 id 를 눈으로도 구분할 수 있다.
REMAP_SEP = "~"

# 레지스트리(`ontology_nodes`)의 `node_id` 열은 VARCHAR(256) 이다. 이 상수는
# fork.py 의 노드 길이 검사와 source_writer.py 의 노드화 예산이 같은 근거를
# 쓰도록 여기 한 곳에 둔다 — 두 곳에 256 을 따로 적으면 언젠가 갈린다.
NODE_ID_COLUMN_LIMIT = 256

# 소스를 그래프 노드로 만들 수 있는 id 길이의 상한(issue #74). fork 는 콘텐츠
# id 에 `{REMAP_SEP}{salt}` 를 붙이므로, 지금 딱 맞는 id 도 한 번 fork 하면
# 열을 넘긴다. 그래서 상한은 열 크기가 아니라 **재매핑 뒤에도 들어가는 크기**다.
#
# 이 예산을 넘는 source_id 에는 노드를 새로 만들지 않는다. source_id 는 doc
# 스토어의 text 열과 벡터의 TEXT id 에 닿을 뿐 길이 제약이 없고(그래서
# tests/test_pack_fork.py 의 T77 이 "장문 source-only id 를 가진 팩의 fork 는
# 거절되면 안 된다"를 계약으로 고정한다), 무조건 노드로 만들면 그 계약이
# 깨진다. 상수 대신 `remap_id` 의 실제 형상에서 파생시켜, 재매핑 모양이 바뀌면
# 이 경계도 함께 움직이게 한다.
SOURCE_NODE_ID_BUDGET = NODE_ID_COLUMN_LIMIT - len(REMAP_SEP) - FORK_SALT_BYTES * 2

# 규칙 3 의 순회 도메인(정본). node/edge properties, doc 소스 행, 벡터 metadata
# 전부에서 같은 다섯 키만 본다 — 이 밖의 키(`parent_id` 등)는 설계상 보장 범위
# 밖이고 unverified 로만 집계된다(§4-A "보장 범위의 명시적 한계").
REFERENCE_KEYS = ("node_id", "source_id", "document_id", "source", "id")

# 규칙 5 의 소유 키. 벡터 metadata 에서만 재작성한다 — 그래프/doc 소유권은
# `write_gate.stamp()` 가 쓰기 시점에 이미 forker 값으로 찍으므로 여기서
# 다시 만질 필요가 없다(source/node properties 는 이 목록의 대상이 아니다).
OWNER_KEYS = ("user_id", "owner_id")


def new_salt() -> str:
    """fork 호출 하나당 salt 하나. 12-hex, 암호학적으로 예측 불가능하다.

    salt 를 예측 가능하게 하면(예: 순번) 동시에 도는 다른 fork 와 재매핑 id 가
    충돌할 여지가 생긴다 — §6-1 의 "다른 writer 가 같은 id 를 고를 확률은
    사실상 0" 이라는 위험 평가가 `secrets` 를 전제로 한다.
    """
    return secrets.token_hex(FORK_SALT_BYTES)


def remap_id(old: str, salt: str) -> str:
    """콘텐츠 id 재매핑(규칙 1). 결정적 — 같은 `(old, salt)` 는 항상 같은 값."""
    return f"{old}{REMAP_SEP}{salt}"


def build_mapping(
    node_ids: Iterable[str],
    source_ids: Iterable[str],
    *,
    salt: str,
    src_anchor: str,
    dst_anchor: str,
) -> dict[str, str]:
    """노드 id·소스 id·앵커를 아우르는 재매핑 정본 dict 를 만든다.

    `node_ids`/`source_ids` 의 모든 id 는 `remap_id(id, salt)` 로 매핑된다.
    그 다음 `mapping[src_anchor] = dst_anchor` 를 **덮어쓴다** — `node_ids` 에
    앵커 id 가 섞여 들어와도(export 는 앵커를 보통 노드로 함께 돌려준다) 앵커
    항목은 항상 salt 붙은 값이 아니라 `dst_anchor` 를 직통으로 가리키게 한다.

    반환된 매핑은 **엣지 구조 끝점(`from_id`/`to_id`)에도 그대로 쓴다**(규칙 6).
    노드 id 공간을 전부 담고 있으므로 별도 헬퍼가 필요 없다 — 호출자는
    `mapping[old_from_id]`/`mapping[old_to_id]` 로 직접 조회한다.

    **주의(규칙 2): 이 매핑의 `src_anchor` 항목은 엣지 끝점 재지정 전용이다.**
    이 값으로 앵커 노드 자체를 "복사"하면 앵커 쓰기 경로가 이미 같은 id 를
    점유한 뒤라 벡터 임포트가 ADD 충돌로 배치 전체 실패한다. 원본 앵커는
    복사 대상에서 아예 빼고(오케스트레이터의 §5-1 몫), 새 앵커는 앵커 생성
    경로가 만든다.
    """
    mapping = {node_id: remap_id(node_id, salt) for node_id in node_ids}
    mapping.update({source_id: remap_id(source_id, salt) for source_id in source_ids})
    mapping[src_anchor] = dst_anchor
    return mapping


def _remap_reference_keys(
    props: Mapping[str, Any],
    mapping: Mapping[str, str],
    *,
    src_pack: str,
    dst_pack: str,
) -> tuple[dict[str, Any], int]:
    """규칙 3 을 `REFERENCE_KEYS` 최상위 scalar `str` 자리에만 적용한다.

    `remap_props`/`remap_vector_metadata` 공용 로직. 두 함수의 차이(pack_id
    무조건 재작성, 소유 키 재작성)는 여기 넣지 않는다 — 그건 벡터 metadata
    에만 있는 규칙 4·5 고, 이 함수는 두 함수 모두에 공통인 규칙 3 만 담당한다.

    입력을 제자리 수정하지 않는다 — 항상 얕은 사본을 새로 만들어 반환한다.
    """
    new_props = dict(props)
    unverified = 0
    for key in REFERENCE_KEYS:
        if key not in new_props:
            continue
        value = new_props[key]
        if isinstance(value, dict | list):
            # 중첩 구조는 규칙 3 의 도메인 밖이다(최상위 scalar 만). 참조
            # 판정 스키마가 없어 재귀하면 무관한 값을 훼손할 위험이 있다 —
            # 그대로 두고 unverified 로만 집계한다(§4-A 보장 범위 한계).
            unverified += 1
            continue
        if not isinstance(value, str):
            # str 도 dict/list 도 아닌 값(int/bool/None 등)은 REFERENCE_KEYS
            # 라는 이름을 우연히 가진 비참조 필드로 보고 손대지 않는다. 규칙 3
            # 은 "값이 str 일 때만"이라 발화 자체가 안 하는 자리이므로 unverified
            # 로도 집계하지 않는다 — 애초에 참조로 판정하지 않은 값이다.
            continue
        if value in mapping:
            new_props[key] = mapping[value]
        elif value == src_pack:
            # 이 두 갈래의 순서는 `src_pack` 이 매핑 키가 아닐 때만 무의미하다
            # (모듈 docstring 규칙 3). 그 전제는 `pack_fork` 의 preflight 가
            # 세운다 — 여기서 재판정하지 않는다.
            new_props[key] = dst_pack
        else:
            # 원본에서 이미 팩 밖을 가리키던 참조(또는 "node:"+old_id 같은
            # 합성 문자열 — 값 전체가 매핑 키와 같지 않으므로 여기로 떨어진다).
            # 새로 만들어 낼 근거가 없으므로 그대로 두고 집계만 한다.
            unverified += 1
    return new_props, unverified


def remap_props(
    props: Mapping[str, Any],
    mapping: Mapping[str, str],
    *,
    src_pack: str,
    dst_pack: str,
) -> tuple[dict[str, Any], int]:
    """노드/엣지/소스 properties 에 규칙 3 을 적용한다.

    Returns
    -------
    ``(재작성된 properties 사본, unverified_ref_count)``. 후자는 §4-A 의
    "보장 범위 밖" 참조(팩 밖 참조, 합성 문자열, 중첩 dict/list) 건수로,
    오케스트레이터가 응답의 `unverified_refs` 로 그대로 합산해 보고한다.
    """
    return _remap_reference_keys(props, mapping, src_pack=src_pack, dst_pack=dst_pack)


def remap_vector_metadata(
    meta: Mapping[str, Any],
    mapping: Mapping[str, str],
    *,
    src_pack: str,
    dst_pack: str,
    owner_id: str,
) -> tuple[dict[str, Any], int]:
    """벡터 metadata 에 규칙 3·4·5 를 전부 적용한다.

    규칙 3(참조 키)은 `remap_props` 와 동일 로직. 그 위에:

    - 규칙 4: `metadata["pack_id"]` 를 **무조건** `dst_pack` 으로 덮는다.
      빠뜨리면 #200 의 import 계약(`declared != pack_id → reject`)이
      record 0 에서 전량 거부하므로 벡터 축이 통째로 죽는다 — 이 재작성이
      단순 정합성 문제가 아니라 임포트 성공의 전제조건이다.
    - 규칙 5: `OWNER_KEYS`(`user_id`/`owner_id`) 중 metadata 에 **있는** 키만
      `owner_id` 로 덮는다(없는 키를 새로 추가하지 않는다). **사후 검증(H4)은
      이 재작성이 빠져도 잡지 못한다** — owner 값은 매핑의 키가 아니라서
      "매핑 키가 안 남아 있다"는 H4 술어가 볼 수 있는 도메인 밖이다. 이 결함을
      막는 유일한 장치가 `tests/test_fork_remap.py` 의 T7c 다.

    Returns
    -------
    ``(재작성된 metadata 사본, unverified_ref_count)`` — 규칙 3 몫만 집계한다.
    (규칙 4·5 는 무조건 재작성이라 "unverified" 개념이 없다.)
    """
    new_meta, unverified = _remap_reference_keys(
        meta, mapping, src_pack=src_pack, dst_pack=dst_pack
    )
    new_meta["pack_id"] = dst_pack
    for key in OWNER_KEYS:
        if key in new_meta:
            new_meta[key] = owner_id
    return new_meta, unverified


def surviving_source_ids(
    payload: Iterable[Mapping[str, Any]],
    mapping: Mapping[str, str],
) -> set[str]:
    """`payload` 의 각 레코드에서 매핑을 살아남은(=원본 id 가 매핑 키에 있는)
    소스의 **새(dst) id** 집합을 뽑는다.

    §5-4-18b 의 잔차 보고(`skipped.sources_without_vectors`)가 쓰는 헬퍼다:
    오케스트레이터가 이 함수를 (a) 복사한 소스 레코드 payload 와 (b) 임포트한
    벡터 레코드 payload 양쪽에 불러 두 집합의 차(`(a) - (b)`)를 구하면,
    "doc 축에는 복사됐는데 벡터 축에는 조용히 빠진 소스"가 드러난다
    (`_doc_owner_pred` 의 레거시 `source` 폴백과 `export_pack_vectors` 의
    `pack_id` 단독 술어가 비대칭이라 생기는 누락 — 설계 §5-4-18b).

    각 레코드의 식별자는 `"source_id"` 를 먼저 보고, 없으면 `"id"` 로
    폴백한다(REFERENCE_KEYS 안에서 소스 레코드에 실제로 쓰이는 두 키 — 이
    순서는 다른 규칙과 마찬가지로 원본 id 공간을 신뢰할 수 있는 자리만 본다는
    원칙을 따른다). 식별자가 없거나 매핑 키에 없는(=preflight 에서 이미 걸러진
    orphan) 레코드는 조용히 건너뛴다 — 이 함수는 매핑을 살아남은 것만 센다.
    """
    survivors: set[str] = set()
    for record in payload:
        old_id = record.get("source_id")
        if old_id is None:
            old_id = record.get("id")
        if old_id is None:
            continue
        new_id = mapping.get(old_id)
        if new_id is not None:
            survivors.add(new_id)
    return survivors
