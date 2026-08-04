"""opencrab.pack.normalize — 순수 정규화 계층 계약.

여기서 검사하는 것은 "적재기와 게이트가 같은 판정을 쓴다"는 계약 자체다.
표가 아니라 **판정 함수**를 검사한다 — 표만 맞고 순서가 다르면 게이트 통과가
적재 성공을 뜻하지 않게 되고, 그것이 2026-08-04 이관의 이유였다.
"""

import pytest

from opencrab.pack import normalize
from opencrab.pack.schema import NODE_STRUCT_KEYS, NODE_TYPE_OVERRIDE, SPACE_DEFAULT_TYPE

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


def test_struct_keys_are_not_absorbed():
    _s, _t, _i, props = N.transform_node("p", _node(degree=7, source_type="x"))
    assert "source_type" not in props
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


def test_collection_completeness_gets_valid_status_and_score():
    """status 는 enum(pass/retry/fail) — 위반값이 들어가면 add_node 가 skip 된다."""
    _s, t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type="CollectionCompleteness",
                   properties={"status": "이상한값"}))
    assert t == "CollectionCompleteness"
    assert props["status"] == "pass" and props["score"] == 1.0


def test_collection_completeness_keeps_valid_status():
    _s, _t, _i, props = N.transform_node(
        "p", _node(space="claim", node_type="CollectionCompleteness",
                   properties={"status": "retry", "score": 0.5}))
    assert props["status"] == "retry" and props["score"] == 0.5


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


def test_missing_metadata_is_tolerated():
    assert N.transform_chunk_meta("p", {}) == {"pack_id": "p", "source": "p"}


def test_transform_chunk_meta_is_deterministic():
    row = {"metadata": {"char_start": 0}, "document_id": "d"}
    assert N.transform_chunk_meta("p", row) == N.transform_chunk_meta("p", row)
