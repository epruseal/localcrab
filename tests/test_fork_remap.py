"""`opencrab/pack/fork_remap.py` 의 순수 재매핑 규칙 단위 테스트 (#201 §4-A).

역-변이를 검출력의 기준으로 삼는다(설계 §8) — 가드마다 "그 가드를 지우면
죽는" 테스트가 최소 1건이어야 한다. 여기서 검증하는 가드와 대응 설계 항목:

- T7 (네 참조 키 각각): 키 하나를 `REFERENCE_KEYS` 에서 빼면 그 키만 다루는
  테스트 하나만 죽어야 한다 — 그래서 키마다 독립된 테스트로 쪼갠다.
- T7b (규칙 4, pack_id): 재작성을 빼면 #200 의 import 계약이 record 0 에서
  전량 거부한다 — 벡터 축이 통째로 죽는 실패 모드라 별도로 못박는다.
- T7c (규칙 5, 소유 키): 사후 검증(H4)은 이 결함을 못 잡는다(owner 값은
  매핑 키가 아니다) — 이 파일이 유일한 안전장치다.
- 규칙 2 (앵커): `build_mapping` 의 앵커 항목이 salt 매핑을 덮어써 항상
  `dst_anchor` 를 직통으로 가리키는지 — 잘못 읽으면 앵커 벡터 이중 ADD 로
  벡터 축 전량이 죽는다(§4-A 규칙 2).
- 규칙 3 의 보장 범위 한계(§4-A "보장 범위의 명시적 한계", T33/T40/T47):
  중첩 dict/list, 합성 문자열, 팩 밖 참조는 재작성하지 않고 `unverified_refs`
  로만 집계한다 — 조용히 통과시키면(집계 누락) 사용자에게 보고되지 않는다.
- 입력 비-변경: `remap_props`/`remap_vector_metadata` 가 호출자의 dict 를
  제자리 수정하면, 오케스트레이터가 원본 export 결과를 재사용하는 다른 축
  (예: §5-4-18b 잔차 보고)에서 이미 재매핑된 값을 다시 읽는 오염이 생긴다.
"""

from __future__ import annotations

from opencrab.pack.fork_remap import (
    FORK_SALT_BYTES,
    OWNER_KEYS,
    REFERENCE_KEYS,
    build_mapping,
    new_salt,
    remap_id,
    remap_props,
    remap_vector_metadata,
    surviving_source_ids,
)

SRC_PACK = "pack-src"
DST_PACK = "pack-dst"
SALT = "abc123abc123"  # 고정 salt — 테스트 값을 손으로 재현 가능하게 한다.


def _mapping(*ids: str, src_anchor: str = "anchor-src", dst_anchor: str = "anchor-dst"):
    return build_mapping(ids, (), salt=SALT, src_anchor=src_anchor, dst_anchor=dst_anchor)


# ---------------------------------------------------------------------------
# new_salt / remap_id — 기초 결정성
# ---------------------------------------------------------------------------


def test_new_salt_is_hex_of_expected_length():
    salt = new_salt()
    assert len(salt) == FORK_SALT_BYTES * 2
    int(salt, 16)  # ValueError 없이 hex 로 파싱돼야 한다


def test_new_salt_calls_differ():
    # secrets.token_hex 기반이라 사실상 항상 다르다. 결정론이 아니라
    # "매 fork 호출마다 새 salt" 라는 §4-A 규칙 1의 전제를 확인한다.
    assert new_salt() != new_salt()


def test_remap_id_is_deterministic_and_salt_sensitive():
    assert remap_id("node-1", SALT) == remap_id("node-1", SALT)
    assert remap_id("node-1", SALT) != remap_id("node-1", "different-salt")
    assert remap_id("node-1", SALT) == f"node-1~{SALT}"


# ---------------------------------------------------------------------------
# T7 — 네 참조 키 각각 독립 재작성 (REFERENCE_KEYS 에서 하나 빼면 이 중 하나만 죽는다)
# ---------------------------------------------------------------------------


def test_remap_props_rewrites_node_id_via_mapping():
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"node_id": "node-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["node_id"] == mapping["node-1"]
    assert unverified == 0


