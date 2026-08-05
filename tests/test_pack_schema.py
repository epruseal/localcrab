"""팩 소스 계약(opencrab.pack.schema) 테스트.

이 계약이 없어서 실제로 난 사고 두 건을 회귀 검사로 고정한다.
  - 노드 커스텀 필드가 최상위에 펼쳐져 91만 건이 라이브에 도달하지 못한 건
  - grammar 표 사본이 정본과 드리프트할 수 있던 구조
"""

import pathlib
import re
from unittest import mock

import pytest

from opencrab.grammar import manifest
from opencrab.grammar.manifest import META_EDGES
from opencrab.pack import schema
from opencrab.pack.schema import (
    ALL_SPACES,
    ALLOWED,
    CHUNK_STRUCT_KEYS,
    EDGE_STRUCT_KEYS,
    FIX,
    KEEP,
    NODE_STRUCT_KEYS,
    RESERVED_NODE_KEYS,
    TRACE_SRC,
    PackSchemaError,
    absorb_legacy_top_level,
    check_grammar_tables,
    stray_top_level_keys,
    validate_chunk,
    validate_edge,
    validate_node,
    validate_node_props,
)


def _node(**over):
    row = {
        "id": "n1", "workspace_id": "pack", "label": "L", "node_type": "Concept",
        "space": "concept", "source_type": "reference-public",
        "created_at": "2026-08-04T00:00:00+00:00",
    }
    row.update(over)
    return row


def _edge(**over):
    row = {
        "id": "e1", "workspace_id": "pack", "source_id": "n1", "target_id": "n2",
        "label": "related_to", "created_at": "2026-08-04T00:00:00+00:00",
        "properties": {},
    }
    row.update(over)
    return row


