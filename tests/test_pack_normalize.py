"""opencrab.pack.normalize — 순수 정규화 계층 계약.

여기서 검사하는 것은 "적재기와 게이트가 같은 판정을 쓴다"는 계약 자체다.
표가 아니라 **판정 함수**를 검사한다 — 표만 맞고 순서가 다르면 게이트 통과가
적재 성공을 뜻하지 않게 되고, 그것이 2026-08-04 이관의 이유였다.
"""

from types import MappingProxyType

import pytest

from opencrab.pack import normalize
from opencrab.pack.schema import (
    ALL_SPACES,
    NODE_STRUCT_KEYS,
    NODE_TYPE_OVERRIDE,
    SPACE_DEFAULT_TYPE,
)

N = normalize


# ── resolve_node_space_type ──────────────────────────────────────────────────

@pytest.mark.parametrize("src_type", sorted(NODE_TYPE_OVERRIDE))
def test_override_wins_over_grammar_registration(src_type):
    """NODE_TYPE_OVERRIDE 는 space 까지 바꾼다 — grammar 등록 여부보다 먼저 본다.

    전 항목을 도는 이유: 우선순위가 실제로 갈리는 것은 **grammar 미등록**인 키뿐이다.
    임의의 한 항목만 보면 그 키가 등록형일 때 순서를 뒤집어도 테스트가 통과한다
    (실제로 그렇게 만들었다가 돌연변이 1종을 생존시켰다).
    """
    want = NODE_TYPE_OVERRIDE[src_type]
    # 원본 space 를 일부러 엉뚱하게 줘도 override 가 이긴다
    assert N.resolve_node_space_type("outcome", src_type) == want


def test_unregistered_override_keys_exist():
    """위 테스트가 공허하지 않음을 보증한다 — 이런 키가 0개면 순서는 관측 불가다."""
    unregistered = [k for k in NODE_TYPE_OVERRIDE if k not in N._GRAMMAR_NODE_TYPES]
    assert unregistered, "override 키가 전부 grammar 등록형이면 우선순위를 검사할 수 없다"


def test_unregistered_type_falls_back_to_space_default():
    assert "Zzz없는타입" not in N._GRAMMAR_NODE_TYPES
    assert N.resolve_node_space_type("evidence", "Zzz없는타입") == SPACE_DEFAULT_TYPE["evidence"]


def test_registered_type_invalid_for_space_falls_back_again():
    """subject/Document — Document 는 resource 전용이라 subject 에서는 무효다.

    이 3단계가 없으면 add_node 가 ValueError 를 던지고 로더가 skip 으로 삼킨다.
    """
    assert "Document" in N._GRAMMAR_NODE_TYPES
    assert "Document" not in N._GRAMMAR_SPACES["subject"]["node_types"]
    assert N.resolve_node_space_type("subject", "Document") == SPACE_DEFAULT_TYPE["subject"]


def test_space_default_keys_are_all_real_spaces():
    """2단계(grammar 미등록 -> space 기본형)가 3단계에 흡수되는 전제.

    3단계는 `_allowed` 가 비어 있지 않을 때만 발동한다. SPACE_DEFAULT_TYPE 의 키가
    전부 실재 space 라면 미등록 타입은 3단계에서도 똑같이 걸리므로 2단계 제거는
    등가 변이다(적대 검증이 전 조합 differences 0 으로 실증). 이 전제가 깨지면
    2단계가 유일 경로가 되므로 여기서 못박는다.
    """
    for space in SPACE_DEFAULT_TYPE:
        allowed = N._GRAMMAR_SPACES.get(space, {}).get("node_types", ())
        assert allowed, f"{space} 가 grammar 에 없다 — 2단계가 유일 경로가 된다"


def test_valid_pair_is_left_alone():
    assert N.resolve_node_space_type("resource", "Document") == ("resource", "Document")


def test_every_space_default_is_valid_in_its_space():
    """SPACE_DEFAULT_TYPE 자체가 무효하면 3단계 귀착이 헛돈다."""
    for space, (out_space, out_type) in SPACE_DEFAULT_TYPE.items():
        allowed = N._GRAMMAR_SPACES.get(out_space, {}).get("node_types", ())
        assert out_type in allowed, f"{space} 의 기본형 {out_space}/{out_type} 이 grammar 에 없다"


# ── resolve_edge ─────────────────────────────────────────────────────────────

def test_override_reverse_returns_swapped_spaces():
    """claim→evidence SUPPORTS 는 evidence→claim supports 로 반전된다."""
    assert ("SUPPORTS", "claim", "evidence") in N.LABEL_SPACE_OVERRIDE
    assert N.resolve_edge("SUPPORTS", "claim", "evidence") == (
        "evidence", "supports", "claim", True)


def test_every_override_value_is_a_truthy_pair():
    """`if override:` 가 `is not None` 과 등가임을 보장하는 전제.

    값이 falsy 해질 수 있으면 그 항목만 조용히 REL_MAP 경로로 새는데, 두 경로는
    반전 규칙이 달라 방향이 뒤집힌다. 전제를 여기서 못박아 등가 변이를 없앤다.
    """
    for key, value in N.LABEL_SPACE_OVERRIDE.items():
        assert value, f"{key} 의 값이 falsy 다"
        relation, reverse = value
        assert relation and isinstance(reverse, bool), f"{key} -> {value!r}"


def test_override_without_reverse_keeps_direction():
    assert N.resolve_edge("SUPPORTS", "claim", "outcome") == (
        "claim", "supports", "outcome", False)


def test_same_label_resolves_differently_per_space_pair():
    """3-tuple 분기가 죽으면 이 둘이 같은 결과가 된다."""
    assert N.resolve_edge("SUPPORTS", "claim", "evidence") != N.resolve_edge(
        "SUPPORTS", "claim", "outcome")


def test_reverse_relations_flip_direction():
    a, rel, b, rev = N.resolve_edge("HAS_PART", "resource", "concept")
    assert rel == "part_of"
    assert (a, b, rev) == ("concept", "resource", True)


def test_lookup_ignores_case():
    assert N.resolve_edge("has_part", "resource", "concept") == N.resolve_edge(
        "HAS_PART", "resource", "concept")


def test_unknown_label_falls_back_to_lowercase():
    assert N.resolve_edge("전혀없는라벨", "concept", "outcome") == (
        "concept", "전혀없는라벨", "outcome", False)


def test_empty_label_does_not_raise():
    assert N.resolve_edge("", "concept", "concept") == ("concept", "", "concept", False)


