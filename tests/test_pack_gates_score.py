"""``opencrab.pack.gates.score`` 계약.

이 게이트는 팩 품질을 100점 루브릭으로 판정한다. 여기서 거는 것은 넷이다.

  · 배점 합이 실제로 100인가. 6개 항목 배점(25·10·20·20·15·10)은 함수 본문에 흩어진
    매직 넘버라 소스를 읽는 것만으로는 검증되지 않는다 — 실제로 만점을 받는 팩을
    만들어 합산하는 수밖에 없다.
  · `nodes.jsonl` 부재(검사 불가)와 저품질 팩(0점에 가까운 판정)이 다른 신호로
    남는가 — 하나로 뭉개면 파이프라인이 채점 실패와 저품질 팩을 구분 못한다.
  · 근거 추적성이 **evidence 타깃만** 인정하는가(2026-07-16 전수감사). 종전엔
    resource 타깃도 인정해 claim→resource 팩이 만점을 받았으나, 적재 시 그 엣지는
    grammar 로 skip돼 추적성이 실제로는 깨져 있었다(실물: 공공데이터품질관리
    CITES→resource).
  · 노드 최상위 비구조 키가 **감점이 아니라 경고**인가. 감점하면 -3이 pack_gate 의
    SCORE_THRESHOLD(90)를 넘겨 팩들이 신규 FAIL 이 되고 적재 대상에서 빠진다 —
    로더가 레거시 최상위를 흡수하게 만든 목적과 정면으로 상쇄된다(2026-08-03
    실측: HEAD PASS 109 -> 104). 의도된 설계다.
"""
from __future__ import annotations

import json
from collections import defaultdict

from opencrab.pack.gates.score import grade_pack


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


def _e(src, tgt, label="RELATES_TO"):
    return {"id": f"{src}->{tgt}", "source_id": src, "target_id": tgt, "label": label}


class TestRubricSumsToOneHundred:
    def test_all_sections_maxed_sum_to_100(self, tmp_path):
        """모든 항목이 만점인 팩을 손으로 지어 총점을 확인한다.

        기대값(25/10/20/20/15/10, 합 100)은 여기 문자 그대로 적는다 — `grade_pack`
        에서 유도하면 배점이 바뀌어도 테스트가 따라 바뀌어 자기참조가 된다.
        """
        non_trace = {"subject": 10, "community": 10, "concept": 10, "resource": 10}
        trace = {"policy": 15, "claim": 15, "lever": 15, "outcome": 15}
        ids: dict[str, list[str]] = defaultdict(list)
        nodes = []
        for sp, n in {**non_trace, **trace}.items():
            for i in range(n):
                nid = f"{sp}-{i}"
                nodes.append(_n(nid, space=sp))
                ids[sp].append(nid)
        ev_per_res = 15
        for rid in ids["resource"]:
            for j in range(ev_per_res):
                eid = f"evidence-{rid}-{j}"
                nodes.append(_n(eid, space="evidence"))
                ids["evidence"].append(eid)
        assert len(nodes) == 100 + 150  # 비-evidence 100 + evidence 150 = 250(규모 임계)

        chunks = []
        ev_iter = iter(ids["evidence"])
        for rid in ids["resource"]:
            for j in range(ev_per_res):
                chunks.append({"id": next(ev_iter), "document_id": rid, "source": rid,
                                "metadata": {"evidence_index": j + 1}})
        assert len(chunks) == 150

        edges = []
        for sp in trace:
            for k, nid in enumerate(ids[sp]):
                edges.append(_e(nid, ids["evidence"][k % len(ids["evidence"])], "EVIDENCED_BY"))
        assert len(edges) == 60
        for i in range(240):  # 규모 임계(edges>=300) 채우는 필러 — 무결성엔 영향 없음
            edges.append(_e(ids["subject"][i % 10], ids["community"][(i + 1) % 10], "MENTIONS"))
        assert len(edges) == 300

        d = _pack(tmp_path, nodes, edges, chunks)
        r = grade_pack(d)
        assert r["sections"] == {
            "space": 25, "balance": 10, "source": 20, "trace": 20, "integrity": 15, "scale": 10,
        }
        assert r["total"] == 100
        assert r["gaps"] == [], f"만점 팩인데 갭이 남았다: {r['gaps']}"