def _chunk(**over):
    row = {
        "id": "c1", "document_id": "d1", "workspace_id": "pack", "text": "t",
        "source": "d1", "source_type": "reference-public",
        "created_at": "2026-08-04T00:00:00+00:00", "metadata": {},
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# grammar 표는 정본에서 유도된다 (스냅샷 드리프트 불가)
# ---------------------------------------------------------------------------

class TestGrammarDerivation:
    def test_allowed_is_derived_from_manifest_exactly(self):
        expected = {(e["from_space"], e["to_space"]): frozenset(e["relations"])
                    for e in META_EDGES}
        assert ALLOWED == expected

    def test_allowed_covers_every_manifest_edge(self):
        assert len(ALLOWED) == len(META_EDGES)

    def test_fix_keys_match_allowed_keys(self):
        """FIX 와 ALLOWED 의 공간쌍 집합이 정확히 같아야 한다.

        어긋나면 생산자가 정합 불가 공간쌍에서 KeyError 를 내거나(FIX 누락),
        존재하지 않는 공간쌍에 대표값을 들고 있게 된다(ALLOWED 누락).
        """
        assert set(FIX) == set(ALLOWED)

    def test_every_fix_value_is_an_allowed_relation(self):
        bad = {k: v for k, v in FIX.items() if v not in ALLOWED[k]}
        assert bad == {}

    def test_keep_pairs_are_traceability_reversals(self):
        """KEEP 은 적재기가 방향을 뒤집는 공간쌍이다. 역방향이 grammar 에 있어야 한다."""
        for (src, tgt) in KEEP:
            assert (tgt, src) in ALLOWED, f"{src}->{tgt} 의 역방향이 grammar 에 없다"

    def test_trace_src_spaces_are_real_spaces(self):
        assert TRACE_SRC <= set(ALL_SPACES)

    def test_all_spaces_matches_manifest_spaces(self):
        used = {s for pair in ALLOWED for s in pair}
        assert used <= set(ALL_SPACES)


class TestGrammarTableGuard:
    """import 시점 자기정합성 가드. 무력화해도 아무 테스트가 안 죽었다(변이 생존).

    위 TestGrammarDerivation 이 표 자체는 지키므로 불변식은 보호되지만, 가드가
    사문이 되는 것은 별개 문제다 — 여기서 가드의 세 분기를 직접 건다.
    """

    def test_current_tables_pass(self):
        check_grammar_tables(ALLOWED, FIX)

    def test_fix_pair_absent_from_allowed_is_rejected(self):
        fix = dict(FIX)
        fix[("concept", "존재하지않는공간")] = "related_to"
        with pytest.raises(RuntimeError, match="manifest 에 없는 공간쌍"):
            check_grammar_tables(ALLOWED, fix)

    def test_fix_value_outside_allowed_set_is_rejected(self):
        fix = dict(FIX)
        fix[("concept", "concept")] = "아무거나"
        with pytest.raises(RuntimeError, match="허용 집합 밖"):
            check_grammar_tables(ALLOWED, fix)

    def test_allowed_pair_without_fix_default_is_rejected(self):
        """manifest 에 공간쌍이 추가됐는데 FIX 를 안 고친 경우 — 실제로 잦은 함정."""
        allowed = dict(ALLOWED)
        allowed[("concept", "subject")] = frozenset({"related_to"})
        with pytest.raises(RuntimeError, match="FIX 대표값이 없다"):
            check_grammar_tables(allowed, FIX)

    def test_the_guard_is_actually_invoked_at_import_time(self):
        """**호출부**를 건다. 함수만 검사하면 호출을 지워도 아무도 모른다.

        적대 검증 실측: `check_grammar_tables(ALLOWED, FIX)` 호출 한 줄을 주석 처리해도
        스위트 전량이 통과했다. 가드를 함수로 승격한 목적이 "사문화를 막는 것"인데
        정작 그 목적이 반만 달성돼 있었다.

        manifest 에 FIX 가 모르는 공간쌍을 끼운 채 schema 소스를 실행하면 import 가
        실패해야 한다 — 호출이 살아 있어야만 그렇게 된다.
        """
        import opencrab.pack.schema as schema_mod
        src = pathlib.Path(schema_mod.__file__).read_text(encoding="utf-8")
        extra = dict(from_space="concept", to_space="subject",
                     relations=["related_to"], description="테스트용 주입")
        ns = {"__name__": "opencrab.pack.schema_probe", "__file__": schema_mod.__file__}
        with mock.patch.object(manifest, "META_EDGES", [*manifest.META_EDGES, extra]):
            with pytest.raises(RuntimeError, match="FIX 대표값이 없다"):
                exec(compile(src, schema_mod.__file__, "exec"), ns)


# ---------------------------------------------------------------------------
# 노드 props 규약 — 91만 필드 사고의 회귀 검사
# ---------------------------------------------------------------------------

class TestValidateNodeProps:
    def test_flat_props_pass(self):
        validate_node_props("n1", {"mst": "268103", "year": 2019})

    def test_none_and_empty_pass(self):
        validate_node_props("n1", None)
        validate_node_props("n1", {})

    def test_pre_wrapped_props_rejected(self):
        """이미 감싼 형태를 넘기면 properties.properties 가 되어 한 겹 깊어진다."""
        with pytest.raises(PackSchemaError, match="평면 dict"):
            validate_node_props("n1", {"properties": {"mst": "1"}})

    @pytest.mark.parametrize("key", sorted(RESERVED_NODE_KEYS))
    def test_every_reserved_key_is_rejected(self, key):
        with pytest.raises(PackSchemaError, match="예약 키"):
            validate_node_props("n1", {key: "x"})

    def test_source_type_is_deliberately_not_reserved(self):
        """팩 기본값을 노드별로 덮는 것은 의도된 기능이다(생성본 self-authored 구분)."""
        assert "source_type" not in RESERVED_NODE_KEYS
        validate_node_props("n1", {"source_type": "self-authored"})

    def test_error_message_names_the_offending_keys(self):
        with pytest.raises(PackSchemaError) as ei:
            validate_node_props("n1", {"id": "x", "label": "y"})
        assert "'id'" in str(ei.value) and "'label'" in str(ei.value)


class TestDiagnosticIdentifiesTheFailingRow:
    """필수 필드 오류 메시지는 **어느 행이 실패했는지**를 말해야 한다.

    세 검사의 메시지가 `{row.get('id')!r}` 로 실패 행의 id 를 붙인다. 그 `'id'` 를 빈
    문자열로 바꾸는 변이가 세 자리 전부에서 살아남았다(2026-08-05 스윕).

    처음엔 "예외 종류·발생이 같으니 표시 문구 전용 등가"로 판정했는데 **틀렸다.** id 가 없는
    행만 입력으로 썼기 때문이다. id 가 **있고** 다른 필드가 빠진 행에서는 갈린다:

        원본: 노드에 필수 필드 'label' 가 없다: 'node-1'
        변이: 노드에 필수 필드 'label' 가 없다: None

    운영자가 수만 행 중 어느 것이 문제인지 잃는다. **등가를 측정할 때도 입력이 그 변이가
    건드리는 축을 갈라야 한다** — 이 실수를 등가 판정에서 세 번 반복했다(적대 검증 지적).
    """

    @pytest.mark.parametrize("validator,builder,rid,missing", [
        (validate_node, _node, "node-1", "label"),
        (validate_edge, _edge, "edge-1", "source_id"),
        (validate_chunk, _chunk, "chunk-1", "document_id"),
        # id 와 빠진 필드 이름이 **같은** 경우. 아래 정규식이 위치를 고정하지 않으면
        # 두 단언이 서로의 조각으로 충족돼 한쪽이 사라져도 통과한다(적대 검증 지적).
        (validate_node, _node, "label", "label"),
    ])
    def test_message_carries_the_row_id(self, validator, builder, rid, missing):
        row = builder()
        row["id"] = rid
        del row[missing]
        with pytest.raises(PackSchemaError) as ei:
            validator(row)
        # **위치를 고정한다.** `x in msg` 두 개로 나누면 rid == missing 일 때 각 단언이
        # 상대방 조각으로 충족된다 — 한쪽을 지워도 통과하는 구멍이 생긴다.
        # "필수 필드 <빠진필드> 가 없다: <행id>" 순서를 통째로 요구한다.
        want = rf"필수 필드 {re.escape(repr(missing))} 가 없다: {re.escape(repr(rid))}"
        assert re.search(want, str(ei.value)), \
            f"진단이 (빠진 필드, 실패 행 id) 를 그 순서로 담아야 한다: {ei.value}"


class TestValidateNode:
    def test_minimal_valid_node(self):
        validate_node(_node())

    @pytest.mark.parametrize("key", ["id", "label", "node_type", "space"])
    def test_missing_required_field_rejected(self, key):
        row = _node()
        del row[key]
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_node(row)

    @pytest.mark.parametrize("key", ["id", "label", "node_type", "space"])
    def test_empty_string_required_field_rejected(self, key):
        """`if not row.get(key)` 를 `if key not in row` 로 바꿔도 생존했다(변이).

        빈 문자열 id 는 dangling 검사를 통과하면서 라이브에 유령 노드를 만든다 —
        "키가 있다"가 아니라 "값이 있다"가 계약이다.
        """
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_node(_node(**{key: ""}))

    def test_space_outside_nine_space_rejected(self):
        with pytest.raises(PackSchemaError, match="9-space"):
            validate_node(_node(space="nonsense"))

    def test_non_dict_properties_rejected(self):
        with pytest.raises(PackSchemaError, match="dict"):
            validate_node(_node(properties=["a"]))

    def test_legacy_top_level_allowed_by_default(self):
        """디스크에 91만 건이 남아 있다. 기본값이 이를 거부하면 기존 팩이 전부 죽는다."""
        validate_node(_node(brand="Yamaha"))

    def test_legacy_top_level_rejected_in_strict_mode(self):
        with pytest.raises(PackSchemaError, match="비구조 키"):
            validate_node(_node(brand="Yamaha"), allow_legacy_top_level=False)

    def test_degree_is_structural_not_stray(self):
        """degree 는 생산자가 안 쓰지만 소비자가 구조 키로 다룬다.
        집합이 어긋나면 빌드는 죽는데 게이트는 통과하는 비대칭이 생긴다."""
        assert "degree" in NODE_STRUCT_KEYS
        validate_node(_node(degree=3), allow_legacy_top_level=False)


class TestValidateEdge:
    def test_minimal_valid_edge(self):
        validate_edge(_edge())

    @pytest.mark.parametrize("key", ["id", "source_id", "target_id", "label"])
    def test_missing_required_field_rejected(self, key):
        row = _edge()
        del row[key]
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_edge(row)

    def test_stray_top_level_rejected_no_legacy_path(self):
        """엣지에는 레거시 흡수 경로가 없다 — 전수 실측에서 0건이므로 관용하지 않는다."""
        with pytest.raises(PackSchemaError, match="비구조 키"):
            validate_edge(_edge(weight=1))

    def test_non_dict_properties_rejected(self):
        with pytest.raises(PackSchemaError, match="dict"):
            validate_edge(_edge(properties="x"))


class TestValidateChunk:
    def test_minimal_valid_chunk(self):
        validate_chunk(_chunk())

    @pytest.mark.parametrize("key", ["id", "document_id", "text"])
    def test_missing_required_field_rejected(self, key):
        row = _chunk()
        del row[key]
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_chunk(row)

    def test_empty_text_is_valid(self):
        """빈 청크는 계약 위반이 아니다 — 필드 부재만 위반이다."""
        validate_chunk(_chunk(text=""))

    def test_stray_top_level_rejected(self):
        with pytest.raises(PackSchemaError, match="비구조 키"):
            validate_chunk(_chunk(page=3))

    def test_non_dict_metadata_rejected(self):
        with pytest.raises(PackSchemaError, match="dict"):
            validate_chunk(_chunk(metadata=[1]))


# ---------------------------------------------------------------------------
# 레거시 흡수 규칙 — 생산자와 소비자가 같은 함수를 봐야 한다
# ---------------------------------------------------------------------------

class TestAbsorbLegacyTopLevel:
    def test_nested_only(self):
        assert absorb_legacy_top_level(_node(properties={"a": 1})) == {"a": 1}

    def test_top_level_only_is_absorbed(self):
        assert absorb_legacy_top_level(_node(brand="Yamaha")) == {"brand": "Yamaha"}

    def test_nested_wins_over_top_level(self):
        """정본 위치가 중첩이다. 전수 실측에서 충돌 0건이라 현재 데이터에선 무손실."""
        row = _node(brand="TopLevel", properties={"brand": "Nested"})
        assert absorb_legacy_top_level(row) == {"brand": "Nested"}

    def test_both_positions_merge(self):
        row = _node(brand="Yamaha", properties={"year": 2019})
        assert absorb_legacy_top_level(row) == {"brand": "Yamaha", "year": 2019}

    def test_structural_keys_never_absorbed(self):
        """id/label/space 등이 properties 로 새면 라이브 노드에 중복 키가 생긴다."""
        got = absorb_legacy_top_level(_node(degree=7))
        assert got == {}

    def test_does_not_mutate_input(self):
        row = _node(brand="Yamaha", properties={"year": 2019})
        before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in row.items()}
        absorb_legacy_top_level(row)
        assert row == before

    @pytest.mark.parametrize("stray", [{}, {"brand": "Yamaha"}])
    def test_result_is_independent_of_input(self, stray):
        """방어 복사를 제거해도 생존했다(변이) — 반환값이 row 를 별칭하는 경로가 있었다.

        stray 가 없으면 예전 구현은 `row["properties"]` 를 그대로 돌려줄 수 있었고,
        호출자가 그 dict 에 pack_id 를 넣는 순간 원본 행이 오염된다. 적재기가 정확히
        그렇게 쓴다(`props["pack_id"] = pack_name`).
        """
        row = _node(properties={"year": 2019}, **stray)
        got = absorb_legacy_top_level(row)
        got["pack_id"] = "오염"
        assert "pack_id" not in row["properties"]
        assert "pack_id" not in row

    def test_missing_properties_key(self):
        row = _node()
        row.pop("properties", None)
        assert absorb_legacy_top_level(row) == {}

    def test_null_properties_treated_as_empty(self):
        assert absorb_legacy_top_level(_node(properties=None, brand="Y")) == {"brand": "Y"}


