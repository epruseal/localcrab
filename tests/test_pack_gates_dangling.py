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