@pytest.mark.parametrize("label", sorted(N.REVERSE_RELATIONS))
def test_every_listed_reverse_label_actually_reverses(label):
    a, _rel, b, rev = N.resolve_edge(label, "resource", "concept")
    assert rev is True and (a, b) == ("concept", "resource")


# ── transform_node ───────────────────────────────────────────────────────────

def _node(**kw):
    base = {"id": "n1", "label": "라벨", "space": "concept", "node_type": "Concept"}
    base.update(kw)
    return base


def test_returns_space_type_id_props_in_that_order():
    """반환 계약 전체를 한 번은 통째로 단언한다.

    나머지 검사가 props 만 보고 node_id 를 `_i` 로 버리면, id 를 workspace_id 나
    pack_name 으로 바꿔치기해도 전부 통과한다(2026-08-04 적대 검증이 실증).
    node_id 는 id_map 의 키이자 모든 엣지 endpoint 해석의 기준이라 틀리면 팩 전체가 무너진다.
    """
    row = _node(id="node-1", workspace_id="workspace-1", space="resource",
                node_type="Document", properties={"title": "제목"})
    space, node_type, node_id, props = N.transform_node("pack-1", row)
    assert (space, node_type, node_id) == ("resource", "Document", "node-1")
    assert props["pack_id"] == "pack-1"


@pytest.mark.parametrize("decoy_key,decoy", [
    ("workspace_id", "workspace-1"), ("label", "라벨값"), ("source_type", "src"),
])
def test_node_id_is_never_taken_from_another_field(decoy_key, decoy):
    _s, _t, node_id, _p = N.transform_node(
        "pack-1", _node(id="node-1", **{decoy_key: decoy}))
    assert node_id == "node-1"


def test_node_id_is_not_the_pack_name():
    _s, _t, node_id, _p = N.transform_node("pack-1", _node(id="node-1"))
    assert node_id == "node-1"


def test_nested_properties_beat_legacy_top_level():
    """정본 위치는 중첩이다. 반대로 되면 이미 적재된 값이 조용히 바뀐다."""
    _s, _t, _i, props = N.transform_node("p", _node(properties={"k": "중첩"}, k="최상위"))
    assert props["k"] == "중첩"


def test_legacy_top_level_custom_fields_are_absorbed():
    """91만 필드가 라이브에 도달하지 못한 사고의 회귀 검사.

    2026-08-03 이전 pack_lib 은 커스텀 필드를 노드 최상위에 펼쳤고 이 함수는
    중첩 properties 만 읽었다.
    """
    _s, _t, _i, props = N.transform_node("p", _node(커스텀="값", properties={}))
    assert props["커스텀"] == "값"


@pytest.mark.parametrize("key", sorted(NODE_STRUCT_KEYS - {"id", "properties", "degree", "label"}))
def test_no_struct_key_leaks_into_props(key):
    """구조 키는 흡수 대상이 아니다 — 전 항목을 돈다.

    한두 개만 보면 집합에서 다른 하나를 빼도 통과한다. 실제로 그렇게 썼다가
    `workspace_id` 제거 변이를 생존시켰다(실데이터 238,987건 전건 보유,
    2026-08-04 적대 검증). degree/label 은 아래 별도 계약으로 props 에 들어가고,
    id/properties 는 흡수 로직의 입력 자체라 이 목록에서 뺀다.
    """
    _s, _t, _i, props = N.transform_node("p", _node(**{key: "누출값"}))
    assert key not in props, f"{key} 가 props 로 새어 들어갔다"


def test_only_declared_struct_keys_reach_props():
    _s, _t, _i, props = N.transform_node("p", _node(degree=7, source_type="x"))
    assert set(props) & NODE_STRUCT_KEYS <= {"degree", "label", "properties"}


def test_overridden_node_keeps_original_type():
    src_type, (_sp, want_type) = next(
        (k, v) for k, v in NODE_TYPE_OVERRIDE.items() if v[1] != k)
    _s, t, _i, props = N.transform_node("p", _node(node_type=src_type))
    assert t == want_type and props["original_type"] == src_type


def test_absorbed_value_cannot_overwrite_original_type():
    """레거시 최상위에 original_type 이 있어도 로더가 계산한 값이 이긴다."""
    src_type, _ = next((k, v) for k, v in NODE_TYPE_OVERRIDE.items() if v[1] != k)
    _s, _t, _i, props = N.transform_node(
        "p", _node(node_type=src_type, original_type="거짓말"))
    assert props["original_type"] == src_type


def test_pack_id_is_forced_to_the_argument():
    _s, _t, _i, props = N.transform_node("내팩", _node(properties={"pack_id": "남의팩"}))
    assert props["pack_id"] == "내팩" and props["pack"] == "내팩"


def test_statement_fallback_reads_nested_only():
    """레거시 흡수분을 폴백 소스로 끌어들이면 적재된 Claim 의 statement 가 바뀐다.

    실측(2026-08-04): 완결 문장 label 이 단편 description 으로 퇴화했다.
    """
    row = _node(node_type="Claim", space="claim", label="완결된 문장이다.",
                description="단편.", properties={})
    _s, t, _i, props = N.transform_node("p", row)
    assert t == "Claim"
    assert props["statement"] == "완결된 문장이다."
    assert props["description"] == "단편."   # 흡수는 됐지만 폴백 소스는 아니다


def test_user_email_is_synthesized_and_marked():
    """이메일이 없으면 add_node 가 ValueError → skip → 그 저자 엣지가 영구 dangling."""
    _s, t, _i, props = N.transform_node("p", _node(node_type="User", space="subject"))
    assert t == "User"
    assert props["email"] == "n1@local.invalid" and props["email_synthesized"] is True


def test_enum_violation_is_corrected_and_original_kept():
    _s, _t, _i, props = N.transform_node(
        "p", _node(node_type="User", space="subject", properties={"role": "왕"}))
    assert props["role"] == "viewer" and props["role_original"] == "왕"


def test_label_is_generalized_to_name():
    _s, _t, _i, props = N.transform_node("p", _node(label="표시이름"))
    assert props["name"] == "표시이름" and props["label"] == "표시이름"


