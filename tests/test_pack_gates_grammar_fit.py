"""``opencrab.pack.gates.grammar_fit`` 계약.

이 게이트의 존재 이유는 **적재 결과를 예측하는 것**이다. 그래서 판정을 재구현하면 안 된다 —
게이트가 정본과 다른 규칙을 쓰면 "게이트 통과"가 "적재 성공"을 뜻하지 않게 된다.
이관 전 이 파일은 실제로 적재기의 해석 순서를 손으로 옮겨 적고 있었다.

여기 거는 것은 **정본과 같은 함수를 쓰는가**와 **예측이 실제 적재와 일치하는가**다.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from opencrab.pack.gates import grammar_fit as gf
from opencrab.pack.normalize import resolve_edge, resolve_node_space_type


def _n(i, space="concept", node_type="Concept"):
    return {"id": i, "label": i, "node_type": node_type, "space": space}


def _e(src, tgt, label):
    return {"id": f"{src}->{tgt}", "source_id": src, "target_id": tgt, "label": label}


class TestUsesTheCanonicalResolvers:
    """정본을 **부르는지** 구조로 건다 — 규칙을 베껴 쓰면 여기서 걸린다."""

    def test_effective_space_delegates_to_the_loader_resolver(self):
        """자체 규칙이 아니라 `resolve_node_space_type` 결과여야 한다.

        기대값을 손으로 적지 않고 정본에서 유도한다 — 이 게이트의 계약이
        "정본과 같은 답"이지 "특정 값"이 아니기 때문이다.
        """
        for space, ntype in [("concept", "Concept"), ("resource", "Document"),
                             ("evidence", "Evidence"), ("concept", "Metric")]:
            assert gf.resolve_effective_space(space, ntype) == \
                resolve_node_space_type(space, ntype)[0]

    @pytest.mark.parametrize("label,fs,ts", [
        ("EVIDENCED_BY", "claim", "evidence"),   # traceability 반전 — 실팩에 실제로 나온다
        ("SHOWN_IN", "concept", "resource"),
    ])
    def test_reversed_edge_is_not_flipped_again(self, label, fs, ts):
        """반전 관계에서 **다시 뒤집지 않는지**. 손으로 뒤집던 판이 오탐을 냈다.

        **기대값을 `edge_allowed` 로 만들면 안 된다** — 뒤집는 변이를 넣으면 기대값도
        같이 뒤집혀 통과한다(자체 측정: `if _reversed: a, b = b, a` 주입 시 8 passed).
        자기참조다. `True` 를 직접 단언한다.

        그리고 **대칭 공간쌍(concept->concept)으로는 안 갈린다** — 양방향이 다 허용이라
        뒤집어도 같은 답이 나온다. 비대칭 쌍을 써야 축이 갈린다.
        """
        _a, _rel, _b, reversed_ = resolve_edge(label, fs, ts)
        assert reversed_ is True, f"전제: {label} 은 반전 관계다"
        assert gf.fits(label, fs, ts) is True, (
            f"{label} {fs}->{ts} 를 미정합으로 판정했다 — 반전 후 space 를 또 뒤집었을 때 "
            "나오는 결과다. 이 오탐이 이 파일을 정본 위임으로 바꾼 이유다")

    def test_no_lazy_loading_or_snapshot_fallback_remains(self):
        """이관으로 사라져야 할 것이 정말 사라졌는가.

        지연 로딩·스냅샷 폴백은 "호출자 리포에 있었기 때문에" 필요했던 것이고,
        그 폴백이 정본과 다른 판정을 내려 Mac 오탐의 원인이었다. 같은 패키지가 된
        지금 되살아나면 같은 문제가 돌아온다.
        """
        src = pathlib.Path(gf.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 함수 **안**의 import 가 지연 로딩의 표식이다.
        inner = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner += [f"{fn.name}:{x.lineno}" for x in ast.walk(fn)
                          if isinstance(x, (ast.Import, ast.ImportFrom))]
        assert not inner, f"함수 안 import 가 남았다(지연 로딩): {inner}"
        handlers = [f"L{h.lineno}" for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)]
        assert not handlers, (
            f"예외를 삼키는 자리가 남았다 {handlers} — 폴백은 정본과 다른 답을 낸다. "
            "판정 실패는 삼키지 말고 터뜨려야 게이트가 거짓 초록을 안 낸다")


class TestPrediction:
    def test_clean_pack_passes(self):
        """기대값을 **리터럴로** 적는다.

        1판은 `r["pass"] is (r["violations"] == 0 and ...)` 였다 — 반환값끼리 대사하는
        항등식이라 **정합 판정문 자체를 지워도** 통과한다(스윕 실측: `del-stmt:If@L81`
        생존). 자기참조 테스트는 통과 개수만 늘린다.

        리터럴로 바꾸자마자 **픽스처 자신의 결함**이 드러났다: 1판이 쓰던 `RELATES_TO` 는
        concept->concept 허용 목록(`depends_on`·`influences`·`part_of`·`related_to`·
        `subclass_of`)에 없어서 "clean pack" 이 실은 미정합 1건이었다. 자기참조 단언이
        그것을 몇 라운드 동안 가리고 있었다.
        """
        nodes = [_n("a"), _n("b")]           # 둘 다 concept
        r = gf.predict_grammar_fit(nodes, [_e("a", "b", "related_to")])
        assert (r["total_edges"], r["violations"], r["missing_endpoint"]) == (1, 0, 0)
        assert r["pass"] is True

    def test_missing_endpoint_is_counted_separately_not_as_violation(self):
        """endpoint 누락은 정합 판정 **대상 밖**이다.

        `total_edges` 에 넣으면 정합률이 누락을 희석한다 — 두 수치가 섞이면
        "정합 100%인데 절반이 적재 안 됨"이 가능해진다.
        """
        r = gf.predict_grammar_fit([_n("a")], [_e("a", "없는놈", "CITES")])
        assert (r["total_edges"], r["violations"], r["missing_endpoint"]) == (0, 0, 1)
        assert r["pass"] is False, "endpoint 누락이 있는데 통과로 판정했다"

    def test_violation_detail_names_the_allowed_relations(self):
        """미정합 보고가 **무엇이 허용인지** 말해야 고칠 수 있다."""
        nodes = [_n("a", space="concept"), _n("b", space="concept")]
        r = gf.predict_grammar_fit(nodes, [_e("a", "b", "존재하지않는관계")])
        assert r["violations"] == 1
        d = r["violations_detail"][0]
        assert d["label"] == "존재하지않는관계"
        assert d["from_space"] == "concept" and d["to_space"] == "concept"
        assert d["allowed"], "허용 relation 목록이 비어 있으면 고칠 방법을 못 알려준다"

    @pytest.mark.parametrize("field_pair", [
        ("source_id", "target_id"), ("from_id", "to_id"),
    ])
    def test_both_endpoint_key_conventions_are_read(self, field_pair):
        """교환 포맷에 두 관례가 섞여 있다 — 한쪽만 읽으면 전량 endpoint 누락이 된다."""
        src_k, tgt_k = field_pair
        e = {"id": "e1", src_k: "a", tgt_k: "b", "label": "RELATES_TO"}
        r = gf.predict_grammar_fit([_n("a"), _n("b")], [e])
        assert r["missing_endpoint"] == 0, f"{field_pair} 를 못 읽었다"
        assert r["total_edges"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 반환 계약 — score 의 `counts` 와 **같은 클래스**다.
#
# 판정(`pass`·수치)만 걸면 키 이름을 `""` 로 바꾸는 변이가 통과한다. 판정은 그대로인데
# 호출자가 `KeyError` 로 죽는다 — 게이트가 초록인 채 파이프라인이 끊긴다.
# 돌연변이 스윕 실측(2026-08-10): 이 모듈 총 77 중 생존 15, 그중
# `top_violations`·`detail`·`count` 키 이름 변이가 살아 있었다.
# score 에서 이미 닫은 축을 형제 모듈에 안 건 것이 클래스 미폐쇄다.
# ══════════════════════════════════════════════════════════════════════════


class TestReturnContract:
    KEYS = {"pass", "total_edges", "violations", "missing_endpoint",
            "top_violations", "violations_detail", "detail"}
    DETAIL_KEYS = {"label", "from_space", "to_space", "count", "allowed"}

    def _bad(self, n=1):
        nodes = [_n(f"a{i}") for i in range(n)] + [_n(f"b{i}") for i in range(n)]
        edges = [_e(f"a{i}", f"b{i}", f"없는관계{i}") for i in range(n)]
        return gf.predict_grammar_fit(nodes, edges)

    def test_top_level_keys_are_exactly_these_seven(self):
        assert set(self._bad().keys()) == self.KEYS

    def test_violation_detail_keys_are_exactly_these_five(self):
        d = self._bad()["violations_detail"][0]
        assert set(d.keys()) == self.DETAIL_KEYS

    def test_top_violations_is_capped_at_five(self):
        """상한이 없으면 미정합 관계가 많은 팩에서 한 줄이 수백 항목을 뱉는다.

        상한을 늘리는 변이는 판정을 안 바꾸므로 수치 검사로는 안 잡힌다.
        """
        r = self._bad(7)
        assert r["violations"] == 7, "전제: 7종이 전부 미정합이어야 한다"
        assert len(r["top_violations"]) == 5, f"상한이 5가 아니다: {len(r['top_violations'])}"
        assert len(r["violations_detail"]) == 7, "상세는 잘리지 않는다 — 상한은 요약에만 건다"

    def test_detail_sentence_reports_all_three_counts(self):
        """요약 문장이 총·미정합·누락 셋을 다 말하는가. 하나라도 빠지면 운영자가 오판한다."""
        nodes = [_n("a"), _n("b")]
        r = gf.predict_grammar_fit(nodes, [_e("a", "b", "없는관계"), _e("a", "유령", "CITES")])
        assert (r["total_edges"], r["violations"], r["missing_endpoint"]) == (1, 1, 1)
        assert r["detail"] == "엣지 1건 중 grammar 미정합 1건, endpoint 누락 1건"


class TestNodeFieldDefaults:
    """노드에 필드가 **없을 때**의 기본값도 계약이다.

    실팩에는 `space`·`node_type` 이 빠진 노드가 있다. 기본값이 바뀌면 그 노드들의
    effective space 가 통째로 달라져 정합 판정이 뒤집힌다. 모든 픽스처가 두 필드를
    갖고 있으면 이 축은 영영 안 갈린다(스윕 실측: `drop-get-default@L47,L48` 생존).
    """

    @pytest.mark.parametrize("row,why", [
        ({"id": "x", "label": "x"}, "둘 다 없음 -> concept/Concept"),
        ({"id": "x", "label": "x", "node_type": "Concept"}, "space 없음 -> concept"),
        ({"id": "x", "label": "x", "space": "concept"}, "node_type 없음 -> Concept"),
    ], ids=["both-absent", "no-space", "no-node-type"])
    def test_absent_fields_fall_back_to_concept(self, row, why):
        got = gf.effective_spaces([row])["x"]
        assert got == resolve_node_space_type("concept", "Concept")[0], why

    def test_present_space_is_not_overridden_by_the_default(self):
        """기본값이 실제 값을 덮으면 안 된다 — `"space"` 키 이름이 깨지면 그렇게 된다."""
        got = gf.effective_spaces([_n("c1", space="claim")])["c1"]
        assert got == resolve_node_space_type("claim", "Concept")[0]
        assert got != resolve_node_space_type("concept", "Concept")[0], (
            "전제: claim 과 concept 의 effective space 가 달라야 이 검사가 유효하다")

    def test_node_type_actually_changes_the_effective_space(self):
        """`node_type` 키를 **읽는지**를 건다.

        `resource`+`Document` 같은 조합으로는 안 갈린다 — 그 space 는 node_type 과
        무관하게 같은 답이 나오기 때문이다(스윕 실측: `const:'node_type'->''@L48` 생존).
        node_type 이 결과를 실제로 바꾸는 조합을 써야 축이 갈린다.
        """
        row = {"id": "a1", "label": "a1", "space": "subject", "node_type": "AdminRule"}
        got = gf.effective_spaces([row])["a1"]
        assert got == resolve_node_space_type("subject", "AdminRule")[0]
        assert got != resolve_node_space_type("subject", "Concept")[0], (
            "전제: AdminRule 이 기본 Concept 과 다른 space 를 내야 이 검사가 유효하다")

    def test_node_type_default_value_is_unreachable_by_design(self):
        """`n.get("node_type", "Concept")` 의 **기본값**은 결과를 바꿀 수 없다 — 등가 증명.

        9-space 전수로 `Concept` · `None` · `""` 셋이 **모두 같은 답**을 낸다. 그래서
        그 리터럴을 흔드는 변이 2종(`'Concept'->''`, `drop-get-default`)이 살아남는다.
        결함이 아니라 도달 불가다. 이 단언이 깨지면 = 기본값이 의미를 갖게 됐다는 뜻이고,
        그때는 위 2종이 진짜 미검사 축이 되므로 픽스처를 추가해야 한다.
        """
        for sp in ["subject", "community", "policy", "claim", "concept",
                   "resource", "evidence", "lever", "outcome"]:
            got = {resolve_node_space_type(sp, t)[0] for t in ("Concept", None, "")}
            assert len(got) == 1, f"{sp}: node_type 기본값이 결과를 바꾼다 {got}"


class TestBulkImportEdgeKeys:
    """엣지 라벨도 두 관례가 섞여 있다. `relation` 을 못 읽으면 라벨이 `""` 이 되어
    **전 엣지가 미정합**으로 잡힌다(스윕 실측: `const:'relation'->''@L75` 생존)."""

    def test_relation_key_is_read_when_label_absent(self):
        nodes = [_n("a"), _n("b")]
        e = {"id": "e1", "source_id": "a", "target_id": "b", "relation": "related_to"}
        r = gf.predict_grammar_fit(nodes, [e])
        assert (r["total_edges"], r["violations"]) == (1, 0), \
            "relation 을 못 읽어 라벨이 빈 문자열이 됐다"


class TestFitsIsTheVerdictNotEdgeAllowed:
    """정합 판정은 `fits` 다 — `edge_allowed` 로 바꿔치면 **반전 관계**에서 갈린다.

    스윕 실측: `call-target:fits->edge_allowed@L81` 생존. 두 함수는 대칭 공간쌍에서
    같은 답을 내므로 비대칭·반전 관계를 써야 축이 갈린다.
    """

    def test_reversed_relation_is_fit_and_counted_as_such(self):
        nodes = [_n("c1", space="claim"), _n("e1", space="evidence")]
        _a, _rel, _b, reversed_ = resolve_edge("EVIDENCED_BY", "claim", "evidence")
        assert reversed_ is True, "전제: 반전 관계여야 한다"
        r = gf.predict_grammar_fit(nodes, [_e("c1", "e1", "EVIDENCED_BY")])
        assert (r["total_edges"], r["violations"]) == (1, 0), \
            "반전 관계를 미정합으로 셌다 — 판정 함수가 정본이 아니다"
        assert r["pass"] is True


class TestAllowedIsNoneWhenNothingIsAllowed:
    """허용 relation 이 **하나도 없는** 공간쌍은 `None` 이다(빈 리스트가 아니다).

    빈 리스트와 `None` 은 호출자에게 다른 뜻이다 — 전자는 "목록이 비었다", 후자는
    "이 방향은 아예 불가"다. 스윕 실측: `drop-or-default@L89` 생존.
    """

    def test_detail_allowed_is_none_for_a_forbidden_direction(self):
        nodes = [_n("e1", space="evidence"), _n("s1", space="subject")]
        r = gf.predict_grammar_fit(nodes, [_e("e1", "s1", "아무관계")])
        assert r["violations"] == 1
        d = r["violations_detail"][0]
        assert (d["from_space"], d["to_space"]) == ("evidence", "subject")
        assert d["allowed"] is None, (
            f"허용이 없는 방향인데 {d['allowed']!r} — 빈 리스트와 None 은 다른 뜻이다")
