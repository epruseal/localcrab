"""팩 소스 계약(opencrab.pack.schema) 테스트.

이 계약이 없어서 실제로 난 사고 두 건을 회귀 검사로 고정한다.
  - 노드 커스텀 필드가 최상위에 펼쳐져 91만 건이 라이브에 도달하지 못한 건
  - grammar 표 사본이 정본과 드리프트할 수 있던 구조
"""

import itertools
import pathlib
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

    @pytest.mark.parametrize("validator,builder,kind,rid,missing", [
        (validate_node, _node, "노드", "node-1", "label"),
        (validate_edge, _edge, "엣지", "edge-1", "source_id"),
        (validate_chunk, _chunk, "청크", "chunk-1", "document_id"),
        # id 와 빠진 필드 이름이 **같은** 경우. 순서를 고정하지 않으면 두 단언이 서로의
        # 조각으로 충족돼 한쪽이 사라져도 통과한다(적대 검증 지적).
        (validate_node, _node, "노드", "label", "label"),
        # id 에 **정규식 메타문자**와 **따옴표**가 든 경우. 이게 없으면 아래 `re.escape` 를
        # 지워도 전부 통과한다 — 즉 escape 의 필요성이 검증되지 않는다(적대 검증 실증).
        (validate_node, _node, "노드", r"row[1].*+?", "label"),
        (validate_node, _node, "노드", "it's \"quoted\"", "label"),
        # id 가 **문자열이 아닌** 경우. 이게 없으면 `row_id` 를 문자열일 때만 유지하는
        # 변이가 세 검사 전부에서 살아남는다(적대 검증 실증, 2026-08-10: W3).
        # `{"id": 12345}` 는 정상 JSON 이고 팩토리도 `row_id: Any` 로 받는다.
        (validate_node, _node, "노드", 12345, "label"),
        (validate_edge, _edge, "엣지", 3.5, "source_id"),
        (validate_chunk, _chunk, "청크", 0, "text"),
    ])
    def test_message_carries_the_row_id(self, validator, builder, kind, rid, missing):
        row = builder()
        row["id"] = rid
        del row[missing]
        with pytest.raises(PackSchemaError) as ei:
            validator(row)
        # **문자열이 아니라 속성으로 본다.** 이 단언은 네 번 실패한 끝에 여기 왔다.
        #   1차 `x in msg` 두 개        rid == missing 이면 서로를 충족 -> 한쪽 지워도 통과
        #   2차 문구 통째로 요구         조사만 바꿔도(`가`->`이`) 깨짐. 계약이 아니라 잡음
        #   3차 순서만 요구 + re.S       너무 느슨. 의미 역전·줄 분리가 통과
        #   4차 의미 앵커(`없다`) + 간격 상한
        #        -> **여전히 우회된다.** `... 가 없다? 아니다, 있다: ...` 가 통과했다
        #           (적대 검증 실증, 2026-08-06). 부분문자열로 의미를 지킬 수 없다.
        #
        # 근본 원인은 단언이 아니라 **진단을 문자열로만 노출한 설계**였다. 예외가
        # `missing_field`/`row_id` 를 속성으로 들고 있으면 문구를 어떻게 다듬든,
        # 어떤 언어로 쓰든, 부정문을 넣든 이 계약은 흔들리지 않는다.
        assert ei.value.missing_field == missing
        assert ei.value.row_id == rid
        # 기대 문자열은 **여기서 직접 쓴다.** 앞선 판은 기대값을 팩토리로 만들면서 `kind` 를
        # 실제 메시지에서 떼어 왔다(`str(ei.value).split("에 ")[0]`). 자기참조라
        # **세 검사가 서로의 kind 를 써도 통과한다** — validate_node 가 "엣지" 를 붙여도
        # 기대값이 따라 바뀐다(적대 검증 실증, 2026-08-06:
        # `row_id_test_uses_message_derived_kind` / `wrong_validator_kind_would_pass`).
        # 자기참조 기대값을 만든 것이 이번이 세 번째다.
        assert str(ei.value) == f"{kind}에 필수 필드 {missing!r} 가 없다: {rid!r}"

    # 위 6케이스는 전부 **필드를 하나만 뺀 행**이다. 그래서 "여러 필수 필드가 동시에
    # 없을 때 어느 것을 보고하는가"라는 축이 통째로 비어 있었고, 필수키 튜플 순서를
    # 역순으로 뒤집는 변이가 87 passed 를 그대로 유지했다(적대 검증 실증, 2026-08-06: D7):
    #
    #     row = {'id': None, 'node_type': 'Concept', 'space': 'concept'}   # label 도 없음
    #     원본  : 노드에 필수 필드 'id' 가 없다: None
    #     D7 변이: 노드에 필수 필드 'label' 가 없다: None   <- 다른 필드를 지목
    #
    # 이 파일의 docstring 이 세 번 반복해 적은 원칙("등가를 측정할 때도 입력이 그 변이가
    # 건드리는 축을 갈라야 한다")을 **그 원칙을 적은 테스트 자신이** 또 어겼다.
    # 이번이 네 번째다. 축을 가르는 입력은 "동시에 여러 개가 빈 행"이다.
    @pytest.mark.parametrize("rid", ["row-1", 12345, 3.5])
    @pytest.mark.parametrize("validator,builder,kind,keys", [
        (validate_node, _node, "노드", ("id", "label", "node_type", "space")),
        (validate_edge, _edge, "엣지", ("id", "source_id", "target_id", "label")),
        (validate_chunk, _chunk, "청크", ("id", "document_id", "text")),
    ])
    def test_first_declared_missing_field_is_the_one_reported(
            self, validator, builder, kind, keys, rid):
        """여러 필수 필드가 동시에 비면 **선언 순서상 첫 번째**를 보고한다.

        운영자가 수만 행을 고칠 때 "어느 필드부터 채우라"는 지시가 행마다 달라지면
        진단이 쓸모없어진다. 순서는 계약이다.

        **크기 2 에서 멈추지 않는다.** 2개 조합만 돌던 판은 "3개 이상 동시 부재일 때만
        마지막 필드를 보고"하는 변이를 통과시켰다(적대 검증 실증, 2026-08-10: V3).
        축을 열어 놓고 깊이를 2에서 끊으면 그 밑은 여전히 무방비다 — `combinations` 로
        2부터 전 크기까지 돈다(노드 11 · 엣지 11 · 청크 4 = 26 조합).

        **그리고 이 축 위에서 계약을 전부 단언한다.** 앞선 판은 `missing_field` 하나만
        봤고, 그래서 **같은 축에서** `row_id` 를 `None` 으로 뭉개는 변이가 90 passed 를
        유지했다(V12). 잃는 것은 이 클래스 docstring 이 "운영자가 수만 행 중 어느 것이
        문제인지 잃는다"라고 못박은 바로 그 값이다. 단일 부재 테스트는 세 가지를 다 보는데
        다중 부재만 하나만 보면 대칭이 깨진다.
        """
        seen = 0
        for r in range(2, len(keys) + 1):
            for combo in itertools.combinations(keys, r):
                first = combo[0]          # combinations 는 입력 순서를 보존한다
                row = builder()
                row["id"] = rid
                for k in combo:
                    row[k] = None
                with pytest.raises(PackSchemaError) as ei:
                    validator(row)
                seen += 1
                assert ei.value.missing_field == first, (
                    f"{kind}: {list(combo)} 가 동시에 비었는데 {ei.value.missing_field!r} 를 "
                    f"보고했다 — 필수키 선언 순서 {keys} 와 어긋난다")
                # 기대값을 **입력 행에서 직접 유도한다.** 앞선 판은 `"row-1"` 리터럴을
                # 특례 계산으로 박았고, 그래서 `row_id` 를 `isinstance(str)` 일 때만
                # 유지하는 변이가 129 passed 를 그대로 통과했다(적대 검증 실증,
                # 2026-08-10: W3). `{"id": 12345}` 는 정상 JSON 이고 팩토리도
                # `row_id: Any` 로 받는다 — 문자열은 이 축의 한 점일 뿐이었다.
                #
                # 타입을 열거로 늘리면(12345, 3.5, Decimal, bytes …) 네 번째 점 수정이
                # 된다. `row` 는 **테스트가 만든 입력**이므로 여기서 유도해도 자기참조가
                # 아니다 — 자기참조의 기준은 "기대값이 검증 대상에서 왔는가"다.
                assert ei.value.row_id == row.get("id"), (
                    f"{kind}: {list(combo)} 에서 row_id 가 {ei.value.row_id!r} — "
                    f"어느 행이 문제인지 잃는다")
                assert str(ei.value) == (
                    f"{kind}에 필수 필드 {first!r} 가 없다: {row.get('id')!r}")
        # 루프가 0회 돌면 위 단언이 **하나도 실행되지 않고** 통과한다.
        want_seen = sum(1 for r in range(2, len(keys) + 1)
                        for _ in itertools.combinations(keys, r))
        assert seen == want_seen, f"조합 순회 {seen}회 (기대 {want_seen})"