def test_label_generalization_does_not_overwrite_existing_values():
    """맨 끝 일반화는 setdefault 다 — 대입으로 바꾸면 팩이 준 label·name 이 조용히 덮인다."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(label="행의 라벨",
                   properties={"label": "원래 라벨", "name": "원래 이름"}))
    assert props["label"] == "원래 라벨" and props["name"] == "원래 이름"


def test_missing_label_does_not_invent_a_name():
    """UUID 오염 방지 — 실제 label 보유 행에만 주입한다."""
    _s, _t, _i, props = N.transform_node("p", _node(label=None))
    assert "name" not in props


def test_degree_defaults_from_top_level_then_zero():
    """degree 는 구조 키라 흡수 대상이 아니다 — setdefault 가 유일한 유입 경로다.

    실측(2026-08-04, 128팩 238,987노드): 중첩 보유 35,250 · 최상위만 60,097 ·
    둘 다 없음 143,640. 즉 20만 노드가 이 한 줄에 의존한다.
    """
    _s, _t, _i, props = N.transform_node("p", _node(degree=7))
    assert props["degree"] == 7
    _s, _t, _i, props = N.transform_node("p", _node())
    assert props["degree"] == 0


def test_nested_degree_beats_top_level():
    _s, _t, _i, props = N.transform_node("p", _node(degree=7, properties={"degree": 3}))
    assert props["degree"] == 3


def test_document_title_is_filled_from_label():
    """Document 스키마의 required 필드 — 비면 add_node 가 ValueError 로 죽는다."""
    _s, t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document", label="문서 제목"))
    assert t == "Document" and props["title"] == "문서 제목"


def test_existing_title_is_not_overwritten():
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document", label="라벨",
                   properties={"title": "원래 제목"}))
    assert props["title"] == "원래 제목"


@pytest.mark.parametrize("space,node_type", [
    ("subject", "Team"), ("subject", "Org"),
    ("outcome", "Outcome"), ("lever", "Lever"), ("policy", "Policy"),
])
def test_name_is_filled_even_without_a_label(space, node_type):
    """label 이 없으면 타입별 자동채움이 name 의 **유일한** 유입 경로다.

    label 이 있으면 맨 끝의 label->name 일반화가 어차피 채우므로, 그 경우로만 검사하면
    자동채움을 통째로 지워도 테스트가 통과한다(실제로 그렇게 만들었다가 변이 2종을
    생존시켰다). name 은 required 라 비면 add_node 가 ValueError 로 죽는다.
    """
    _s, t, _i, props = N.transform_node(
        "p", _node(space=space, node_type=node_type, label=None))
    assert t == node_type and props["name"] == "n1"


@pytest.mark.parametrize("space,node_type", [
    ("subject", "Team"), ("outcome", "Outcome"), ("lever", "Lever"),
])
def test_name_follows_label_when_present(space, node_type):
    _s, _t, _i, props = N.transform_node(
        "p", _node(space=space, node_type=node_type, label="이름"))
    assert props["name"] == "이름"


@pytest.mark.parametrize("bad", ["이상한값", "", None, "PASS", "passed"])
def test_collection_completeness_corrects_every_invalid_status(bad):
    """status 는 enum(pass/retry/fail) — 위반값이 들어가면 add_node 가 skip 된다.

    빈 문자열까지 도는 이유: 유효값 튜플에서 한 원소를 빈 문자열로 바꾸는 변이는
    빈 status 를 유효로 통과시킨다. 대표값 하나만 보면 그 변이가 생존한다
    (2026-08-04 전면 스윕에서 실제로 생존했다).
    """
    _s, t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type="CollectionCompleteness",
                   properties={"status": bad}))
    assert t == "CollectionCompleteness"
    assert props["status"] == "pass" and props["score"] == 1.0


@pytest.mark.parametrize("blank", ["", None])
def test_document_title_is_filled_when_blank(blank):
    """조건은 `not props.get("title")` 다 — 키 부재뿐 아니라 **빈 값**도 채운다.

    빈 문자열은 required 를 만족한 것처럼 보이지만 스키마 검증을 통과해도 표시가 죽는다.
    키 부재로만 검사하면 `not` 을 `is None` 으로 바꿔도 테스트가 통과한다.
    """
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document", label="문서",
                   properties={"title": blank}))
    assert props["title"] == "문서"


@pytest.mark.parametrize("space,node_type", [
    ("subject", "Team"), ("subject", "Org"),
    ("outcome", "Outcome"), ("lever", "Lever"), ("policy", "Policy"),
])
def test_name_is_filled_when_blank(space, node_type):
    _s, _t, _i, props = N.transform_node(
        "p", _node(space=space, node_type=node_type, label="이름",
                   properties={"name": ""}))
    assert props["name"] == "이름"


def test_policy_rule_type_is_filled_when_blank():
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="policy", node_type="Policy", properties={"rule_type": ""}))
    assert props["rule_type"] == "classification"


def test_user_name_is_filled_without_a_label():
    """User 는 name 자동채움 분기가 따로 있다 — label 이 없으면 여기가 유일 경로다."""
    _s, t, _i, props = N.transform_node(
        "p", _node(space="subject", node_type="User", label=None))
    assert t == "User" and props["name"] == "n1"


@pytest.mark.parametrize("status", ["pass", "retry", "fail"])
def test_collection_completeness_keeps_every_valid_status(status):
    """유효 enum 3종을 전부 돈다 — 하나만 보면 그 값만 남기는 변이가 생존한다."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type="CollectionCompleteness",
                   properties={"status": status, "score": 0.5}))
    assert props["status"] == status and props["score"] == 0.5


def test_policy_gets_name_and_rule_type():
    _s, t, _i, props = N.transform_node(
        "p", _node(space="policy", node_type="Policy", label="정책명"))
    assert t == "Policy"
    assert props["name"] == "정책명" and props["rule_type"] == "classification"