class TestStrayTopLevelKeys:
    def test_returns_only_non_structural(self):
        assert stray_top_level_keys(_node(brand="Y", year=1)) == {"brand": "Y", "year": 1}

    def test_clean_node_has_none(self):
        assert stray_top_level_keys(_node()) == {}

    def test_agrees_with_absorb_when_nested_empty(self):
        row = _node(brand="Y", year=1)
        assert stray_top_level_keys(row) == absorb_legacy_top_level(row)


# ---------------------------------------------------------------------------
# 구조 키 집합 자체
# ---------------------------------------------------------------------------

class TestStructKeySets:
    def test_reserved_is_subset_of_node_struct(self):
        assert RESERVED_NODE_KEYS <= NODE_STRUCT_KEYS

    def test_properties_is_structural_for_nodes(self):
        assert "properties" in NODE_STRUCT_KEYS

    def test_edge_and_chunk_carry_their_own_bag(self):
        assert "properties" in EDGE_STRUCT_KEYS
        assert "metadata" in CHUNK_STRUCT_KEYS


# ── 표 내용 계약 ──────────────────────────────────────────────────────────────
#
# 표는 계약의 절반이다. 함수만 검사하면 표 내용이 무검사로 남는다 — 전면 스윕
# (scripts/qa/mutate_module.py)이 이 모듈을 훑자 814종 중 451종이 표 리터럴에서
# 생존했다(2026-08-05). 항목을 하나씩 쓸 수 없으므로 지문으로 닫고, 표를 고치는 것이
# 라이브 판정을 바꾸는 일임을 지문 갱신이라는 의도적 행위로 강제한다.
# (normalize 쪽 표는 tests/test_pack_normalize.py 가 같은 방식으로 고정한다.)

