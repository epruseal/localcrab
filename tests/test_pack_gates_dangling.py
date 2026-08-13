"""``opencrab.pack.gates.dangling`` 계약.

이 게이트는 **정본 키를 고정하는 것**이 존재 이유다. 이전에 dangling 점검을
`source`/`target` 같은 추측 키로 인라인 수행해 전 엣지가 오탐으로 잡힌 적이 있다.
그래서 키 자체를 계약으로 건다.

판정 두 축(dangling · evidence 대사)이 **둘 다 결과에 반영**되는지도 건다 — 하나만
보고 다른 하나를 출력만 하면 게이트가 초록인데 결함이 남는다.
"""
from __future__ import annotations

import json

import pytest

from opencrab.pack.gates.dangling import EDGE_SRC, EDGE_TGT, NODE_ID, check_pack


def _pack(tmp_path, nodes, edges, chunks=()):
    d = tmp_path / "p"
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in (("nodes", nodes), ("edges", edges), ("chunks", chunks)):
        if rows is None:
            continue
        (d / f"{name}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return d


def _n(i, space="concept"):
    return {"id": i, "label": i, "node_type": "Concept", "space": space}


def _e(src, tgt):
    return {"id": f"{src}->{tgt}", "source_id": src, "target_id": tgt, "label": "cites"}


class TestCanonicalKeys:
    """정본 키가 이 모듈의 존재 이유다 — 값 자체를 고정한다."""

    def test_keys_are_the_exchange_format_names(self):
        assert (EDGE_SRC, EDGE_TGT, NODE_ID) == ("source_id", "target_id", "id")

    def test_guessed_keys_are_not_used(self, tmp_path):
        """`source`/`target` 로 쓴 엣지는 **키가 없어서** 터져야 한다 — 조용히 통과하면
        추측 키를 관용하는 것이고, 그것이 전 엣지 오탐 사고의 형태였다."""
        d = _pack(tmp_path, [_n("a"), _n("b")],
                  [{"id": "e", "source": "a", "target": "b", "label": "cites"}])
        with pytest.raises(KeyError):
            check_pack(d)


class TestBothAxesReachTheVerdict:
    def test_clean_pack_passes(self, tmp_path):
        d = _pack(tmp_path, [_n("a"), _n("b")], [_e("a", "b")])
        r = check_pack(d)
        assert r["ok"] is True and r["reasons"] == []
        assert (r["nodes"], r["edges"], r["dangling"]) == (2, 1, 0)

    def test_dangling_edge_fails(self, tmp_path):
        d = _pack(tmp_path, [_n("a")], [_e("a", "없는놈")])
        r = check_pack(d)
        assert r["ok"] is False and r["dangling"] == 1
        assert "dangling>0" in r["reasons"]

    def test_evidence_chunk_mismatch_fails_even_with_zero_dangling(self, tmp_path):
        """**두 축이 독립**이어야 한다. dangling 만 보면 이 팩이 통과한다."""
        d = _pack(tmp_path, [_n("a", space="evidence"), _n("b", space="evidence")],
                  [_e("a", "b")], chunks=[{"id": "c1"}])
        r = check_pack(d)
        assert r["dangling"] == 0
        assert r["ok"] is False, "evidence 2 vs chunks 1 인데 통과했다"
        assert any("evidence" in x for x in r["reasons"])

    def test_ev_lt_ok_relaxes_only_one_direction(self, tmp_path):
        """완화는 `evidence < chunks` 쪽만이다 — 초과는 그래도 위반이다.

        양방향으로 풀면 선언 하나로 이 축이 통째로 사라진다.
        """
        under = _pack(tmp_path, [_n("a", space="evidence")], [_e("a", "a")],
                      chunks=[{"id": "c1"}, {"id": "c2"}])
        assert check_pack(under, ev_lt_ok=True)["ok"] is True
        assert check_pack(under, ev_lt_ok=False)["ok"] is False

        over = _pack(tmp_path / "o", [_n("a", space="evidence"), _n("b", space="evidence")],
                     [_e("a", "b")], chunks=[{"id": "c1"}])
        assert check_pack(over, ev_lt_ok=True)["ok"] is False, (
            "완화가 초과까지 풀어 버렸다 — 한쪽 방향만이어야 한다")


class TestMissingFiles:
    def test_absent_nodes_or_edges_is_not_a_verdict(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert check_pack(d) is None, "검사 불가와 PASS 를 구분해야 한다"

    def test_absent_chunks_counts_as_zero(self, tmp_path):
        d = _pack(tmp_path, [_n("a")], [_e("a", "a")], chunks=None)
        r = check_pack(d)
        assert r is not None and r["chunks"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 반환 계약 + 판정 경계 — score 의 `counts` 격자와 **같은 클래스**다.
#
# 돌연변이 스윕 실측(2026-08-10): 이 모듈 총 55 중 생존 5. 전부 여기서 닫는 축이다 —
# `ev_lt_ok` 기본값, 파일 존재 판정의 `and`, 반환 키 이름, 사유 문자열의 비교기호.
# score 하나만 닫고 형제 게이트를 안 본 것이 클래스 미폐쇄였다.
# ══════════════════════════════════════════════════════════════════════════


class TestReturnContract:
    KEYS = {"ok", "nodes", "edges", "dangling", "evidence", "chunks", "reasons"}

    def test_top_level_keys_are_exactly_these_seven(self, tmp_path):
        r = check_pack(_pack(tmp_path, [_n("a"), _n("b")], [_e("a", "b")]))
        assert set(r.keys()) == self.KEYS

    def test_counts_are_reported_not_just_the_verdict(self, tmp_path):
        """수치가 판정과 같이 나와야 운영자가 어디를 고칠지 안다."""
        nodes = [_n("a"), _n("b"), _n("e1", space="evidence")]
        chunks = [{"id": "e1", "document_id": "a", "text": "x"}]
        r = check_pack(_pack(tmp_path, nodes, [_e("a", "b")], chunks))
        assert (r["nodes"], r["edges"], r["evidence"], r["chunks"]) == (3, 1, 1, 1)


class TestEvidenceChunkPolarity:
    """`ev_lt_ok` 는 **기본이 엄격(==)** 이다.

    기본값이 느슨(`<=`)해지면 evidence 가 청크보다 적은 팩이 조용히 통과한다 —
    호출자가 완화를 **명시적으로** 선언하게 하려고 둔 인자인데 그 의미가 뒤집힌다.
    """

    def _pack23(self, tmp_path):
        nodes = [_n("a"), _n("b")] + [_n(f"e{i}", space="evidence") for i in range(2)]
        chunks = [{"id": f"e{i}", "document_id": "a", "text": "x"} for i in range(3)]
        return _pack(tmp_path, nodes, [_e("a", "b")], chunks)

    def test_default_is_strict_equality(self, tmp_path):
        r = check_pack(self._pack23(tmp_path))
        assert (r["evidence"], r["chunks"]) == (2, 3), "전제: evidence < chunks"
        assert r["ok"] is False, "기본값이 느슨해졌다 — evidence!=chunks 를 통과시켰다"
        assert "evidence==chunks 위반" in r["reasons"], f"사유 문구가 다르다: {r['reasons']}"

    def test_opt_in_relaxation_accepts_fewer_evidence(self, tmp_path):
        r = check_pack(self._pack23(tmp_path), ev_lt_ok=True)
        assert r["ok"] is True, "명시적 완화가 안 먹었다"
        assert r["reasons"] == []

    def test_relaxed_mode_still_rejects_more_evidence_than_chunks(self, tmp_path):
        """완화는 **한 방향**이다. evidence 가 청크보다 많으면 완화 모드에서도 위반이다."""
        nodes = [_n("a"), _n("b")] + [_n(f"e{i}", space="evidence") for i in range(3)]
        chunks = [{"id": "e0", "document_id": "a", "text": "x"}]
        r = check_pack(_pack(tmp_path, nodes, [_e("a", "b")], chunks), ev_lt_ok=True)
        assert (r["evidence"], r["chunks"]) == (3, 1)
        assert r["ok"] is False
        assert "evidence<=chunks 위반" in r["reasons"], f"사유 문구가 다르다: {r['reasons']}"


class TestMissingFileIsNotAVerdict:
    """노드·엣지 **둘 다** 있어야 검사할 수 있다. 하나라도 없으면 `None`(검사 불가)이다.

    `and` 가 `or` 로 바뀌면 한쪽만 있어도 검사를 진행해 **없는 파일을 0건으로** 읽는다 —
    "엣지 0건이라 dangling 0" 이 되어 초록이 난다. 검사 불가와 통과는 다른 신호다.
    """

    @pytest.mark.parametrize("present", ["nodes", "edges"], ids=["only-nodes", "only-edges"])
    def test_one_sided_pack_is_uncheckable(self, tmp_path, present):
        d = tmp_path / present
        d.mkdir(parents=True)
        rows = [_n("a")] if present == "nodes" else [_e("a", "b")]
        (d / f"{present}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        assert check_pack(d) is None, f"{present} 만 있는데 판정을 냈다 — 검사 불가여야 한다"