def test_document_format_enum_violation_is_corrected():
    """실측 19건(n2sf 15 · krmf 3 · aiready 1, 예 'pdf(pdftotext)')."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document",
                   properties={"format": "pdf(pdftotext)"}))
    assert props["format"] == "plain" and props["format_original"] == "pdf(pdftotext)"


@pytest.mark.parametrize("fmt", ["markdown", "pdf", "html", "plain"])
def test_valid_document_format_is_untouched(fmt):
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document", properties={"format": fmt}))
    assert props["format"] == fmt and "format_original" not in props


def test_transform_node_flattens_nested_properties():
    """flatten_props 를 직접만 검사하면 transform_node 가 그것을 **부르는지**는 안 본다.

    실제로 `flatten_props(...)` 를 `dict(...)` 로 바꾸는 변이가 생존했다
    (2026-08-04 적대 검증). 스토어는 스칼라만 받으므로 미호출은 적재 실패다.
    """
    _s, _t, _i, props = N.transform_node(
        "p", _node(properties={"d": {"a": 1}, "n": None, "mixed": [1, {"x": 2}]}))
    assert props["d"] == "{'a': 1}"
    assert props["n"] == ""
    assert isinstance(props["mixed"], str)


def test_transform_node_flattens_absorbed_legacy_values_too():
    """흡수 경로도 같은 flatten 을 거쳐야 한다 — 최상위에도 중첩 dict 가 올 수 있다."""
    _s, _t, _i, props = N.transform_node("p", _node(레거시={"a": 1}, properties={}))
    assert props["레거시"] == "{'a': 1}"


def test_transform_node_is_deterministic():
    """증분 대조가 이 결정성에 의존한다 — 시각·난수 삽입 금지."""
    row = _node(properties={"a": 1})
    assert N.transform_node("p", row) == N.transform_node("p", row)


def test_transform_node_does_not_mutate_input():
    row = _node(properties={"a": 1}, 커스텀="값")
    before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in row.items()}
    N.transform_node("p", row)
    assert row == before


# ── flatten_props / chroma_safe_meta ─────────────────────────────────────────

def test_flatten_keeps_scalar_lists_but_chroma_serializes_them():
    d = {"xs": [1, 2, 3]}
    assert N.flatten_props(d)["xs"] == [1, 2, 3]
    assert N.chroma_safe_meta(d)["xs"] == "[1, 2, 3]"


def test_nested_dict_is_stringified_by_flatten():
    """스토어는 스칼라만 받는다 — dict 를 그대로 두면 적재가 죽는다."""
    out = N.flatten_props({"d": {"a": 1}})["d"]
    assert isinstance(out, str) and out == "{'a': 1}"


def test_none_handling_differs_between_the_two():
    """flatten 은 빈 문자열, chroma 는 키 제거(Chroma 가 None 을 거부한다)."""
    assert N.flatten_props({"a": None})["a"] == ""
    assert "a" not in N.chroma_safe_meta({"a": None})


@pytest.mark.parametrize("value", [True, False])
def test_bool_survives_both_as_bool(value):
    """bool 은 스칼라다 — 문자열화하면 Chroma 필터 `where={"k": True}` 가 안 맞는다."""
    assert N.flatten_props({"k": value})["k"] is value
    assert N.chroma_safe_meta({"k": value})["k"] is value


def test_mixed_lists_are_stringified_by_both():
    d = {"xs": [1, {"k": "v"}]}
    assert isinstance(N.flatten_props(d)["xs"], str)
    assert isinstance(N.chroma_safe_meta(d)["xs"], str)


def test_chroma_safe_meta_tolerates_none_input():
    assert N.chroma_safe_meta(None) == {}


# ── transform_chunk_meta ─────────────────────────────────────────────────────

def test_original_source_moves_to_source_doc():
    """권위 필드 source 는 pack_name 으로 고정되므로 원본과 충돌한다."""
    meta = N.transform_chunk_meta("내팩", {"metadata": {"source": "원본경로.md"}})
    assert meta["source_doc"] == "원본경로.md"
    assert meta["source"] == "내팩" and meta["pack_id"] == "내팩"


def test_document_id_is_lifted_from_the_row():
    meta = N.transform_chunk_meta("p", {"metadata": {}, "document_id": "d1"})
    assert meta["document_id"] == "d1"


@pytest.mark.parametrize("blank", ["", None])
def test_blank_document_id_is_not_promoted(blank):
    """조건은 truthiness 다 — 빈 값을 올리면 라이브에 빈 document_id 가 생긴다."""
    assert "document_id" not in N.transform_chunk_meta(
        "p", {"metadata": {}, "document_id": blank})


def test_missing_metadata_is_tolerated():
    assert N.transform_chunk_meta("p", {}) == {"pack_id": "p", "source": "p"}


def test_transform_chunk_meta_is_deterministic():
    row = {"metadata": {"char_start": 0}, "document_id": "d"}
    assert N.transform_chunk_meta("p", row) == N.transform_chunk_meta("p", row)


# ── 무정규화 계약 ─────────────────────────────────────────────────────────────
#
# 아래는 전부 "이 계층은 값을 **있는 그대로** 옮긴다"는 계약이다. 조용한 정규화(공백 제거,
# 길이 절단, 유니코드 NFC, 키 정렬, 대소문자 무시)는 하나같이 "개선처럼 보이는 변경"이라
# 리뷰를 통과하기 쉬운데, 실데이터에는 NFD 라벨 149,286건 · 공백 민감 라벨 2,616건 ·
# 255자 초과 라벨 86건이 있어 전부 라이브 표시·조회를 바꾼다(2026-08-04 적대 검증 실측).
# node_id 와 label 은 조회 키라 한 글자만 달라져도 참조가 끊긴다.

def test_label_is_passed_through_without_trimming():
    row = _node(label="끝에 개행이 있다\n")
    _s, _t, _i, props = N.transform_node("p", row)
    assert props["label"] == "끝에 개행이 있다\n"
    assert props["name"] == "끝에 개행이 있다\n"


def test_label_is_not_truncated():
    long_label = "가" * 300
    _s, _t, _i, props = N.transform_node("p", _node(label=long_label))
    assert props["label"] == long_label and len(props["label"]) == 300


def test_label_unicode_form_is_preserved():
    """NFD(자모 분리) 라벨을 NFC 로 합치면 조회 키가 바뀐다 — 실데이터 149,286건."""
    nfd = "가".encode().decode()  # 기준
    import unicodedata
    decomposed = unicodedata.normalize("NFD", "가")
    assert decomposed != nfd
    _s, _t, _i, props = N.transform_node("p", _node(label=decomposed))
    assert props["label"] == decomposed


def test_empty_label_falls_back_to_node_id():
    """`or` 다 — 빈 문자열도 폴백한다. `is None` 으로 바꾸면 빈 label 이 그대로 나간다."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(id="n1", label="", space="policy", node_type="Policy"))
    assert props["name"] == "n1"


def test_node_type_override_lookup_is_case_sensitive():
    """대소문자 무시로 바꾸면 의도치 않은 타입이 remap 된다."""
    src = next(k for k in NODE_TYPE_OVERRIDE if k != k.lower())
    assert src.lower() not in NODE_TYPE_OVERRIDE
    assert N.resolve_node_space_type("concept", src) == NODE_TYPE_OVERRIDE[src]
    # 소문자 변형은 override 를 타지 않는다(미등록 타입 경로로 간다)
    assert N.resolve_node_space_type("concept", src.lower()) != NODE_TYPE_OVERRIDE[src]


def test_edge_label_lookup_does_not_strip_whitespace():
    """공백을 떼면 " has_part " 가 HAS_PART 로 붙어 방향까지 뒤집힌다."""
    a, rel, b, rev = N.resolve_edge(" has_part ", "resource", "concept")
    assert (a, rel, b, rev) == ("resource", " has_part ", "concept", False)