class TestMissingRequiredTemplate:
    """필수 필드 부재 문구를 **한 곳에서** 검사한다.

    처음엔 "문구는 리뷰 사안이지 테스트 사안이 아니다"라고 판단하고 속성만 검사했다.
    **틀렸다.** MCP 도구가 예외를 `{"error": str(exc)}` 로 감싸 그대로 응답에 싣고
    (`opencrab/mcp/tools/graph.py:128,202`), `missing_field`/`row_id` 는 이 모듈과 테스트
    밖에서 아무도 안 읽는다. 즉 **거짓 문구가 운영 사용자에게 도달하는 실경로가 있다**
    (적대 검증이 MCP handler 에 거짓 메시지를 주입해 실증, 2026-08-06).

    문구가 계약이라면 문자열 검사가 필요하다. 다만 그것을 **세 자리에 흩어 두면** 앞서
    네 라운드를 태운 부분문자열 싸움이 되풀이된다. 팩토리로 템플릿을 한 곳에 모았으니
    여기 한 자리만 정확히 못박으면 된다.
    """

    @pytest.mark.parametrize("kind", ["노드", "엣지", "청크"])
    def test_message_is_exactly_the_template(self, kind):
        e = PackSchemaError.missing_required(kind, "label", "row-1")
        assert str(e) == f"{kind}에 필수 필드 'label' 가 없다: 'row-1'"
        assert (e.missing_field, e.row_id) == ("label", "row-1")

    # `test_message_states_absence_not_presence` 를 여기서 **삭제했다.** 금지어 목록
    # (`아니다`/`있다:`/`정상`/`실패`)로 거짓 문구를 막으려 했는데 블랙리스트는 원리적으로
    # 우회된다 — 목록에 없는 다른 거짓 문구는 그대로 통과했다(적대 검증 실증, 2026-08-06:
    # `forbidden_word_subtest_accepts_alternate_false_wording`). 바로 위
    # `test_message_is_exactly_the_template` 이 **완전 일치**를 걸므로 어떤 거짓 문구도
    # 통과할 수 없다. 완전 일치 옆의 블랙리스트는 보호를 더하지 않고 거짓 안심만 준다.

    def test_every_raise_site_uses_the_factory(self):
        """세 검사의 필수필드 raise 가 전부 팩토리를 거치는지 **AST 로** 본다.

        앞선 판은 소스 문자열 `src.count("PackSchemaError.missing_required(") == 3` 을 셌다.
        **우회된다** — `_mr = PackSchemaError.missing_required` 로 alias 를 두거나
        `getattr(PackSchemaError, "missing_required")` 로 부르면 카운트가 0인데 동작은
        같다(적대 검증 실증, 2026-08-06: `alias_comment_bypass_source_guard` /
        `dynamic_comment_bypass_source_guard`). 문자열이 아니라 **구문 구조**로 본다.

        **이 테스트가 못 보는 것을 정직하게 적는다**(적대 검증 실증, 2026-08-06):

        1. **인자값을 안 본다.** `Call/Attribute/Name` 모양만 확인하므로 kind·key·row_id
           에 무엇이 담기는지, 필수키 튜플의 내용과 **순서**가 어떤지는 전혀 모른다.
           튜플 순서 역전 변이(D7)를 이 테스트는 잡지 못한다 —
           `test_first_declared_missing_field_is_the_one_reported` 가 잡는다.
        2. **런타임 몬키패치에 눈이 멀어 있다.** 클래스 정의 뒤에
           `PackSchemaError.missing_required = classmethod(_sneaky)` 로 갈아치우면 raise
           사이트의 소스 문법은 그대로라 **이 테스트만 단독 실행하면 통과한다**(1 passed).
           그 변이를 잡는 것은 이 테스트가 아니라 곁의 런타임 동작 테스트들이다.
           즉 "팩토리 우회를 막는다"는 이 테스트의 방어 범위는 **소스 수준 우회까지**다.
           그 이상을 기대하지 마라.

           (실패 **건수**는 근거로 쓰지 마라 — `_sneaky` 본문에 따라 갈린다. 다른 문구를
           쓰면 27 failed, 원 문구를 베껴 쓰면 13 failed 로 측정됐다. 후자에서는 아래
           템플릿 리터럴 카운트가 단독으로도 잡는다. 앞선 판이 "24 failed"를 단정값으로
           적었는데 재현되지 않았다 — 적대 검증 지적, 2026-08-10.)
        """
        import ast
        import pathlib

        import opencrab.pack.schema as m
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        fns = {n.name: n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)}
        for name in ("validate_node", "validate_edge", "validate_chunk"):
            # 필수필드 루프(`for key in (...)`) 안의 raise 만 대상이다. 같은 함수의 다른
            # raise(space 범위·properties 타입)는 팩토리를 쓰지 않는 게 정상이라 전량을
            # 요구할 수 없다. 루프가 사라지면 조용히 0건이 되므로 루프와 raise 의 존재를
            # 각각 단언한다 — 대가로 **루프를 if 3개로 언롤하는 정당한 리팩터도 실패한다**
            # (측정: 5개 주입 중 M5). 조용한 통과보다 시끄러운 실패를 택한 것이고,
            # 메시지가 "필수필드 루프가 없다"라 원인이 바로 보인다.
            loops = [n for n in fns[name].body if isinstance(n, ast.For)]
            assert loops, f"{name} 에 필수필드 루프가 없다"
            raises = [n for lp in loops for n in ast.walk(lp) if isinstance(n, ast.Raise)]
            assert raises, f"{name} 의 필수필드 루프에 raise 가 없다"
            for r in raises:
                f = getattr(r.exc, "func", None)
                assert (isinstance(r.exc, ast.Call)
                        and isinstance(f, ast.Attribute)
                        and f.attr == "missing_required"
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "PackSchemaError"), (
                    f"{name}:{r.lineno} 가 PackSchemaError.missing_required 를 "
                    "직접 부르지 않는다")
        # 템플릿 **리터럴**의 복제만 문자열로 본다. 이건 alias 로 우회할 수 있는 대상이
        # 아니다 — 문구를 베껴 쓰면 반드시 소스에 한 벌 더 나타난다.
        assert src.count("필수 필드 {key!r}") == 1, \
            "필수필드 문구는 팩토리에만 있어야 한다 — 호출부가 직접 쓰면 속성과 어긋난다"