def test_remap_props_rewrites_source_id_via_mapping():
    mapping = _mapping("source-1")
    props, unverified = remap_props(
        {"source_id": "source-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["source_id"] == mapping["source-1"]
    assert unverified == 0


def test_remap_props_rewrites_document_id_via_mapping():
    mapping = _mapping("doc-1")
    props, unverified = remap_props(
        {"document_id": "doc-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["document_id"] == mapping["doc-1"]
    assert unverified == 0


def test_remap_props_rewrites_source_field_from_src_pack_to_dst_pack():
    # "source" 는 매핑 키가 아니라 규칙 3 의 두 번째 갈래(== src_pack)로 걸린다 —
    # 빌더가 노드 벡터의 `source` 자리에 pack_id 를 그대로 찍기 때문이다.
    mapping = _mapping()
    props, unverified = remap_props(
        {"source": SRC_PACK}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["source"] == DST_PACK
    assert unverified == 0


def test_t91_pack_id_as_a_content_key_makes_the_two_branches_ambiguous():
    # T91 (설계 §14-3): 이 모듈은 값 하나를 두 네임스페이스(매핑 키 = 콘텐츠 id,
    # `src_pack` = 팩 id)에 대고 고정 순서로 해석한다. 두 공간이 겹치면 첫 갈래가
    # 이겨서 pack 값 자리가 `dst_pack` 이 아니라 재매핑 id 를 받는다. 그 오작동을
    # 여기 고정해 두는 이유는, 이것이 `fork_pack` 이 그런 팩을 아예 거부하는
    # (§14-5 step 6c) 근거이기 때문이다. 갈래 순서를 뒤집는 변경은 이 행을 죽여
    # §14-3 의 양방향 반례를 다시 검토하게 만든다 — 뒤집으면 반대로 충돌 노드를
    # 가리키는 진짜 참조가 팩 id 를 가리키게 되므로 그쪽도 옳지 않다.
    mapping = _mapping(SRC_PACK)
    props, unverified = remap_props(
        {"source": SRC_PACK}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["source"] == mapping[SRC_PACK]
    assert props["source"] != DST_PACK
    assert unverified == 0


def test_remap_props_rewrites_id_key_via_mapping():
    # REFERENCE_KEYS 의 다섯 번째 키. `upsert_node` 가 항상 주입하는
    # `props["id"]` 자리(설계 §1 실측)를 커버한다.
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"id": "node-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["id"] == mapping["node-1"]
    assert unverified == 0


def test_reference_keys_tuple_has_exactly_the_five_normative_keys():
    # 이 테스트 자체가 REFERENCE_KEYS 의 회귀 가드다: 위 개별 키 테스트들이
    # "제거하면 그 키만 죽는다"는 전제가 성립하려면 다섯 키가 정확히 이것이어야 한다.
    assert REFERENCE_KEYS == ("node_id", "source_id", "document_id", "source", "id")


# ---------------------------------------------------------------------------
# T7b — 규칙 4: pack_id 재작성 (#200 import 계약의 전제조건)
# ---------------------------------------------------------------------------


def test_remap_vector_metadata_rewrites_pack_id_to_dst_pack():
    # 이 재작성을 빼면 opencrab.stores 의 validate_import_records 가
    # "declared pack_id != target pack_id" 로 record 0 에서 즉시 거부한다 —
    # export_pack_vectors 는 모든 record 에 pack_id=src 를 달아 내보내므로
    # (설계 §1 실측, §4-A 규칙 4) 벡터 축 전체가 첫 record 에서 죽는다.
    meta = {"pack_id": SRC_PACK}
    new_meta, _ = remap_vector_metadata(
        meta, _mapping(), src_pack=SRC_PACK, dst_pack=DST_PACK, owner_id="forker-1"
    )
    assert new_meta["pack_id"] == DST_PACK


def test_remap_vector_metadata_sets_pack_id_even_if_absent():
    # 방어적 케이스: 무조건 재작성이지 "있으면 고친다"가 아니다.
    new_meta, _ = remap_vector_metadata(
        {}, _mapping(), src_pack=SRC_PACK, dst_pack=DST_PACK, owner_id="forker-1"
    )
    assert new_meta["pack_id"] == DST_PACK


# ---------------------------------------------------------------------------
# T7c — 규칙 5: 소유 키 재작성 (H4 사후 검증으로는 못 잡는 결함)
# ---------------------------------------------------------------------------


def test_remap_vector_metadata_rewrites_owner_keys_to_forker():
    assert OWNER_KEYS == ("user_id", "owner_id")
    meta = {"user_id": "original-owner", "owner_id": "original-owner"}
    new_meta, _ = remap_vector_metadata(
        meta, _mapping(), src_pack=SRC_PACK, dst_pack=DST_PACK, owner_id="forker-1"
    )
    assert new_meta["user_id"] == "forker-1"
    assert new_meta["owner_id"] == "forker-1"


def test_remap_vector_metadata_does_not_add_absent_owner_keys():
    # 규칙 5 는 "있으면 치환"이다. 없는 키를 새로 만들어 붙이지 않는다 —
    # 원본 벡터 metadata 형상에 없던 필드를 추가하면 H10(비참조 metadata
    # 그대로 통과)과 충돌한다.
    meta = {"user_id": "original-owner"}
    new_meta, _ = remap_vector_metadata(
        meta, _mapping(), src_pack=SRC_PACK, dst_pack=DST_PACK, owner_id="forker-1"
    )
    assert new_meta["user_id"] == "forker-1"
    assert "owner_id" not in new_meta


# ---------------------------------------------------------------------------
# 구조적 엣지 끝점 (규칙 6) — build_mapping 의 dict 를 from_id/to_id 에 직접 적용
# ---------------------------------------------------------------------------


def test_structural_edge_endpoints_are_remapped_via_build_mapping():
    # add_edge 는 from_id/to_id 를 properties 가 아니라 별도 구조 필드로
    # 받으므로 remap_props 를 거치지 않는다 — 호출자가 build_mapping 의
    # 매핑 dict 를 두 필드에 직접 적용한다는 것을 확인한다.
    from_id, to_id = "node-from", "node-to"
    mapping = build_mapping(
        [from_id, to_id], [], salt=SALT, src_anchor="anchor-src", dst_anchor="anchor-dst"
    )
    new_from_id = mapping[from_id]
    new_to_id = mapping[to_id]
    assert new_from_id == remap_id(from_id, SALT)
    assert new_to_id == remap_id(to_id, SALT)
    assert new_from_id != from_id
    assert new_to_id != to_id


# ---------------------------------------------------------------------------
# 보장 범위 한계 (§4-A) — 중첩 구조, 합성 문자열, 팩 밖 참조
# ---------------------------------------------------------------------------


def test_nested_dict_under_reference_key_is_not_rewritten_and_is_unverified():
    mapping = _mapping("node-1")
    original_nested = {"node_id": "node-1"}
    props, unverified = remap_props(
        {"node_id": original_nested}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["node_id"] == original_nested
    assert props["node_id"] is original_nested  # 재귀 안 함 — 참조 그대로 보존
    assert unverified == 1


def test_list_under_reference_key_is_not_rewritten_and_is_unverified():
    mapping = _mapping("node-1", "node-2")
    original_list = ["node-1", "node-2"]
    props, unverified = remap_props(
        {"node_id": original_list}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["node_id"] == original_list
    assert unverified == 1


def test_composite_string_reference_is_not_rewritten_and_is_unverified():
    # 값 전체가 매핑 키와 같지 않으므로("node:node-1" != "node-1") 세 번째
    # 갈래(unverified)로 떨어진다. 부분 문자열 치환은 하지 않는다(오탐 방지).
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"document_id": "node:node-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["document_id"] == "node:node-1"
    assert unverified == 1


def test_out_of_pack_reference_is_left_unchanged_and_unverified():
    # 매핑 키에도 없고 src_pack 도 아닌 값 — 원본에서 이미 다른 팩을 가리키던
    # 참조. 새로 만들어 낼 근거가 없으므로 그대로 두고 집계만 한다.
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"source": "some-other-pack"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["source"] == "some-other-pack"
    assert unverified == 1


def test_unverified_keys_outside_reference_keys_are_ignored():
    # REFERENCE_KEYS 밖의 임의 키(`parent_id`)는 값이 원본 id 와 같아도
    # 손대지 않고 unverified 로도 세지 않는다 — 도메인 자체가 아니다.
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"parent_id": "node-1"}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["parent_id"] == "node-1"
    assert unverified == 0


def test_non_string_scalar_under_reference_key_is_left_alone_and_not_counted():
    # str 도 dict/list 도 아닌 값(예: None)은 규칙 3 이 발화하는 도메인 밖이다.
    mapping = _mapping("node-1")
    props, unverified = remap_props(
        {"node_id": None}, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK
    )
    assert props["node_id"] is None
    assert unverified == 0


# ---------------------------------------------------------------------------
# 입력 비-변경
# ---------------------------------------------------------------------------


def test_remap_props_does_not_mutate_input():
    mapping = _mapping("node-1")
    original = {"node_id": "node-1", "other": "untouched"}
    snapshot = dict(original)
    remap_props(original, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK)
    assert original == snapshot


def test_remap_vector_metadata_does_not_mutate_input():
    mapping = _mapping("node-1")
    original = {"node_id": "node-1", "pack_id": SRC_PACK, "user_id": "orig"}
    snapshot = dict(original)
    remap_vector_metadata(
        original, mapping, src_pack=SRC_PACK, dst_pack=DST_PACK, owner_id="forker-1"
    )
    assert original == snapshot


# ---------------------------------------------------------------------------
# 규칙 2 — 앵커 매핑 항목은 엣지 끝점 재지정 전용
# ---------------------------------------------------------------------------


def test_anchor_mapping_entry_points_directly_to_dst_anchor():
    mapping = build_mapping(
        ["node-1"], [], salt=SALT, src_anchor="anchor-src", dst_anchor="anchor-dst"
    )
    assert mapping["anchor-src"] == "anchor-dst"


def test_anchor_entry_overrides_salted_value_when_anchor_is_in_node_ids():
    # export 는 앵커도 보통 노드로 함께 돌려주므로 node_ids 에 앵커 id 가 섞여
    # 들어올 수 있다. build_mapping 이 anchor 항목을 나중에 덮어써서, 앵커는
    # 항상 dst_anchor 를 직통으로 가리키지 salt 붙은 값을 가리키지 않는다 —
    # 그렇지 않으면 호출자가 "이 매핑을 다 써서 노드를 복사"할 때 앵커까지
    # 두 번째로 쓰여 벡터 임포트가 ADD 충돌로 죽는다(§4-A 규칙 2).
    src_anchor = "anchor-src"
    dst_anchor = "anchor-dst"
    mapping = build_mapping(
        ["node-1", src_anchor], [], salt=SALT, src_anchor=src_anchor, dst_anchor=dst_anchor
    )
    assert mapping[src_anchor] == dst_anchor
    assert mapping[src_anchor] != remap_id(src_anchor, SALT)


def test_build_mapping_covers_both_node_ids_and_source_ids():
    mapping = build_mapping(
        ["node-1"], ["source-1"], salt=SALT, src_anchor="anchor-src", dst_anchor="anchor-dst"
    )
    assert mapping["node-1"] == remap_id("node-1", SALT)
    assert mapping["source-1"] == remap_id("source-1", SALT)


# ---------------------------------------------------------------------------
# surviving_source_ids — §5-4-18b 잔차 보고 헬퍼
# ---------------------------------------------------------------------------


def test_surviving_source_ids_maps_records_to_new_ids():
    mapping = _mapping("source-1", "source-2")
    payload = [{"source_id": "source-1"}, {"source_id": "source-2"}]
    result = surviving_source_ids(payload, mapping)
    assert result == {mapping["source-1"], mapping["source-2"]}


def test_surviving_source_ids_falls_back_to_id_key():
    mapping = _mapping("source-1")
    payload = [{"id": "source-1"}]
    assert surviving_source_ids(payload, mapping) == {mapping["source-1"]}


def test_surviving_source_ids_skips_records_not_in_mapping():
    # preflight 에서 이미 걸러진 orphan(매핑에 없는 id)은 조용히 빠진다 —
    # 이 함수는 "매핑을 살아남은 것"만 집계한다.
    mapping = _mapping("source-1")
    payload = [{"source_id": "source-1"}, {"source_id": "orphan-id"}]
    assert surviving_source_ids(payload, mapping) == {mapping["source-1"]}


def test_surviving_source_ids_skips_records_without_identifier():
    mapping = _mapping("source-1")
    payload = [{"source_id": "source-1"}, {"unrelated": "field"}]
    assert surviving_source_ids(payload, mapping) == {mapping["source-1"]}
