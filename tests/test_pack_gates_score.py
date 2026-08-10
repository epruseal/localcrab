"""``opencrab.pack.gates.score`` 계약.

이 게이트는 팩 품질을 100점 루브릭으로 판정한다. 여기서 거는 것은 넷 + **상수 격자**다.

**왜 격자가 따로 필요한가.** 아래 네 항목은 루브릭의 *성질*을 건다 — 합이 100인가,
None 과 저품질이 구분되는가, 추적 타깃이 evidence 뿐인가, 비구조 키가 경고인가.
그런데 성질만 걸면 **축을 열어 놓고 그 위의 한 점만 고정**하게 된다. 만점 팩 하나로는
감점 분기·임계 분기·반올림 분기를 **한 번도 지나지 않는다.** 적대 검증이 실측으로 보였다:
임계 90->80, 위반당 감점 3->2, 규모 임계 완화, `round`->`int` 를 넣어도 이 파일이 전부 통과했고
128팩 재채점에서는 최대 6팩·6점이 실제로 움직였다(2026-08-10).

그래서 `TestConstantGrid` 는 **축마다 정확히 하나의 픽스처**를 두고 기대 `sections`·`total` 을
**리터럴로** 박는다. `grade_pack` 에서 유도하면 상수가 바뀔 때 기대값도 같이 바뀌어 자기참조가 된다.

**종료 조건과 그 재측정 방법.** "테스트를 늘렸다"는 검출력의 증거가 아니다. 수치로 잰다::

    python scripts/qa/mutate_module.py <클론> \\
        opencrab/pack/gates/score.py tests/test_pack_gates_score.py /tmp/s.json

측정(2026-08-10, 격리 클론): 총 326 · KILLED 317 · BROKEN 0 · HUNG 0 · **생존 9**.
남은 9 는 전부 **등가 증명이 붙어 있고** `TestProvenEquivalences` 가 그 전제를 계약으로
건다 — 전제가 깨지면 거기서 빨간불이 난다.

격자 도입 **전**(커밋 `0891447^`, 7 tests)에는 **생존 162** 였다. 재현::

    git show 0891447^:tests/test_pack_gates_score.py > <클론>/tests/test_pack_gates_score.py
    python scripts/qa/mutate_module.py <클론> \\
        opencrab/pack/gates/score.py tests/test_pack_gates_score.py /tmp/pre.json

.. note::

   이 자리에 한동안 **"생존 58"** 이라고 적혀 있었다. 거짓이다 — 58 은 격자를 이미
   46 테스트만큼 넣은 뒤의 **중간 측정치**였고, 그것을 "격자 전"으로 인용했다.
   두 독립 검증자가 각각 162 를 재현해 잡아냈다.
   그래서 이 파일의 수치는 **(값, 그 값을 낸 커밋, 재현 명령)** 셋을 함께 적는다.
   커밋을 적는 순간 중간 측정치는 기준선으로 쓸 수 없게 된다.

===========================  ===  ===================================================
잔존 변이                    수   등가 근거
===========================  ===  ===================================================
L56-57 label/relation 정규화   5   루브릭이 엣지 label 을 **한 번도 읽지 않는다**
L189 ``s6 = max(0, s6)``       2   임계가 셋뿐이라 s6 치역이 {1,4,7,10} — 하한 도달 불가
L87 ``most_common(1)[0]``      1   ``most_common(k)[0]`` 은 k>=1 이면 동일 원소
L100 ``min(1.0, ...)``         1   ``1 == 1.0`` 이고 결과는 ``round()`` 로만 쓰인다
===========================  ===  ===================================================

**주의**: 이 도구는 복합(두 위치 동시) 변이를 하지 않는다. "전부 훑었다"로 읽지 마라.

여기서 거는 것은 넷이다.

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

import ast
import json
import pathlib
import re
from collections import defaultdict

import pytest

from opencrab.pack.gates import score as gs
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


# ══════════════════════════════════════════════════════════════════════════
# 상수 격자
#
# 기준 팩(`_base`)은 9-space 각 4노드·엣지 0·청크 0 이다. 각 픽스처는 거기서
# **정확히 한 축만** 흔들고, 기대 sections·total 을 리터럴로 박는다.
# 기준 팩 자신도 격자의 한 행이다 — 그것이 흔들리면 나머지 행의 의미가 사라진다.
# ══════════════════════════════════════════════════════════════════════════

SPACES = ["subject", "community", "policy", "claim", "concept",
          "resource", "evidence", "lever", "outcome"]


def _gn(i, sp, **extra):
    return {"id": i, "label": i, "node_type": "Concept", "space": sp, **extra}


def _ge(i, s, t, label="EVIDENCED_BY"):
    return {"id": i, "source_id": s, "target_id": t, "label": label}


def _base(**counts):
    """9-space 각 4노드. `counts` 로 특정 space 개수만 덮어쓴다."""
    c = dict.fromkeys(SPACES, 4)
    c.update(counts)
    return [_gn(f"{sp}{i}", sp) for sp in SPACES for i in range(c[sp])]


def _fx_base():
    return _base(), [], []


def _fx_empty_space():
    return _base(lever=0), [], []


def _fx_sparse_space():
    return _base(outcome=2), [], []


def _fx_balance_at_threshold():
    """비-evidence 100 중 최다 60 = 정확히 60%. `<=` 라 감점 없음."""
    return _base(concept=60, subject=6, community=6, policy=6, claim=6,
                 resource=6, lever=5, outcome=5), [], []


def _fx_balance_over_threshold():
    """62/97 = 63.9%. 임계 0.60·기울기 50 을 동시에 갈라낸다."""
    return _base(concept=62, evidence=5, subject=5, community=5, policy=5,
                 claim=5, resource=5, lever=5, outcome=5), [], []


def _fx_coverage_over_one():
    """소스 6 > 기대 3. `min(1.0, ...)` 이 없으면 s3=40 이 되어 총점이 100 을 넘는다."""
    nodes = _base(resource=3, evidence=6)
    chunks = [{"id": f"evidence{i}", "document_id": "resource0", "source": f"src{i}",
               "metadata": {"evidence_index": 1}} for i in range(6)]
    return nodes, [], chunks


def _fx_coverage_round_half():
    """cov=7/8 -> 20*0.875 = 17.5. `round` 면 18, `int` 면 17."""
    nodes = _base(resource=8, evidence=8)
    chunks = [{"id": f"evidence{i}", "document_id": f"resource{i}", "source": f"resource{i}",
               "metadata": {"evidence_index": 1}} for i in range(7)]
    return nodes, [], chunks


def _fx_trace_875():
    """추적 대상 16 중 14 연결 = 87.5%. 임계가 0.85 면 만점이 된다."""
    nodes = _base()
    need = [f"{sp}{i}" for sp in ("policy", "claim", "lever", "outcome") for i in range(4)]
    return nodes, [_ge(f"t{k}", nid, "evidence0") for k, nid in enumerate(need[:14])], []


def _fx_trace_full():
    """추적 대상 **전량 연결** = 만점 분기. 격자에 만점 행이 없으면 만점 상수가 열린다.

    적대 검증이 실증했다: `s4 = 20 if rate >= 0.90` 의 `20` 을 `21` 로 바꾸면
    격자·규모 표는 **전부 통과**하는데(47 passed) 128팩 채점이 바뀐다. 다른 클래스가
    잡고 있었을 뿐 격자 자신은 그 분기를 한 번도 지나지 않았다.
    """
    nodes = _base()
    need = [f"{sp}{i}" for sp in ("policy", "claim", "lever", "outcome") for i in range(4)]
    return nodes, [_ge(f"t{k}", nid, "evidence0") for k, nid in enumerate(need)], []


def _fx_trace_interior():
    """임계도 만점도 아닌 **구간 내부** 한 점(55%).

    경계 양쪽만 고정하면 내부를 바꾸는 변이가 통과한다 — 설계 검증이 인메모리 모델로
    보였다(`pinned (1.0, 0.875)` 상태에서 `0.55 -> 11` 변이가 수용됨).
    유한한 두 점은 구간을 닫지 못하므로 세 번째 점을 둔다.
    """
    nodes = _base(policy=5, claim=5, lever=5, outcome=5)
    need = [f"{sp}{i}" for sp in ("policy", "claim", "lever", "outcome") for i in range(5)]
    return nodes, [_ge(f"t{k}", nid, "evidence0") for k, nid in enumerate(need[:11])], []


def _fx_trace_policy_unlinked():
    """claim·lever·outcome 만 연결. policy 가 추적 대상에서 빠지면 만점이 된다."""
    nodes = _base()
    linked = [f"{sp}{i}" for sp in ("claim", "lever", "outcome") for i in range(4)]
    return nodes, [_ge(f"t{k}", nid, "evidence0") for k, nid in enumerate(linked)], []


def _fx_one_violation():
    return _base(), [_ge("d1", "claim0", "ghost")], []


def _fx_two_violations():
    """양끝 모두 미존재 = 2건. 1건 픽스처와 함께 **감점 계수 3** 을 결정한다."""
    return _base(), [_ge("d1", "ghost1", "ghost2")], []


def _fx_dangling_source():
    """src 미존재 분기(위반 상세 메시지)를 지나게 한다."""
    return _base(), [_ge("g1", "ghost", "evidence0"),
                     _ge("c1", "claim0", "resource0", "CITES")], []


def _fx_chunk_anchor_violations():
    """document_id 가 resource 아님 + id 가 evidence 아님 = 2건."""
    chunks = [{"id": "notevidence", "document_id": "notaresource", "source": "s",
               "metadata": {"evidence_index": 1}}]
    return _base(), [], chunks


def _fx_index_zero_based():
    """0-based 는 불연속으로 잡혀야 한다(1건)."""
    chunks = [{"id": f"evidence{i}", "document_id": "resource0", "source": "s",
               "metadata": {"evidence_index": i}} for i in range(2)]
    return _base(), [], chunks


def _fx_index_missing_three():
    """미부여 3건이지만 감점은 **팩당 1건**. 건당이면 s5 가 6 이 된다."""
    chunks = [{"id": f"evidence{i}", "document_id": "resource0", "source": "s"}
              for i in range(3)]
    return _base(), [], chunks


def _fx_index_out_of_order():
    """도착 순서가 뒤집혀도 **집합이 연속이면** 위반이 아니다(`sorted` 가 하는 일)."""
    chunks = [{"id": f"evidence{i}", "document_id": "resource0", "source": "s",
               "metadata": {"evidence_index": 2 - i}} for i in range(2)]
    return _base(), [], chunks


def _fx_single_non_evidence():
    """비-evidence 가 딱 1개. 점수 **0 하한**과 `max(1, len(non_ev))` 를 동시에 가른다."""
    return [_gn("c0", "concept")] + [_gn(f"evidence{i}", "evidence") for i in range(4)], [], []


def _fx_balance_69():
    """비-evidence 100 중 최다 69 = 69%. 기울기가 50 이면 6, 51 이면 5 가 된다.

    앞의 63.9% 행은 50 과 51 을 못 가른다(둘 다 8 로 반올림). 임계만 잡고 기울기를
    열어 두는 것이 바로 이 파일이 닫으려는 형태라, 반올림 경계를 넘는 점을 따로 둔다.
    """
    return _base(concept=69, subject=5, community=5, policy=5, claim=5,
                 resource=5, lever=3, outcome=3), [], []


def _fx_six_violations():
    """위반 6건 -> 15-18 이 음수. 0 하한이 없으면 참조 무결성이 음수 점수가 된다."""
    return _base(), [_ge(f"d{i}", f"ghostA{i}", f"ghostB{i}") for i in range(3)], []


def _fx_one_resource_covered():
    """resource 1개를 청크가 덮음. 기대 소스 하한이 2 면 커버리지가 절반이 된다."""
    nodes = _base(resource=1)
    chunks = [{"id": "evidence0", "document_id": "resource0", "source": "resource0",
               "metadata": {"evidence_index": 1}}]
    return nodes, [], chunks


def _fx_mixed_edge_keys():
    """두 관례가 **한 엣지에** 다 있는 경우. 정규 키가 이긴다(덮어쓰지 않는다).

    `and` 가 `or` 로 바뀌면 정규 키가 대량 import 키로 덮여 양끝이 유령이 된다 —
    조건이 `not in` 이라 정상 엣지에서는 두 형태가 같은 답을 내므로 이 혼합 입력이 없으면
    영영 안 갈린다.
    """
    edge = {"id": "m1", "source_id": "claim0", "from_id": "ghostA",
            "target_id": "evidence0", "to_id": "ghostB", "label": "EVIDENCED_BY"}
    return _base(), [edge], []


# (축 이름, 픽스처, 기대 sections, 기대 total, 이 행이 무엇을 결정하는가)
GRID = [
    ("base", _fx_base,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 51,
     "기준선. 이 행이 흔들리면 나머지 행의 델타 해석이 전부 무의미해진다"),
    ("empty-space-costs-8", _fx_empty_space,
     {"space": 17, "balance": 10, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 43,
     "빈 space 감점 = 8"),
    ("sparse-space-costs-3", _fx_sparse_space,
     {"space": 22, "balance": 10, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 48,
     "노드 3개 미만 space 감점 = 3, 그리고 그 경계가 '<3'"),
    ("balance-threshold-is-inclusive-60", _fx_balance_at_threshold,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 51,
     "균형 임계 0.60 이고 비교가 `<=` (`<` 이면 여기서 감점이 난다)"),
    ("balance-slope-is-50", _fx_balance_over_threshold,
     {"space": 25, "balance": 8, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 49,
     "임계 0.60 + 기울기 50. 임계가 0.65 면 10, 기울기가 30 이면 9 가 된다"),
    ("coverage-is-capped-at-one", _fx_coverage_over_one,
     {"space": 25, "balance": 10, "source": 20, "trace": 0, "integrity": 15, "scale": 1}, 71,
     "`min(1.0, ...)` 상한. 없으면 s3=40 이고 total 이 100 을 넘는다"),
    ("coverage-rounds-not-truncates", _fx_coverage_round_half,
     {"space": 25, "balance": 10, "source": 18, "trace": 0, "integrity": 15, "scale": 1}, 69,
     "17.5 -> 18. `int` 면 17"),
    ("trace-threshold-is-90", _fx_trace_875,
     {"space": 25, "balance": 10, "source": 0, "trace": 19, "integrity": 15, "scale": 1}, 70,
     "추적 임계 0.90. 0.85 면 87.5% 가 만점이 된다"),
    ("trace-full-credit-is-20", _fx_trace_full,
     {"space": 25, "balance": 10, "source": 0, "trace": 20, "integrity": 15, "scale": 1}, 71,
     "만점 분기의 상수 20. 이 행이 없으면 20->21 변이가 격자를 통과한다(실증됨)"),
    ("trace-interior-point-55pct", _fx_trace_interior,
     {"space": 25, "balance": 10, "source": 0, "trace": 12, "integrity": 15, "scale": 1}, 63,
     "임계도 만점도 아닌 구간 내부. 두 점만으로는 구간이 안 닫힌다"),
    ("trace-includes-policy", _fx_trace_policy_unlinked,
     {"space": 25, "balance": 10, "source": 0, "trace": 17, "integrity": 15, "scale": 1}, 68,
     "추적 대상 = claim·lever·outcome·**policy**. policy 가 빠지면 만점"),
    ("integrity-deducts-3-per-violation", _fx_one_violation,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 12, "scale": 1}, 48,
     "위반 1건 -> 15-3"),
    ("integrity-scales-linearly", _fx_two_violations,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 9, "scale": 1}, 45,
     "위반 2건 -> 15-6. 1건 행과 묶여 계수가 3 임을 확정한다"),
    ("dangling-source-counts", _fx_dangling_source,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 12, "scale": 1}, 48,
     "src 미존재도 위반. 동시에 claim->resource 가 추적 점수를 못 얻는 것도 확인"),
    ("chunk-anchors-count-separately", _fx_chunk_anchor_violations,
     {"space": 25, "balance": 10, "source": 5, "trace": 0, "integrity": 9, "scale": 1}, 50,
     "document_id≠resource 와 id≠evidence 는 **각각** 1건"),
    ("evidence-index-is-one-based", _fx_index_zero_based,
     {"space": 25, "balance": 10, "source": 5, "trace": 0, "integrity": 12, "scale": 1}, 53,
     "0-based 는 불연속. 연속성 기준이 `range(1, n+1)`"),
    ("missing-index-counts-once-per-pack", _fx_index_missing_three,
     {"space": 25, "balance": 10, "source": 5, "trace": 0, "integrity": 12, "scale": 1}, 53,
     "미부여 3건이어도 감점은 1건분. 건당이면 s5=6"),
    ("index-order-does-not-matter", _fx_index_out_of_order,
     {"space": 25, "balance": 10, "source": 5, "trace": 0, "integrity": 15, "scale": 1}, 56,
     "연속성은 **집합**의 성질. `sorted` 가 빠지면 도착 순서가 위반이 된다"),
    ("scores-floor-at-zero", _fx_single_non_evidence,
     {"space": 0, "balance": 0, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 16,
     "s1·s2 의 0 하한, 그리고 `total_ne = max(1, ...)` 의 1"),
    ("balance-slope-is-exactly-50", _fx_balance_69,
     {"space": 25, "balance": 6, "source": 0, "trace": 0, "integrity": 15, "scale": 1}, 47,
     "69% 는 기울기 50 이면 6, 51 이면 5. 반올림 경계를 넘는 점"),
    ("integrity-floors-at-zero", _fx_six_violations,
     {"space": 25, "balance": 10, "source": 0, "trace": 0, "integrity": 0, "scale": 1}, 36,
     "위반 6건 -> 15-18 이 음수. 하한이 없으면 음수 점수가 나온다"),
    ("expected-sources-floor-is-one", _fx_one_resource_covered,
     {"space": 22, "balance": 10, "source": 20, "trace": 0, "integrity": 15, "scale": 1}, 68,
     "`max(1, len(res_ids))` 의 1. 2 면 커버리지가 절반이 된다"),
    ("canonical-edge-keys-win", _fx_mixed_edge_keys,
     {"space": 25, "balance": 10, "source": 0, "trace": 1, "integrity": 15, "scale": 1}, 52,
     "정규 키가 있으면 대량 import 키로 덮지 않는다(`and`, 그리고 키 이름 자체)"),
]


class TestConstantGrid:
    """배점·임계·반올림 상수를 축마다 한 점씩 고정한다."""

    @pytest.mark.parametrize("axis,build,sections,total,why",
                             GRID, ids=[row[0] for row in GRID])
    def test_axis(self, tmp_path, axis, build, sections, total, why):
        nodes, edges, chunks = build()
        r = grade_pack(_pack(tmp_path / axis, nodes, edges, chunks))
        assert r is not None
        assert r["sections"] == sections, f"[{axis}] 결정 대상: {why}"
        assert r["total"] == total, f"[{axis}] 결정 대상: {why}"

    @pytest.mark.parametrize("axis,build,sections,total,why",
                             GRID, ids=[row[0] for row in GRID])
    def test_counts_are_reported_under_stable_keys(self, tmp_path, axis, build,
                                                   sections, total, why):
        """`counts` 는 반환 계약의 일부다 — 키 이름과 값이 소비자 코드에 박혀 있다.

        점수만 걸면 `{"nodes": ...}` 를 `{"": ...}` 로 바꾸는 변이가 통과한다. 판정은
        그대로인데 호출자가 `KeyError` 로 죽는다 — 게이트가 초록인 채로 파이프라인이 끊긴다.
        """
        nodes, edges, chunks = build()
        r = grade_pack(_pack(tmp_path / f"cnt-{axis}", nodes, edges, chunks))
        assert r["counts"] == {"nodes": len(nodes), "edges": len(edges),
                               "chunks": len(chunks)}, f"[{axis}] counts 계약 위반"

    def test_grid_pins_each_section_at_both_ends(self):
        """6개 항목이 **만점과 그보다 낮은 값 양쪽**에서 고정되는가.

        격자 자신에 대한 비공허성 검사다. 1판은 "기준선에서 **움직였는가**"만 봤는데,
        그러면 **만점 분기를 한 번도 안 지나는** 축이 있어도 통과한다.
        실제로 그랬다 — `trace` 만점 상수 20 을 21 로 바꾸면 격자·규모 표는 전부
        통과하면서(47 passed) 128팩 채점이 바뀌었다(적대 검증 실증, 2026-08-10).
        "축을 열어 놓고 그 위의 한 점만 고정"의 재발이고, 이 검사가 그것을 못 봤다.

        만점(`full`)은 루브릭 배점이라 **리터럴로 적는다** — `grade_pack` 에서 유도하면
        배점이 바뀔 때 기대값도 같이 바뀌어 자기참조가 된다.
        """
        full = {"space": 25, "balance": 10, "source": 20,
                "trace": 20, "integrity": 15, "scale": 10}
        seen: dict[str, set[int]] = {k: set() for k in full}
        for _a, _b, sec, _t, _w in GRID:
            for k, v in sec.items():
                seen[k].add(v)
        for _n, _e, _c, scale, _w in SCALE_GRID:
            seen["scale"].add(scale)

        missing_max = {k for k, m in full.items() if m not in seen[k]}
        assert not missing_max, (
            f"만점 분기를 한 번도 안 지나는 항목: {sorted(missing_max)} — "
            "그 항목의 만점 상수는 격자로 안 닫힌다")
        missing_low = {k for k, m in full.items() if not any(v < m for v in seen[k])}
        assert not missing_low, (
            f"만점 아래 값이 없는 항목: {sorted(missing_low)} — 감점 분기가 안 닫힌다")


def _scale_pack(n_nodes, n_edges, n_chunks):
    """규모 임계만 보기 위한 팩. 무결성 위반 0 이 되도록 앵커를 맞춘다."""
    nodes = [_gn("r0", "resource")]
    nodes += [_gn(f"ev{i}", "evidence") for i in range(n_chunks)]
    nodes += [_gn(f"c{i}", "concept") for i in range(n_nodes - len(nodes))]
    assert len(nodes) == n_nodes, "픽스처 자신이 목표 노드 수를 못 맞췄다"
    ids = [n["id"] for n in nodes]
    edges = [_ge(f"e{i}", ids[i % len(ids)], ids[(i + 1) % len(ids)], "RELATES_TO")
             for i in range(n_edges)]
    chunks = [{"id": f"ev{i}", "document_id": "r0", "source": "r0",
               "metadata": {"evidence_index": i + 1}} for i in range(n_chunks)]
    return nodes, edges, chunks


# (노드, 엣지, 청크, 기대 scale, 이 행이 결정하는 것)
SCALE_GRID = [
    (250, 300, 120, 10, "세 임계를 정확히 만족 -> 감점 없음"),
    (249, 300, 120, 7, "nodes 임계는 250 (249 는 미달)"),
    (250, 299, 120, 7, "edges 임계는 300"),
    (250, 300, 119, 7, "chunks 임계는 120"),
]


class TestScaleThresholds:
    """규모 임계 250/300/120 을 **경계 양쪽**으로 고정한다.

    임계 하나당 두 점(딱 맞음 / 하나 모자람)을 두는 이유는, 한 점만 두면 임계를 완화하는
    변이가 그 점을 계속 통과시키기 때문이다. 실측으로 250->150·300->200·120->60 변이가
    기존 테스트를 전부 통과했다.
    """

    @pytest.mark.parametrize("n_nodes,n_edges,n_chunks,scale,why", SCALE_GRID,
                             ids=["at-all-thresholds", "nodes-249", "edges-299", "chunks-119"])
    def test_boundary(self, tmp_path, n_nodes, n_edges, n_chunks, scale, why):
        nodes, edges, chunks = _scale_pack(n_nodes, n_edges, n_chunks)
        r = grade_pack(_pack(tmp_path / f"s{n_nodes}-{n_edges}-{n_chunks}",
                             nodes, edges, chunks))
        assert r["sections"]["scale"] == scale, why


class TestViolationDetailsAreReported:
    """위반 **상세 메시지** 분기. 판정이 맞아도 이유를 못 말하면 고칠 수가 없다.

    이 분기들(17줄)은 만점 팩만 보던 시절 한 번도 실행되지 않았다 — 커버리지 87% 의
    미실행분이 정확히 여기였다. 커버리지 숫자를 올리려는 게 아니라, **위반 픽스처가
    실제로 이 경로를 지나야** 메시지가 계약이 된다.
    """

    @pytest.mark.parametrize("build,needle", [
        (_fx_dangling_source, "edge src 미존재"),
        (_fx_one_violation, "edge tgt 미존재"),
        (_fx_chunk_anchor_violations, "chunk doc_id≠resource"),
        (_fx_chunk_anchor_violations, "chunk id≠evidence"),
        (_fx_index_missing_three, "evidence_index 미부여 3건"),
        (_fx_index_zero_based, "evidence_index 불연속"),
    ], ids=["edge-src", "edge-tgt", "chunk-doc", "chunk-id",
            "index-missing", "index-gap"])
    def test_detail_appears_in_gaps(self, tmp_path, build, needle):
        nodes, edges, chunks = build()
        r = grade_pack(_pack(tmp_path / "d", nodes, edges, chunks))
        integrity = [g for g in r["gaps"] if g.startswith("[integrity]")]
        assert integrity, "위반이 있는데 [integrity] 갭이 없다"
        assert any(needle in g for g in integrity), \
            f"{needle!r} 상세가 안 나왔다: {integrity}"


class TestReportShowsTheSameVerdictItComputed:
    """표시 문자열도 계약이다.

    판정은 맞고 **출력만** 틀리게 하는 변이(`{s1}/25` -> `{s1}/30`)가 있다. total 도
    sections 도 안 바뀌므로 위 격자로는 안 잡힌다. 그런데 운영자는 report 를 읽고
    팩을 고친다 — 분모가 거짓이면 어디를 얼마나 고쳐야 하는지 알 수 없다.
    """

    def test_each_line_carries_its_own_score_and_max(self, tmp_path):
        nodes, edges, chunks = _fx_balance_over_threshold()
        r = grade_pack(_pack(tmp_path / "rep", nodes, edges, chunks))
        expected = [("1", r["sections"]["space"], 25), ("2", r["sections"]["balance"], 10),
                    ("3", r["sections"]["source"], 20), ("4", r["sections"]["trace"], 20),
                    ("5", r["sections"]["integrity"], 15), ("6", r["sections"]["scale"], 10)]
        for idx, (num, got, mx) in enumerate(expected):
            line = r["report"][idx]
            assert re.match(rf"^{num}\. ", line), f"{idx}번째 줄이 '{num}. ' 로 시작하지 않는다: {line}"
            assert f": {got}/{mx}" in line, (
                f"{num}번 줄의 표시가 판정과 어긋난다 — 기대 '{got}/{mx}', 실제: {line}")

    def test_separator_width_is_stable(self, tmp_path):
        """구분선 폭은 표시 계약이다. 폭이 바뀌면 로그 파서와 눈금이 어긋난다."""
        r = grade_pack(_pack(tmp_path / "sep", *_fx_base()))
        assert "─" * 52 in r["report"]
        assert "\n" + "=" * 52 in r["report"]


class TestBulkImportEdgeKeysAreNormalized:
    """교환 포맷에 엣지 키 관례가 두 벌 섞여 있다.

    대량 import 포맷은 `from_id`/`to_id`/`relation` 을 쓴다. 정규화가 빠지면 그 팩의
    엣지가 **전량 양끝 미존재**로 잡혀 참조 무결성이 0 점이 되고, 추적성도 0 이 된다.
    판정이 조용히 틀리는 게 아니라 크게 틀리므로 등가로 못박는다.
    """

    def test_alternate_keys_score_identically(self, tmp_path):
        nodes = _base()
        need = [f"{sp}{i}" for sp in ("policy", "claim", "lever", "outcome") for i in range(4)]
        canonical = [_ge(f"t{k}", nid, "evidence0") for k, nid in enumerate(need[:14])]
        alternate = [{"id": e["id"], "from_id": e["source_id"],
                      "to_id": e["target_id"], "relation": e["label"]} for e in canonical]

        r_canon = grade_pack(_pack(tmp_path / "canon", nodes, canonical, []))
        r_alt = grade_pack(_pack(tmp_path / "alt", nodes, alternate, []))

        assert r_alt["sections"] == r_canon["sections"], (
            "from_id/to_id/relation 을 못 읽었다 — 이 포맷의 팩이 통째로 오판된다")
        assert r_alt["sections"]["trace"] == 19 and r_alt["sections"]["integrity"] == 15


class TestStructKeySetIsExactlyNine:
    """비구조 키 판정의 기준 집합을 **전 원소** 고정한다.

    한 원소만 확인하면 집합에서 다른 원소를 빼는 변이를 못 잡는다. 특히
    `workspace_id` 는 실팩에 실제로 존재하는 키라 빠지면 128팩이 통째로 경고를 받는다.
    """

    STRUCT = ["id", "workspace_id", "label", "node_type", "space",
              "source_type", "created_at", "properties", "degree"]

    @pytest.mark.parametrize("key", STRUCT)
    def test_structural_key_raises_no_shape_warning(self, tmp_path, key):
        nodes = _base()
        nodes[0] = _gn("subject0", "subject", **{key: "값"})
        r = grade_pack(_pack(tmp_path / f"k-{key}", nodes, [], []))
        assert not any("[shape]" in g for g in r["gaps"]), \
            f"{key!r} 는 구조 키인데 비구조로 경고했다"

    def test_a_non_member_still_warns(self, tmp_path):
        """대조군 — 집합을 통째로 넓히는 변이는 이 행에서 죽는다."""
        nodes = _base()
        nodes[0] = _gn("subject0", "subject", 아무키="값")
        r = grade_pack(_pack(tmp_path / "k-other", nodes, [], []))
        assert any("[shape]" in g for g in r["gaps"]), "비구조 키인데 경고가 없다"

    def test_the_set_is_exactly_these_nine_not_a_superset(self):
        """이름이 약속하는 것을 실제로 건다 — **정확히 이 9개**.

        위 두 검사는 행동만 본다. 그래서 생산 코드가 집합에 **키를 추가**해도 둘 다
        통과한다(설계 검증 지적: 상위집합만 고정하고 있다). 행동으로 "정확한 집합"을
        거는 것은 원리적으로 불가능하다 — 비회원을 전수 열거해야 하기 때문이다.
        그래서 **집합 자체**를 본다.

        소스를 AST 로 읽는 이유: `score.py` 는 이번 라운드에서 **무수정**이다(이관
        무결성 증거를 보존한다). 모듈 상수로 노출하면 더 깔끔하지만 그건 소스 변경이다.
        기대값은 리터럴이라 자기참조가 아니다 — 생산 집합이 바뀌면 여기서 깨진다.
        """
        src = pathlib.Path(gs.__file__).read_text(encoding="utf-8")
        found = None
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "struct_keys"
                    and isinstance(node.value, ast.Set)):
                found = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
                break
        assert found is not None, (
            "`struct_keys` 집합 리터럴을 못 찾았다 — 형태가 바뀌었으면 이 검사도 고쳐라. "
            "찾기 실패를 통과로 두면 검사가 조용히 죽는다")
        assert found == set(self.STRUCT), (
            f"구조 키 집합이 달라졌다: 추가 {sorted(found - set(self.STRUCT))} · "
            f"삭제 {sorted(set(self.STRUCT) - found)}")
        assert len(found) == 9


class TestEveryDeductionSaysWhy:
    """감점 6종이 **각각** 갭을 남기는가.

    점수만 맞으면 된다고 보면 갭 생성문을 지워도 격자가 전부 통과한다(실측: 감점 6종의
    `gaps.append` 를 하나씩 지운 변이가 전부 생존). 그런데 운영자는 점수가 아니라
    갭을 읽고 팩을 고친다 — 이유 없는 감점은 고칠 수 없는 감점이다.
    """

    @pytest.mark.parametrize("build,tag,needle", [
        (_fx_empty_space, "[space]", "공간 비어있음"),
        (_fx_sparse_space, "[space]", "노드 2개(<3)"),
        (_fx_balance_over_threshold, "[balance]", "(>60%)"),
        (_fx_base, "[source]", "청크 보유 소스 0/4"),
        (_fx_trace_875, "[trace]", "(<90%)"),
        (_fx_one_violation, "[integrity]", "위반 1건"),
        (_fx_base, "[scale]", "nodes=36(<250)"),
        (_fx_base, "[scale]", "edges=0(<300)"),
        (_fx_base, "[scale]", "chunks=0(<120)"),
    ], ids=["space-empty", "space-sparse", "balance", "source", "trace",
            "integrity", "scale-nodes", "scale-edges", "scale-chunks"])
    def test_gap_is_emitted(self, tmp_path, build, tag, needle):
        r = grade_pack(_pack(tmp_path / "g", *build()))
        tagged = [g for g in r["gaps"] if g.startswith(tag)]
        assert tagged, f"{tag} 감점이 났는데 갭이 없다: {r['gaps']}"
        assert any(needle in g for g in tagged), f"{needle!r} 가 없다: {tagged}"


class TestGapMessagesStayBounded:
    """갭 메시지의 **상한**도 계약이다.

    상한이 없으면 128팩 규모에서 갭 하나가 수천 항목을 뱉어 로그가 못 읽히게 된다.
    상한을 늘리는 변이는 판정을 안 바꾸므로 격자로는 안 잡힌다 — 여기서 잡는다.
    """

    def test_unlinked_examples_are_capped_at_eight(self, tmp_path):
        r = grade_pack(_pack(tmp_path / "u", *_fx_base()))   # 미연결 추적 노드 16개
        g = next(x for x in r["gaps"] if x.startswith("[trace]"))
        shown = ast.literal_eval(g.split("미연결 예: ", 1)[1])
        assert len(shown) == 8, f"미연결 예시 상한이 8 이 아니다: {len(shown)}개"

    def test_violation_details_are_capped_at_five(self, tmp_path):
        r = grade_pack(_pack(tmp_path / "v", *_fx_six_violations()))  # 위반 6건
        g = next(x for x in r["gaps"] if x.startswith("[integrity]"))
        assert "위반 6건" in g, f"위반 건수 자체는 전부 세야 한다: {g}"
        shown = ast.literal_eval(g.split("위반 6건: ", 1)[1])
        assert len(shown) == 5, f"상세 상한이 5 가 아니다: {len(shown)}개"

    def test_stray_key_names_are_capped_at_five_but_counted_in_full(self, tmp_path):
        nodes = _base()
        nodes[0] = _gn("subject0", "subject", **{f"군더더기{i}": "값" for i in range(6)})
        r = grade_pack(_pack(tmp_path / "s", nodes, [], []))
        g = next(x for x in r["gaps"] if x.startswith("[shape]"))
        assert "6종 6건" in g, f"종/건 집계가 틀렸다: {g}"
        shown = ast.literal_eval(g.split("6종 6건 ", 1)[1].split(" — ", 1)[0])
        assert len(shown) == 5, f"키 이름 상한이 5 가 아니다: {len(shown)}개"

    LONG_ID = "abcdefghIJKLMNOP"             # 16자 — 앞 8자만 보여야 한다

    def _assert_truncated(self, r, needles):
        g = next(x for x in r["gaps"] if x.startswith("[integrity]"))
        for needle in needles:
            assert needle + "abcdefgh" in g, f"{needle!r} 뒤 앞 8자가 안 보인다: {g}"
        assert "abcdefghI" not in g, f"8자를 넘겨 실렸다: {g}"

    def test_edge_ids_are_truncated_on_both_endpoints(self, tmp_path):
        """양끝 **각각**의 상세가 잘린다. 한쪽만 걸면 다른 쪽 상한이 열린 채 남는다."""
        edges = [_ge(self.LONG_ID, "ghostSrc", "ghostTgt")]
        r = grade_pack(_pack(tmp_path / "t-edge", _base(), edges, []))
        self._assert_truncated(r, ["edge src 미존재 ", "edge tgt 미존재 "])

    def test_chunk_ids_are_truncated_on_both_anchors(self, tmp_path):
        """document_id 앵커와 id 앵커의 상세가 **각각** 잘린다."""
        chunks = [{"id": self.LONG_ID, "document_id": "notaresource", "source": "s",
                   "metadata": {"evidence_index": 1}}]
        r = grade_pack(_pack(tmp_path / "t-chunk", _base(), [], chunks))
        self._assert_truncated(r, ["chunk doc_id≠resource ", "chunk id≠evidence "])


class TestReportRendersTheGapList:
    """report 는 표시 산출물이다 — 갭을 안 실으면 CLI 사용자에게는 갭이 없는 것과 같다."""

    def test_every_gap_appears_in_the_report_body(self, tmp_path):
        r = grade_pack(_pack(tmp_path / "rb", *_fx_base()))
        assert r["gaps"], "이 픽스처는 갭이 있어야 의미가 있다"
        assert "\n[갭 목록]" in r["report"], "갭 목록 머리글이 없다"
        for g in r["gaps"]:
            assert "  - " + g in r["report"], f"갭이 report 에 안 실렸다: {g}"

    def test_space_counts_are_rendered_space_to_count(self, tmp_path):
        """1번 줄의 집계는 `{space: 개수}` 다. 키와 값이 뒤집히면 읽을 수 없다."""
        r = grade_pack(_pack(tmp_path / "sc", *_fx_base()))
        assert "'subject': 4" in r["report"][0], f"공간별 집계 표기가 뒤집혔다: {r['report'][0]}"

    def test_node_type_tally_is_rendered(self, tmp_path):
        r = grade_pack(_pack(tmp_path / "nt", *_fx_base()))
        assert any("node_type: {'Concept': 36}" in x for x in r["report"]), \
            "node_type 집계 줄이 사라졌다"


class TestProvenEquivalences:
    """돌연변이 스윕에서 살아남되 **등가임이 증명된** 축. 추론이 아니라 측정으로 건다(D9).

    남겨 두는 이유는 두 가지다. ① 등가라고 적어만 두면 다음 사람이 그 주장을 재확인할
    방법이 없다 — 여기 테스트로 있으면 등가가 깨지는 순간 빨간불이 난다.
    ② `score.py` 소스는 이번 라운드에서 건드리지 않는다(이관 무결성 증거가 무효가 된다).
    """

    def test_edge_label_is_never_read_by_the_rubric(self, tmp_path):
        """L56-57 의 `label`/`relation` 정규화는 **채점에 무영향**이다.

        루브릭 어디에서도 엣지 label 을 읽지 않는다(노드 label 만 읽는다). 그래서 그
        그 두 줄에 도구가 만드는 변이는 **8종**이고, 격자 도입 전에는 8종 전부,
        현재는 **5종**이 생존한다(3종은 `e['relation']` 을 KeyError 로 터뜨려 죽는다 —
        label 을 읽어서가 아니다). 생존은 결함이 아니라 **도달 불가**다.
        생산자 계층이 label 을 요구하므로 줄 자체는 남긴다.

        이 단언이 깨지면 = 누가 label 을 채점에 쓰기 시작했다는 뜻이고, 그때는 그 5종이
        진짜 미검사 축이 되므로 격자에 행을 추가해야 한다.
        """
        nodes = _base()
        need = [f"{sp}{i}" for sp in ("policy", "claim", "lever", "outcome") for i in range(4)]
        with_label = [_ge(f"t{k}", nid, "evidence0", "EVIDENCED_BY")
                      for k, nid in enumerate(need)]
        no_label = [{k: v for k, v in e.items() if k != "label"} for e in with_label]
        odd_label = [{**e, "label": "아무거나"} for e in with_label]

        base = grade_pack(_pack(tmp_path / "lbl-a", nodes, with_label, []))
        for name, edges in (("lbl-b", no_label), ("lbl-c", odd_label)):
            r = grade_pack(_pack(tmp_path / name, nodes, edges, []))
            assert r["sections"] == base["sections"], \
                f"엣지 label 이 채점에 영향을 준다({name}) — 등가 증명이 깨졌다"

    def test_balance_uses_the_maximum_space_regardless_of_how_many_are_fetched(self, tmp_path):
        """`most_common(1)[0]` 의 `1` 은 동작 축이 아니다 — `most_common(k)[0]` 은 k>=1 이면
        전부 같은 최빈 원소다. 계약은 "몇 개를 꺼내는가"가 아니라 **최댓값을 쓰는가**다.

        여기서 거는 것은 그 계약이다. 이게 참인 한 그 상수를 흔드는 변이는 등가다.
        """
        nodes = _base(concept=69, subject=5, community=5, policy=5, claim=5,
                      resource=5, lever=3, outcome=3)
        r = grade_pack(_pack(tmp_path / "mc", nodes, [], []))
        assert "'concept' 69%" in r["report"][1], (
            f"최다 비-evidence space 를 concept(69/100)로 안 잡았다: {r['report'][1]}")

    def test_source_coverage_cap_is_numeric_not_typed(self, tmp_path):
        """`min(1.0, ...)` 의 `1.0` 은 `1` 로 바뀌어도 등가다 — 결과가 `round(20*cov)` 로만
        쓰이고 `1 == 1.0` 이라 int/float 차이가 표에 나타나지 않는다.

        계약은 **상한이 1 이라는 것**이지 그 리터럴의 타입이 아니다. 상한 자체는
        `coverage-is-capped-at-one` 행이 이미 건다. 여기서는 반환 타입만 못박는다 —
        타입이 float 로 새면 `total` 이 int 가 아니게 되어 소비자가 깨진다.
        """
        r = grade_pack(_pack(tmp_path / "cap", *_fx_coverage_over_one()))
        assert isinstance(r["sections"]["source"], int), "점수가 float 로 샜다"
        assert isinstance(r["total"], int)

    def test_scale_score_can_never_go_negative(self, tmp_path):
        """`s6 = max(0, s6)` 는 도달 불가다 — 임계가 셋뿐이라 최저가 10-9=1 이다.

        전수로 보인다: 3개 임계의 충족 여부 8가지 조합에서 s6 ∈ {1,4,7,10}.
        그래서 그 하한을 지우거나 1 로 바꾸는 변이가 생존한다. 결함이 아니다.
        """
        seen = set()
        for n_nodes, n_edges, n_chunks in [(250, 300, 120), (249, 300, 120),
                                           (250, 299, 120), (250, 300, 119),
                                           (249, 299, 120), (249, 300, 119),
                                           (250, 299, 119), (249, 299, 119)]:
            r = grade_pack(_pack(tmp_path / f"neg{n_nodes}{n_edges}{n_chunks}",
                                 *_scale_pack(n_nodes, n_edges, n_chunks)))
            seen.add(r["sections"]["scale"])
        assert seen == {1, 4, 7, 10}, f"규모 점수의 실제 치역이 바뀌었다: {sorted(seen)}"
        assert min(seen) >= 1, "0 하한이 도달 가능해졌다 — 등가 증명이 깨졌다"