def _table_fingerprint(table):
    import hashlib
    import json

    if isinstance(table, frozenset):
        payload = sorted(table)
    else:
        payload = sorted((repr(k), repr(v)) for k, v in table.items())
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


SCHEMA_TABLE_FINGERPRINTS = {
    "NODE_STRUCT_KEYS": ("9ce69c22b6b723499951407d74f1515441ff4b8bc8dca64154a5b9c70fdd9f99", 9),
    "EDGE_STRUCT_KEYS": ("1e8da7546c7e48c6f96687249188e67e44f6ffe8239417a5bcc28c7a5f48b08f", 7),
    "CHUNK_STRUCT_KEYS": ("829b995bce491b903710d6c2108dfaaff8d601b883ccd88c66f920da17282adc", 8),
    "RESERVED_NODE_KEYS": ("1b9148cbae0578281a7048f14c3c06b3539f1c6a054dd6c501790c59bf16917f", 6),
    "FIX": ("47f96c7baeca4d70057b1604eab12ed5733635d3b77385721f1e22aeaa6b84b8", 38),
    "KEEP": ("cf38ac073e29544ca91a144972958f5ed4170d15b5e7fb02774eb3f36f65bc66", 1),
    "TRACE_SRC": ("939516f210f90597d0b95b57b52efb64aa89952b5eaf181e844d23865a83d273", 4),
    "NODE_TYPE_OVERRIDE": ("a78048f4610c345c16416bf2c2faf603787eeda39528e328eb63824f5f1c0b36", 81),
    "SPACE_DEFAULT_TYPE": ("281b8aa6600302f054040006c7402bdbcee2d9cfa9bc6fcabe7fd102ee4f1398", 9),
}


