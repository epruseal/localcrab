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
        nodes = [_n("a"), _n("b")]
        r = gf.predict_grammar_fit(nodes, [_e("a", "b", "RELATES_TO")])
        assert r["total_edges"] == 1
        assert r["pass"] is (r["violations"] == 0 and r["missing_endpoint"] == 0)

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
