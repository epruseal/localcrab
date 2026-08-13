"""적재 전 grammar 정합 예측 — 이 팩을 적재하면 몇 개가 문법으로 튕기는가.

**게이트가 정본을 재구현하면 게이트 통과가 적재 성공을 뜻하지 않는다.** 이 파일의
이전 판은 적재기의 라벨·공간 해석 순서를 손으로 옮겨 적고 있었고, 그래서 게이트는
초록인데 적재에서 튕기는 엣지가 나왔다. 지금은 적재기와 **같은 함수**를 부른다:

    resolve_node_space_type   노드의 (space, node_type) -> 실제 적재될 space
    resolve_edge              라벨·방향 해석(반전 포함)
    validate_edge             grammar 판정

**이관으로 지연 로딩·폴백이 통째로 사라졌다.** 호출자 리포에 있던 시절에는 이 세
함수를 얻으려고 적재기를 import 해 vendor 경로를 얹고, 실패하면 `ALLOWED`/`KEEP`
스냅샷으로 폴백했다. 그 폴백이 바로 "게이트가 정본을 재구현한다"는 문제의 잔재였고
(스냅샷은 반전 관계를 다르게 처리한다), 동시에
`적재기 -> pack_gate -> grammar_fit -> 적재기` **순환 임포트**의 원인이었다.
같은 패키지로 오면서 셋 다 없어진다 — import 한 줄이면 된다.

**판정만 하고 출력하지 않는다.** 형식·argv·종료코드는 호출자 CLI 몫이다.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from opencrab.grammar.validator import validate_edge as _validate_edge
from opencrab.pack.normalize import resolve_edge, resolve_node_space_type
from opencrab.pack.schema import ALLOWED


def edge_allowed(from_space: str, to_space: str, relation: str) -> bool:
    """`(from_space, to_space, relation)` 이 grammar 상 유효한가. `validate_edge` 가 권위다."""
    return bool(_validate_edge(from_space, to_space, relation))


def resolve_effective_space(space: str, node_type: str) -> str:
    """노드의 `(space, node_type)` 에서 **실제 적재 시 쓰일** space.

    원본 space 가 아니다 — `NODE_TYPE_OVERRIDE` 등으로 재태깅되면 달라진다.
    적재기와 같은 함수를 쓰므로 여기서 그 규칙을 다시 쓰지 않는다.
    """
    return resolve_node_space_type(space, node_type)[0]


def effective_spaces(nodes) -> dict[str, str]:
    """id -> effective space 맵."""
    return {
        n["id"]: resolve_effective_space(n.get("space", "concept"),
                                         n.get("node_type", "Concept"))
        for n in nodes
    }


def fits(label: str, ss: str, ts: str) -> bool:
    """적재 시 이 엣지가 grammar 를 통과하는가. `ss`/`ts` 는 effective space.

    반전 관계면 `resolve_edge` 가 **뒤바뀐 space 를 그대로** 돌려주므로 여기서 다시
    뒤집지 않는다. 손으로 뒤집던 판이 반전 엣지를 오탐으로 만들었다.
    """
    a, rel, b, _reversed = resolve_edge(label, ss, ts)
    return edge_allowed(a, b, rel)


def predict_grammar_fit(nodes, edges) -> dict[str, Any]:
    """팩의 nodes/edges 로부터 적재 시 grammar 정합을 예측한다.

    `missing_endpoint` 는 정합 판정 대상에서 빠진 엣지다 — `total_edges` 에 안 들어가고
    별도로 센다. 둘을 합치면 "정합률"이 endpoint 누락을 희석해 버린다.
    """
    space = effective_spaces(nodes)
    total = miss = 0
    bad: Counter = Counter()
    for e in edges:
        s = e.get("source_id") or e.get("from_id")
        t = e.get("target_id") or e.get("to_id")
        lab = e.get("label") or e.get("relation") or ""
        ss, ts = space.get(s), space.get(t)
        if ss is None or ts is None:
            miss += 1
            continue
        total += 1
        if fits(lab, ss, ts):
            continue
        bad[(lab, ss, ts)] += 1

    viol = sum(bad.values())
    violations_detail = [
        {
            "label": lab, "from_space": ss, "to_space": ts, "count": n,
            "allowed": sorted(ALLOWED.get((ss, ts), set())) or None,
        }
        for (lab, ss, ts), n in bad.most_common()
    ]
    return {
        "pass": viol == 0 and miss == 0,
        "total_edges": total,
        "violations": viol,
        "missing_endpoint": miss,
        "top_violations": [f"{c}x {ss}→{ts}:{lab}" for (lab, ss, ts), c in bad.most_common(5)],
        "violations_detail": violations_detail,
        "detail": f"엣지 {total}건 중 grammar 미정합 {viol}건, endpoint 누락 {miss}건",
    }