class TestMissingNodesFileIsNotAVerdict:
    def test_absent_nodes_file_returns_none(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert grade_pack(d) is None, "검사 불가와 판정(0점 포함)을 구분해야 한다"

    def test_low_quality_pack_is_a_verdict_not_none(self, tmp_path):
        """노드 1개짜리 저품질 팩은 `None` 이 아니라 낮은 total 을 가진 dict 여야 한다.

        `None`을 '0점'으로 뭉개면 파이프라인이 '채점 실패'와 '저품질 팩'을 구분 못한다.
        """
        d = _pack(tmp_path, [_n("a")], [], [])
        r = grade_pack(d)
        assert r is not None and isinstance(r["total"], int)
        assert r["total"] < 100


class TestTraceCreditsEvidenceOnly:
    def test_resource_target_earns_no_trace_credit(self, tmp_path):
        """claim→resource 엣지만으로는 추적성이 인정되면 안 된다.

        2026-07-16 전수감사 전에는 resource 타깃도 인정해 이런 팩이 만점을 받았으나
        적재 시 이 엣지는 grammar 로 skip 돼 추적성이 실제로는 깨져 있었다.
        """
        d = _pack(tmp_path,
                   [_n("c1", space="claim"), _n("r1", space="resource"),
                    _n("e1", space="evidence")],
                   [_e("c1", "r1", "CITES")])
        r = grade_pack(d)
        assert r is not None
        assert r["sections"]["trace"] == 0, "resource 타깃 엣지로 추적성 점수를 받았다"
        assert any("[trace]" in g for g in r["gaps"])

    def test_evidence_target_earns_full_trace_credit(self, tmp_path):
        """대조군 — 같은 모양의 엣지라도 타깃이 evidence 면 인정돼야 한다."""
        d = _pack(tmp_path,
                   [_n("c1", space="claim"), _n("e1", space="evidence")],
                   [_e("c1", "e1", "EVIDENCED_BY")])
        r = grade_pack(d)
        assert r["sections"]["trace"] == 20


class TestStrayTopLevelKeysAreAWarningNotADeduction:
    def test_stray_keys_do_not_lower_the_integrity_score(self, tmp_path):
        clean = _pack(tmp_path / "clean", [_n("a"), _n("b")], [_e("a", "b")])
        stray = _pack(tmp_path / "stray",
                       [{"id": "a", "label": "a", "node_type": "Concept",
                         "space": "concept", "custom_field": "값"}, _n("b")],
                       [_e("a", "b")])
        r_clean = grade_pack(clean)
        r_stray = grade_pack(stray)
        assert r_clean["sections"]["integrity"] == 15
        assert r_stray["sections"]["integrity"] == 15, "비구조 키가 참조 무결성을 깎았다"
        assert any("[shape]" in g for g in r_stray["gaps"]), "경고 자체가 사라지면 안 된다"
        assert not any("[shape]" in g for g in r_clean["gaps"])


class TestExpectedSourcesOverride:
    def test_explicit_expected_sources_replaces_resource_node_count(self, tmp_path):
        """`expected_sources` 를 주면 resource 노드 수 대신 그 값을 쓴다.

        resource 노드는 3개지만 실제로 청크를 가진 소스는 1개뿐인 팩 — 자동 산정
        (resource 수=3)이면 미달, `expected_sources=1`이면 충족이어야 한다.
        """
        nodes = [_n("r1", space="resource"), _n("r2", space="resource"),
                 _n("r3", space="resource"), _n("e1", space="evidence")]
        chunks = [{"id": "e1", "document_id": "r1", "source": "r1",
                   "metadata": {"evidence_index": 1}}]
        d = _pack(tmp_path, nodes, [], chunks)

        r_auto = grade_pack(d)
        r_forced = grade_pack(d, expected_sources=1)

        assert r_auto["sections"]["source"] < 20, "resource 3개 기준이면 미달이어야 정상"
        assert r_forced["sections"]["source"] == 20, "expected_sources=1 을 무시했다"