@pytest.mark.parametrize("name", sorted(SCHEMA_TABLE_FINGERPRINTS))
def test_schema_table_content_is_pinned(name):
    """표를 고치면 여기서 걸린다 — 지문과 항목 수를 함께 갱신하고 사유를 커밋에 남겨라."""
    want_fp, want_len = SCHEMA_TABLE_FINGERPRINTS[name]
    table = getattr(schema, name)
    assert len(table) == want_len, f"{name} 항목 수 {len(table)} != {want_len}"
    assert _table_fingerprint(table) == want_fp, f"{name} 내용이 바뀌었다"


def test_stray_check_uses_stray_keys_not_the_absorb_result():
    """`stray_top_level_keys` 를 `absorb_legacy_top_level` 로 바꿔도 대부분 안 보인다.

    갈리는 것은 **최상위 비구조 키가 없고 중첩 properties 만 있는** 행이다 —
    absorb 는 그 중첩 내용을 돌려주므로 truthy 가 되어 정상 행이 계약 위반으로 거절된다.
    엄격 모드(allow_legacy_top_level=False)로 새 팩을 검사할 때 전건이 튕긴다.
    """
    row = {"id": "n1", "label": "라벨", "node_type": "Concept", "space": "concept",
           "properties": {"custom": "값"}}
    schema.validate_node(row, allow_legacy_top_level=False)   # 예외 없이 통과해야 한다
    assert schema.stray_top_level_keys(row) == {}
    assert schema.absorb_legacy_top_level(row) == {"custom": "값"}