def test_edge_fallback_preserves_unicode_form():
    import unicodedata
    decomposed = unicodedata.normalize("NFD", "é")
    _a, rel, _b, _rev = N.resolve_edge(decomposed, "concept", "concept")
    assert rel == decomposed.lower()
    assert unicodedata.normalize("NFC", rel) != rel


def test_flatten_preserves_key_order():
    assert list(N.flatten_props({"z": 1, "a": 2})) == ["z", "a"]


def test_chroma_preserves_key_order():
    assert list(N.chroma_safe_meta({"z": 1, "a": 2})) == ["z", "a"]


def test_flatten_empty_dict_becomes_its_repr():
    """빈 dict 는 `""` 가 아니라 `"{}"` 다 — None 과 구분되어야 한다."""
    assert N.flatten_props({"d": {}})["d"] == "{}"
    assert N.flatten_props({"n": None})["n"] == ""


def test_flatten_keeps_empty_list_as_a_list():
    """빈 리스트는 스칼라 리스트다(all() 이 참) — 문자열화하지 않는다."""
    assert N.flatten_props({"l": []})["l"] == []


def test_chunk_source_move_overwrites_existing_source_doc():
    """pop 이라 항상 덮는다 — setdefault 로 바꾸면 원본 경로가 유실된다."""
    meta = N.transform_chunk_meta(
        "pack", {"metadata": {"source": "진짜경로.md", "source_doc": "낡은값"}})
    assert meta["source_doc"] == "진짜경로.md"


def test_blank_chunk_source_is_still_moved():
    """조건은 키 존재다 — truthiness 로 바꾸면 빈 source 가 pack 이름에 조용히 덮인다."""
    meta = N.transform_chunk_meta("pack", {"metadata": {"source": ""}})
    assert meta["source_doc"] == "" and meta["source"] == "pack"


def _dup_mapping(pairs):
    """중복 키를 내는 Mapping. dict 서브클래스로 만들면 빈 dict 라 falsy 가 되어
    `(d or {})` 에 먹히므로 비-dict 로 만든다."""
    class DupItems:
        def items(self):
            return list(pairs)
    return DupItems()


def test_duplicate_keys_from_a_mapping_take_the_last_value():
    """dict 입력으로는 관측 불가하지만 이 함수는 임의 Mapping 을 받는다.

    `out[k] = v` 를 `elif k not in out` 로 바꾸면 first-wins 가 되어, 중복 항목을 내는
    Mapping(정렬된 다중값, DB 커서 래퍼 등)에서 조용히 다른 값이 실린다.
    현재 계약은 last-wins 이며 그것이 dict 의미론과 일치한다.
    """
    scalars = _dup_mapping([("k", "첫값"), ("k", "끝값")])
    assert N.chroma_safe_meta(scalars)["k"] == "끝값"
    # 비스칼라 분기도 같은 규칙이어야 한다 — 한쪽만 보면 다른 분기의 first-wins 변이가 산다
    nonscalars = _dup_mapping([("k", {"a": 1}), ("k", {"b": 2})])
    assert N.chroma_safe_meta(nonscalars)["k"] == "{'b': 2}"
    # 스칼라 -> 비스칼라, 비스칼라 -> 스칼라 로 넘어가는 경우도 마지막이 이긴다
    mixed = _dup_mapping([("k", "첫값"), ("k", {"b": 2})])
    assert N.chroma_safe_meta(mixed)["k"] == "{'b': 2}"


# ── 타입·조건 경계 계약 ───────────────────────────────────────────────────────
#
# 아래는 "값의 타입과 조건 판정을 바꾸지 않는다"는 계약이다. 캐스팅과 조건 완화는
# 국소적으로는 무해해 보이지만 라이브 스키마 검증·조회 키·중복 판정을 바꾼다.

def test_original_type_is_recorded_only_when_it_changed():
    """`if original_type != node_type` 을 항상 참으로 바꾸면 remap 되지 않은 노드에도
    original_type 이 붙어 라이브 properties 가 통째로 달라진다."""
    _s, t, _i, props = N.transform_node("p", _node(space="resource", node_type="Document"))
    assert t == "Document" and "original_type" not in props


def test_degree_value_is_not_cast():
    """int() 캐스팅을 넣으면 문자열 degree 가 조용히 타입을 바꾸고, 캐스팅 불가값은 죽는다."""
    _s, _t, _i, props = N.transform_node("p", _node(degree="7"))
    assert props["degree"] == "7" and isinstance(props["degree"], str)