class TestOnlyPackSchemaErrorEscapes:
    """어떤 dict 를 넣어도 `PackSchemaError` 외의 예외가 새면 안 된다.

    진단 문구가 `row['id']` 첨자를 쓰던 시절, **검사 순서를 바꾸는 리팩터만으로**
    `PackSchemaError` 가 `KeyError` 로 바뀌었다. 그런데 스위트는 129 passed 를 그대로
    유지했다(적대 검증 실증, 2026-08-10: W8b):

        엣지 stray 검사를 필수 루프보다 앞으로 옮김 + id 키 없는 행
        -> BASELINE  PackSchemaError("엣지에 필수 필드 'id' 가 없다: None")
        -> 변이 후    KeyError: 'id'

    같은 실패 모드를 이 파일 위쪽(`schema.py` 필수 루프 주석)이 이미 문서화했는데,
    그 교훈이 **한 자리에만** 적용되고 형제 raise 7자리는 첨자 그대로였다. 결함을 고치면
    형제 경로도 같은 패턴인지 검사한다는 규율의 미적용이다. 이제 8첨자를 `.get` 으로
    바꿨고, 되돌리지 못하도록 여기서 불변식을 건다 — 검사 **순서와 무관하게** 성립한다.

    입력에는 반드시 **id 키가 없으면서 stray 키가 있는 행**과 **id 키가 없으면서
    properties 가 비 dict 인 행**이 들어가야 한다. 그 둘이 없으면 이 테스트도 장식이다.
    """

    def _rows(self, base, struct_key, extra_bad):
        rows = []
        # **드롭 축을 구조 키 전수로 연다.** `'id'` 하나만 빼던 판은 다른 키를 첨자로
        # 읽는 변이를 통과시켰다(적대 검증 실증, 2026-08-10: X5):
        #
        #     _ws = row['workspace_id']   # stray raise 직전, 독립 Assign 문
        #     -> workspace_id 없는 **정상 엣지 행**이 KeyError 로 죽는데 142 passed
        #
        # `workspace_id` 를 목록에 추가하는 것은 여섯 번째 점 수정이고 다음엔
        # `created_at` 이 남는다. 전수로 열면 **어떤 구조 키를 첨자로 읽든, raise 안이든
        # 밖이든** 잡힌다 — 그래서 아래 AST 가드가 못 보는 형태를 열거할 필요가 없어진다.
        # 비용: 검사당 9 -> 최대 41행. 무시할 수준이다.
        for drop in [None, *sorted(base())]:
            for variant in ({}, {"낯선키": 1}, extra_bad, {"낯선키": 1, **extra_bad}):
                row = base()
                row.update(variant)
                if drop:
                    row.pop(drop, None)
                rows.append(row)
        # 비문자열 최상위 키 — `sorted(stray)` 가 int 와 str 을 비교하면 TypeError 다.
        # 이 행이 없으면 "어떤 dict 를 넣어도"라는 이 클래스의 주장이 거짓인 채 남는다.
        for variant in ({7: "x"}, {7: "x", "낯선키": 1}):
            row = base()
            row.update(variant)
            rows.append(row)
        # 필수 필드가 전부 빈 극단
        empty = base()
        for k in struct_key:
            empty[k] = None
        rows.append(empty)
        return rows

    @pytest.mark.parametrize("validator,base,keys,bad", [
        (validate_node, _node, ("id", "label", "node_type", "space"),
         {"properties": "dict 아님"}),
        (validate_edge, _edge, ("id", "source_id", "target_id", "label"),
         {"properties": "dict 아님"}),
        (validate_chunk, _chunk, ("id", "document_id", "text"),
         {"metadata": "dict 아님"}),
    ])
    def test_no_other_exception_type_escapes(self, validator, base, keys, bad):
        for row in self._rows(base, keys, bad):
            try:
                validator(row)
            except PackSchemaError as exc:
                # id 키가 **없는** 행이면 `row_id` 는 None 이어야 한다. 이 단언이 없으면
                # `row.get("id", "<unknown>")` 처럼 **위조 식별자**를 채우는 변이가
                # 통과한다(적대 검증 실증, 2026-08-10: X1·W10b). 세 라운드가 row_id 를
                # 지키는 데 쓰였는데 정작 "id 가 없을 때" 축은 비어 있었다.
                if "id" not in row and exc.row_id is not None:
                    pytest.fail(
                        f"id 키가 없는 행인데 row_id={exc.row_id!r} — 없는 식별자를 "
                        f"지어냈다. 운영자가 존재하지 않는 행을 찾게 된다. 입력: {row!r}")
            except Exception as exc:  # noqa: BLE001 - 계약 위반을 잡는 것이 목적
                pytest.fail(
                    f"{type(exc).__name__}: {exc} — 계약은 PackSchemaError 뿐이다. "
                    f"입력 행: {row!r}")

    def test_diagnostics_never_subscript_the_row(self):
        """`raise` **문 안의** 첨자를 막는다 — 방어 범위를 정확히 적는다.

        앞선 판은 이 검사가 "다른 키를 첨자로 읽는" 경우까지 막는다고 적었는데
        **틀렸다.** `ast.walk` 를 `ast.Raise` 노드에서 시작하므로, 첨자를 독립
        `Assign` 문으로 빼내면(`_ws = row['workspace_id']` 를 raise 앞에) 설계상
        보이지 않는다(적대 검증 실증, 2026-08-10: X5).

        서술된 방어 범위가 실제보다 넓으면 다음 사람이 없는 방어를 믿는다. 사실대로:
        **이 검사는 raise 문 안만 본다. 지역변수 경유는 위 행동 테스트의 드롭 전수가
        잡는다.** 두 검사는 겹치는 게 아니라 각자 다른 형태를 맡는다.
        """
        import ast
        import pathlib as _pl

        import opencrab.pack.schema as m
        src = _pl.Path(m.__file__).read_text(encoding="utf-8")
        bad = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Raise):
                continue
            for x in ast.walk(node):
                if isinstance(x, ast.Subscript) and isinstance(x.value, ast.Name):
                    bad.append(f"L{node.lineno}: {ast.unparse(x)}")
        assert not bad, (
            "진단 문구가 행을 첨자로 읽는다 — 그 키가 없으면 KeyError 로 계약이 깨진다. "
            "`.get()` 을 써라:\n  " + "\n  ".join(bad))


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
    @pytest.mark.parametrize("empty", ["", 0, False, [], {}])
    def test_empty_value_required_field_rejected(self, key, empty):
        """`if not row.get(key)` 를 `if key not in row` 로 바꿔도 생존했다(변이).

        빈 문자열 id 는 dangling 검사를 통과하면서 라이브에 유령 노드를 만든다 —
        "키가 있다"가 아니라 "값이 있다"가 계약이다.

        **falsy 를 `""` 하나로 고정하지 않는다.** 빈 문자열만 넣던 판은
        `not row.get(key)` 를 `row.get(key) in (None, "")` 로 바꾸는 변이를 통과시켰고,
        그러면 `id=0` 인 노드가 검증을 통째로 빠져나간다(적대 검증 실증, 2026-08-10: V11).
        위 유령 노드 사고와 같은 클래스이고 값의 **타입**만 다르다.
        """
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_node(_node(**{key: empty}))

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

    @pytest.mark.parametrize("key", ["id", "source_id", "target_id", "label"])
    @pytest.mark.parametrize("empty", ["", 0, False, [], {}])
    def test_empty_value_required_field_rejected(self, key, empty):
        """엣지도 `not row.get(key)` 다 — 노드와 같은 계약이 걸려야 한다.

        한쪽만 걸면 `row.get(key) in (None, "")` 변이가 엣지에서만 살아남는다
        (적대 검증 실증, 2026-08-10: V11).
        """
        with pytest.raises(PackSchemaError, match="필수 필드"):
            validate_edge(_edge(**{key: empty}))

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

    @pytest.mark.parametrize("empty", ["", 0, False])
    def test_falsy_but_present_values_are_valid(self, empty):
        """청크만 `is None` 이다 — 이 **비대칭은 의도**이고, 그 자체가 계약이다.

        빈 청크는 계약 위반이 아니다(필드 부재만 위반). 그래서 노드·엣지의 falsy 거부를
        청크로 확장하면 안 된다.

        앞선 판은 이 비대칭을 `text=""` 한 케이스로만 지켰고, 그래서 청크의 `is None` 을
        노드·엣지처럼 falsy 로 바꾸는 변이가 1건만 실패해 잡히긴 했지만 계약의 **방향**이
        고정되지 않았다(적대 검증 지적, 2026-08-10: V5·V7c). positive 로 못박는다 —
        "값이 falsy 여도 **있으면** 통과한다."
        """
        validate_chunk(_chunk(text=empty))
        validate_chunk(_chunk(document_id=empty))

    def test_none_required_field_rejected(self):
        """부재의 판정 기준은 `None` 이다 — 위 positive 와 짝을 이룬다."""
        for key in ("id", "document_id", "text"):
            with pytest.raises(PackSchemaError, match="필수 필드"):
                validate_chunk(_chunk(**{key: None}))

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
