"""by-pack 계약(opencrab.pack.schema) 테스트.

이 계약이 없어서 실제로 난 사고 두 건을 회귀 검사로 고정한다.
  - 노드 커스텀 필드가 최상위에 펼쳐져 91만 건이 라이브에 도달하지 못한 건
  - grammar 표 사본이 정본과 드리프트할 수 있던 구조
"""

import pytest

from opencrab.grammar.manifest import META_EDGES
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


class TestValidateNode:
    def test_minimal_valid_node(self):
        validate_node(_node())

    @pytest.mark.parametrize("key", ["id", "label", "node_type", "space"])
    def test_missing_required_field_rejected(self, key):
        row = _node()
        del row[key]
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_node(row)

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