def test_collection_completeness_score_default_is_a_float():
    """int(1.0) 로 바꿔도 `== 1.0` 은 참이라 값 비교로는 안 잡힌다 — 타입까지 본다."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type="CollectionCompleteness"))
    assert props["score"] == 1.0 and isinstance(props["score"], float)


def test_node_id_type_is_preserved():
    """str() 로 감싸면 정수 id 가 문자열이 되어 id_map 조회가 통째로 어긋난다."""
    _s, _t, node_id, _p = N.transform_node("p", _node(id=12345))
    assert node_id == 12345 and isinstance(node_id, int)


@pytest.mark.parametrize("falsy", [None, [], "", 0])
def test_non_dict_properties_are_treated_as_empty(falsy):
    """`or {}` 다 — `get(..., {})` 로 바꾸면 falsy 비-dict 가 그대로 흘러 .items() 에서 죽거나
    엉뚱하게 해석된다."""
    _s, _t, _i, props = N.transform_node("p", _node(properties=falsy))
    assert props["pack_id"] == "p"      # 예외 없이 정상 변환된다
    assert "0" not in props


def test_row_document_id_beats_metadata():
    """행의 document_id 가 권위다 — 삽입 순서를 바꾸면 metadata 의 낡은 값이 이긴다."""
    meta = N.transform_chunk_meta(
        "p", {"metadata": {"document_id": "메타쪽"}, "document_id": "행쪽"})
    assert meta["document_id"] == "행쪽"


# ── 표 불변성 / 필수 필드 / 제어문자 ──────────────────────────────────────────

@pytest.mark.parametrize("name", ["REL_MAP", "LABEL_SPACE_OVERRIDE"])
def test_mapping_tables_are_mapping_proxies(name):
    """소비자 한 곳이 표를 바꾸면 적재기·게이트·리포트의 판정이 전역에서 오염된다.

    표를 정본으로 모은 목적이 "판정이 한 곳에서만 정해진다"이므로, 우회 검사가 아니라
    변형 가능성 자체를 없앤다(적대 검증 실증: REL_MAP_ISOLATED False, 2026-08-04).

    타입으로 **분기**하지 말고 타입을 **단언**한다 — 분기하면 표가 mutable set 으로
    바뀌었을 때 dict 쪽 가지로 빠져 엉뚱한 TypeError 로 통과한다(그렇게 썼다가 A7 을
    생존시켰다).
    """
    table = getattr(N, name)
    assert isinstance(table, MappingProxyType), f"{name} 이 {type(table).__name__} 이다"
    with pytest.raises(TypeError):
        table["오염"] = "값"          # type: ignore[index]


def test_reverse_relations_is_a_frozenset():
    assert isinstance(N.REVERSE_RELATIONS, frozenset), \
        f"{type(N.REVERSE_RELATIONS).__name__} 이다"
    with pytest.raises(AttributeError):
        N.REVERSE_RELATIONS.add("오염")   # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["NODE_TYPE_OVERRIDE", "SPACE_DEFAULT_TYPE"])
def test_schema_tables_are_mapping_proxies(name):
    """형제 경로 — 같은 패턴이라 함께 막는다."""
    from opencrab.pack import schema

    table = getattr(schema, name)
    assert isinstance(table, MappingProxyType), f"{name} 이 {type(table).__name__} 이다"
    with pytest.raises(TypeError):
        table["오염"] = ("concept", "Concept")


def test_missing_id_raises_rather_than_yielding_none():
    """`row["id"]` 다 — `.get()` 으로 바꾸면 node_id=None 노드가 조용히 만들어져
    id_map 과 엣지 endpoint 가 통째로 어긋난다. 없는 id 는 즉시 실패해야 한다."""
    row = {"label": "라벨", "space": "concept", "node_type": "Concept"}
    with pytest.raises(KeyError):
        N.transform_node("p", row)


def test_explicit_none_space_is_not_coerced():
    """`get(key, default)` 다 — falsy 폴백으로 바꾸면 None space 가 concept 으로 둔갑한다.
    키가 있는데 값이 None 인 것은 데이터 결함이며 조용히 메우면 원인이 사라진다."""
    space, _t, _i, _p = N.transform_node("p", _node(space=None, node_type="Concept"))
    assert space is None


def test_explicit_none_node_type_is_recorded_as_the_original():
    """None 은 grammar 미등록이라 space 기본형으로 귀착하되, **원본이 None 이었다는 사실**이
    original_type 에 남는다. falsy 폴백으로 바꾸면 node_type 이 애초에 "Concept" 이 되어
    original_type 키 자체가 사라지고, 데이터 결함이 흔적 없이 지워진다."""
    _s, node_type, _i, props = N.transform_node("p", _node(space="concept", node_type=None))
    assert node_type == "Concept"
    assert "original_type" in props and props["original_type"] is None


def test_control_characters_in_property_values_survive():
    """라벨뿐 아니라 properties 값에서도 NUL·제어문자를 제거하지 않는다."""
    _s, _t, _i, props = N.transform_node("p", _node(properties={"v": "c\x00d\n\t"}))
    assert props["v"] == "c\x00d\n\t"


def test_control_characters_survive_flatten_of_nested_values():
    assert N.flatten_props({"d": {"k": "a\x00b"}})["d"] == "{'k': 'a\\x00b'}"


# ── 자동채움 클래스: 기존 유효값을 절대 덮지 않는다 ──────────────────────────
#
# 아래 표는 transform_node 의 required·enum 자동채움 **전량**이다. 하나씩 손으로
# 검사하면 매번 인접한 것이 빠진다 — 실제로 여러 라운드에 걸쳐 그렇게 새어나갔다.
# 필드를 추가하면 이 표에 넣는다.

AUTOFILL_FIELDS = [
    ("resource", "Document", "title"),
    ("subject", "Team", "name"),
    ("subject", "Org", "name"),
    ("outcome", "Outcome", "name"),
    ("lever", "Lever", "name"),
    ("policy", "Policy", "name"),
    ("policy", "Policy", "rule_type"),
    ("subject", "User", "name"),
    ("subject", "User", "email"),
]


@pytest.mark.parametrize("space,node_type,field", AUTOFILL_FIELDS)
def test_autofill_never_overwrites_an_existing_value(space, node_type, field):
    """조건은 전부 `not props.get(field)` 다 — 키 이름을 바꾸거나 조건을 없애면
    팩이 준 값이 조용히 덮인다."""
    _s, t, _i, props = N.transform_node(
        "p", _node(space=space, node_type=node_type, label="라벨",
                   properties={field: "원래값"}))
    assert t == node_type
    assert props[field] == "원래값"


@pytest.mark.parametrize("space,node_type,field", AUTOFILL_FIELDS)
def test_autofill_fills_when_the_field_is_blank(space, node_type, field):
    _s, _t, _i, props = N.transform_node(
        "p", _node(space=space, node_type=node_type, label="라벨",
                   properties={field: ""}))
    assert props[field]


def test_user_email_is_not_marked_synthesized_when_supplied():
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="subject", node_type="User",
                   properties={"email": "real@example.com"}))
    assert props["email"] == "real@example.com"
    assert "email_synthesized" not in props


# ── enum 클래스: 유효값 튜플의 모든 원소를 존중한다 ──────────────────────────

@pytest.mark.parametrize("role", ["admin", "editor", "viewer", "agent", "analyst", "engineer"])
def test_every_valid_user_role_is_preserved(role):
    """튜플에서 한 원소만 빠져도 그 역할이 viewer 로 강등된다 — 전 원소를 돈다."""
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="subject", node_type="User", properties={"role": role}))
    assert props["role"] == role and "role_original" not in props


def test_absent_role_is_left_alone():
    _s, _t, _i, props = N.transform_node("p", _node(space="subject", node_type="User"))
    assert "role" not in props and "role_original" not in props


def test_absent_document_format_is_left_alone():
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="resource", node_type="Document"))
    assert "format" not in props and "format_original" not in props


# ── 기본값 클래스: 키가 아예 없을 때의 폴백 ──────────────────────────────────

def test_missing_space_defaults_to_concept():
    """`row.get("space", "concept")` — 기본값을 지우면 None 이 흘러 grammar 조회가 깨진다."""
    row = {"id": "n1", "label": "라벨", "node_type": "Concept"}
    space, node_type, _i, _p = N.transform_node("p", row)
    assert (space, node_type) == ("concept", "Concept")


def test_missing_node_type_defaults_to_concept_type():
    row = {"id": "n1", "label": "라벨", "space": "concept"}
    space, node_type, _i, props = N.transform_node("p", row)
    assert (space, node_type) == ("concept", "Concept")
    assert "original_type" not in props        # 기본값이므로 remap 이 아니다


@pytest.mark.parametrize("space", [None, "", "존재하지않는space"])
def test_unknown_space_does_not_crash_type_resolution(space):
    """`_GRAMMAR_SPACES.get(space, {})` 의 기본값을 지우면 None.get 으로 죽는다."""
    assert N.resolve_node_space_type(space, "Concept") == (space, "Concept")


# ── statement 폴백 클래스 ────────────────────────────────────────────────────

@pytest.mark.parametrize("node_type", ["Claim", "CollectionCompleteness", "Covariate"])
def test_statement_branch_covers_every_listed_type(node_type):
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type=node_type, label="라벨"))
    assert props["statement"] == "라벨"


def test_statement_fallback_order_is_text_then_description_then_label():
    both = N.transform_node("p", _node(
        space="claim", node_type="Claim", label="라벨",
        properties={"text": "본문", "description": "설명"}))[3]
    assert both["statement"] == "본문"
    desc_only = N.transform_node("p", _node(
        space="claim", node_type="Claim", label="라벨",
        properties={"description": "설명"}))[3]
    assert desc_only["statement"] == "설명"
    neither = N.transform_node("p", _node(
        space="claim", node_type="Claim", label="라벨", properties={}))[3]
    assert neither["statement"] == "라벨"


def test_existing_statement_is_not_replaced():
    _s, _t, _i, props = N.transform_node("p", _node(
        space="claim", node_type="Claim", label="라벨",
        properties={"statement": "원래 문장", "text": "본문"}))
    assert props["statement"] == "원래 문장"


def test_types_outside_the_statement_list_get_no_statement():
    """`and` 를 `or` 로 바꾸면 관계없는 노드에도 statement 가 생긴다."""
    _s, _t, _i, props = N.transform_node("p", _node(space="concept", node_type="Concept"))
    assert "statement" not in props


# ── resolve_edge 경계 ────────────────────────────────────────────────────────

def test_none_label_is_treated_as_empty():
    """`(label or "")` 의 기본값을 지우면 None.upper() 로 죽는다."""
    assert N.resolve_edge(None, "concept", "concept") == ("concept", "", "concept", False)


# ── 표 내용 계약 ──────────────────────────────────────────────────────────────
#
# 표는 판정의 절반이다. 함수만 검사하면 표 내용이 무검사로 남는다 — 전면 스윕이
# 표 리터럴까지 훑자 813종 중 576종이 생존했다(2026-08-05). 손으로 196개 항목을
# 다 쓸 수는 없으므로 두 축으로 닫는다:
#   ① 구조 불변식 — 존재하지 않는 relation·node_type 으로 바뀌면 즉시 걸린다.
#      실측 위반 0 이라 보편 단언이 가능하다.
#   ② 내용 지문 — ①을 통과하는 **유효값끼리의 교체**(part_of -> related_to)를 잡는다.
#      표를 고치는 것은 라이브 판정을 바꾸는 일이라, 지문 갱신이라는 의도적 행위를 강제한다.

def _fingerprint(table):
    import hashlib
    import json

    if isinstance(table, frozenset):
        payload = sorted(table)
    else:
        payload = sorted((repr(k), repr(v)) for k, v in table.items())
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


TABLE_FINGERPRINTS = {
    "REL_MAP": ("a269d2029ccf067947cb468fb8880d4e453cb5c5a6f5b0f5c71641f3fbed3516", 55),
    "REVERSE_RELATIONS": ("b9ac61265dba3b6a955bbb4178d6fde0692b2443e53740075f1ba894fc1b012b", 3),
    "LABEL_SPACE_OVERRIDE": ("5321b1bf4f7fdbac2cd7d17885b277db37da2ea89953d5851a679101e5bd5d93", 48),
    "NODE_TYPE_OVERRIDE": ("a78048f4610c345c16416bf2c2faf603787eeda39528e328eb63824f5f1c0b36", 81),
    "SPACE_DEFAULT_TYPE": ("281b8aa6600302f054040006c7402bdbcee2d9cfa9bc6fcabe7fd102ee4f1398", 9),
}


def _table(name):
    from opencrab.pack import schema

    return getattr(N, name, None) or getattr(schema, name)


@pytest.mark.parametrize("name", sorted(TABLE_FINGERPRINTS))
def test_table_content_is_pinned(name):
    """표를 고치면 여기서 걸린다 — 지문과 항목 수를 함께 갱신하라.

    갱신은 "라이브 적재 판정을 바꾼다"는 뜻이므로, 무엇이 왜 바뀌는지 커밋에 적을 것.
    """
    want_fp, want_len = TABLE_FINGERPRINTS[name]
    table = _table(name)
    assert len(table) == want_len, f"{name} 항목 수 {len(table)} != {want_len}"
    assert _fingerprint(table) == want_fp, f"{name} 내용이 바뀌었다"


# ── REL_MAP 의미 앵커 ────────────────────────────────────────────────────────
#
# 적대 검증이 지문의 성격을 정확히 규정했다(2026-08-05): 구조 불변식은 **존재하지 않는**
# relation·orphan reverse 키를 실제로 잡지만(장식이 아니다), **유효하지만 다른** 값으로의
# 교체(`part_of` -> `related_to`)는 지문만 잡는다. 그리고 지문은 기대값을 함께 고치면
# 통과하므로 변경 감지이지 의미 보존 증명이 아니다.
#
# 그 틈을 여기서 좁힌다. 다만 **과장하지 않는다** — 판단표의 의미가 옳다는 것을 단위
# 테스트가 독립적으로 증명할 수는 없다. 그 역할은 opencrab-dump 의 128 팩 characterization
# (엣지 779,636 건 재계산)이 한다. 여기서 얻는 것은 세 가지다:
#   ① 표를 읽지 않고 `resolve_edge` **동작**으로 확인한다 — 표가 그대로여도 조회 논리가
#      바뀌면 걸린다.
#   ② 항등(대문자→소문자 정규화) 34 건과 판단 21 건으로 **전수 분할**한다. 키가 새로
#      생기면 반드시 어느 한쪽에 등록해야 하므로 조용히 늘어날 수 없다.
#   ③ 판단 매핑은 값을 명시해 둔다. 리뷰어가 sha 대신 "이 라벨은 왜 이 relation 인가"를
#      본다.

# raw 라벨을 소문자로 내린 것과 결과가 **다른** 매핑 = 사람이 내린 판단.
# 코퍼스 실측(2026-08-05, by-pack 128팩): CONSTRAINS 1,360건/9팩, HAS_SESSION 659건/5팩,
# ACHIEVES 495건/44팩, AFFECTS 461건/34팩. Honda GraphRAG 계열 7키(HAS_ASSEMBLY 등)는
# 현재 by-pack 에 출현 0 이며 graphrag 임포트 경로 전용이다.
JUDGMENT_MAPPINGS = {
    "ACHIEVES": "contributes_to",
    "AFFECTS": "optimizes",
    "CONSTRAINS": "governs",
    "CONTAINS_TURN": "describes",
    "DEPENDS_ON": "related_to",
    "ENABLES": "contributes_to",
    "EVIDENCES": "contains",
    "HAS_ASSEMBLY": "part_of",
    "HAS_CHUNK": "contains",
    "HAS_PART": "part_of",
    "HAS_PART_NUMBER": "related_to",
    "HAS_RESOURCE": "owns",
    "HAS_SESSION": "contains",
    "HAS_TOKEN_CATEGORY": "mentions",
    "INSTANCE_OF": "subclass_of",
    "PREDICTS": "related_to",
    "REQUIRES_APPROVAL": "governs",
    "SHOWN_IN": "mentions",
    "STABILIZES": "optimizes",
    "USED_IN_MODEL": "part_of",
    "USES_CANONICAL_PART": "related_to",
}


def test_rel_map_partitions_into_identity_and_judgment():
    """분할이 전수인지. 키가 늘면 어느 한쪽에 등록해야 하므로 조용히 못 늘어난다."""
    identity = {k for k, v in N.REL_MAP.items() if k.lower() == v}
    judgment = {k for k, v in N.REL_MAP.items() if k.lower() != v}
    assert identity & judgment == set()
    assert identity | judgment == set(N.REL_MAP)
    assert judgment == set(JUDGMENT_MAPPINGS), \
        "판단 매핑이 늘거나 줄었다 — JUDGMENT_MAPPINGS 에 근거와 함께 등록하라"
    assert len(identity) == 34


def _pair_without_override(label):
    """이 라벨이 override 에 가려지지 않는 공간쌍. 없으면 **실패시킨다**.

    처음에는 ("resource", "concept") 로 고정하고 가려지면 skip 했다. 그런데 그러면
    DESCRIBES·PART_OF·SCOPES 세 항등키가 조용히 무검사로 빠진다 — 지금 닫고 있는
    "조용한 미검사" 클래스 그 자체라, 가려지지 않는 쌍을 찾아 반드시 검사한다.
    """
    spaces = sorted(ALL_SPACES)
    for frm in spaces:
        for to in spaces:
            if (label, frm, to) not in N.LABEL_SPACE_OVERRIDE:
                return frm, to
    raise AssertionError(f"{label}: 전 공간쌍이 override 라 REL_MAP 경로를 못 탄다")


@pytest.mark.parametrize("label", sorted(JUDGMENT_MAPPINGS))
def test_judgment_mapping_survives_through_resolve_edge(label):
    """표를 읽지 않고 **동작**으로 확인한다 — 표가 그대로여도 조회 논리가 바뀌면 걸린다."""
    frm, to = _pair_without_override(label)
    want = JUDGMENT_MAPPINGS[label]
    got = N.resolve_edge(label, frm, to)
    if label in N.REVERSE_RELATIONS:
        assert got == (to, want, frm, True)
    else:
        assert got == (frm, want, to, False)


@pytest.mark.parametrize("label", sorted(
    k for k, v in N.REL_MAP.items() if k.lower() == v))
def test_identity_mapping_is_exactly_lowercasing(label):
    """항등 매핑은 대소문자 정규화일 뿐이다 — 다른 relation 으로 새면 안 된다."""
    frm, to = _pair_without_override(label)
    assert N.resolve_edge(label, frm, to)[1] == label.lower()


def test_every_rel_map_value_is_a_real_grammar_relation():
    from opencrab.pack.schema import ALLOWED

    known = {r for rels in ALLOWED.values() for r in rels}
    unknown = sorted({v for v in N.REL_MAP.values() if v not in known})
    assert not unknown, f"grammar 에 없는 relation: {unknown}"


def test_every_override_resolves_to_a_grammar_valid_edge():
    """반전까지 적용한 뒤의 (from, to, relation) 이 문법에 있어야 한다.

    없으면 그 라벨의 엣지가 적재 시 전량 skip 된다 — 표에 오타 하나로 팩이 반쪽 적재된다.
    """
    from opencrab.pack.schema import ALLOWED

    bad = []
    for (label, fs, ts), (relation, reverse) in N.LABEL_SPACE_OVERRIDE.items():
        a, b = (ts, fs) if reverse else (fs, ts)
        if relation not in ALLOWED.get((a, b), ()):
            bad.append((label, fs, ts, relation, reverse))
    assert not bad, f"grammar 무효 override: {bad}"


def test_every_reverse_relation_is_declared_in_rel_map():
    """REL_MAP 에 없는 라벨을 반전 목록에만 넣으면 lowercase 폴백과 반전이 엇갈린다."""
    orphan = sorted(label for label in N.REVERSE_RELATIONS if label not in N.REL_MAP)
    assert not orphan, f"REL_MAP 미등록 반전 라벨: {orphan}"


@pytest.mark.parametrize("name", ["NODE_TYPE_OVERRIDE", "SPACE_DEFAULT_TYPE"])
def test_every_type_table_target_is_valid_in_its_space(name):
    bad = [(k, v) for k, v in _table(name).items()
           if v[1] not in N._GRAMMAR_SPACES.get(v[0], {}).get("node_types", ())]
    assert not bad, f"{name} 의 무효 (space, node_type): {bad}"


# ── 호출 대상 계약 ────────────────────────────────────────────────────────────

def test_legacy_absorption_uses_flatten_not_chroma_semantics():
    """흡수 경로도 flatten_props 여야 한다.

    chroma_safe_meta 로 바꿔도 대부분 같은 결과라 안 보인다 — 두 함수가 갈리는 것은
    **스칼라 리스트**(flatten 은 유지, chroma 는 문자열화)와 **None**(flatten 은 "",
    chroma 는 키 제거)뿐이다. 그 두 값으로만 구분된다(2026-08-05 적대 검증 실증).
    """
    _s, _t, _i, props = N.transform_node(
        "p", _node(legacy_list=[1, 2], legacy_none=None, properties={}))
    assert props["legacy_list"] == [1, 2]
    assert props["legacy_none"] == ""


def test_chunk_meta_uses_chroma_not_flatten_semantics():
    """반대 방향 — 청크 메타는 chroma 의미론이라 None 키가 사라지고 리스트가 문자열이 된다."""
    meta = N.transform_chunk_meta("p", {"metadata": {"xs": [1, 2], "n": None}})
    assert meta["xs"] == "[1, 2]"
    assert "n" not in meta
