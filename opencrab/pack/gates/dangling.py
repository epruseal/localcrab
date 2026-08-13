"""by-pack 산출물 무결성 점검 — dangling 엣지와 evidence/chunks 대사.

배경: 이전 증분에서 dangling 점검을 `source`/`target` 같은 **추측 키**로 인라인 수행해
전 엣지가 오탐으로 잡혔다. 교환 포맷의 실제 키는 엣지가 `source_id`/`target_id`,
노드가 `id` 다. 그 정본 키를 여기 한 곳에 고정해 추측을 원천 차단한다.

점검 두 가지 — **둘 다 판정에 반영한다.** 출력만 하고 판정에서 빠뜨리면 게이트가
초록인데 결함이 남는다:

  · dangling  — 모든 엣지의 `source_id`/`target_id` 가 `nodes.jsonl` 의 id 집합에 있다
  · evidence  — `space == "evidence"` 노드 수 == `chunks.jsonl` 줄 수.
                증분 누적 팩처럼 `evidence < chunks` 가 **태생인** 팩은 `ev_lt_ok=True`
                로 완화한다(초과는 그래도 위반이다 — 완화는 한쪽 방향뿐이다).

**판정만 하고 출력하지 않는다.** 어느 팩을 검사할지, 어떤 팩을 완화할지, 결과를 어떻게
보여줄지는 전부 호출자의 정책이다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from opencrab.pack.jsonl_io import iter_jsonl, jsonl_exists

# 정본 키. 이 세 값이 이 모듈의 존재 이유다 — 추측 키로 인라인 점검하면 오탐이 난다.
EDGE_SRC = "source_id"
EDGE_TGT = "target_id"
NODE_ID = "id"


def load_ids(path: Path | str) -> set[str]:
    """`nodes.jsonl` 의 id 집합. shard 분할 팩도 논리 스트림으로 읽는다."""
    return {row[NODE_ID] for row in iter_jsonl(path)}


def check_pack(pack_dir: Path | str, *, ev_lt_ok: bool = False) -> dict[str, Any] | None:
    """팩 하나를 점검한다. 노드·엣지 파일이 없으면 `None`(검사 불가).

    반환 dict: `ok`·`nodes`·`edges`·`dangling`·`evidence`·`chunks`·`reasons`.
    `reasons` 는 위반 사유 목록이며 `ok` 가 True 면 빈 리스트다.
    """
    d = Path(pack_dir)
    nfile, efile, cfile = d / "nodes.jsonl", d / "edges.jsonl", d / "chunks.jsonl"
    if not (jsonl_exists(nfile) and jsonl_exists(efile)):
        return None

    ids = load_ids(nfile)
    n_ev = sum(1 for row in iter_jsonl(nfile) if row.get("space") == "evidence")
    n_edge = dangling = 0
    for e in iter_jsonl(efile):
        n_edge += 1
        if e[EDGE_SRC] not in ids or e[EDGE_TGT] not in ids:
            dangling += 1
    n_chunk = sum(1 for _ in iter_jsonl(cfile, missing_ok=True))

    ev_ok = (n_ev <= n_chunk) if ev_lt_ok else (n_ev == n_chunk)
    reasons: list[str] = []
    if dangling:
        reasons.append("dangling>0")
    if not ev_ok:
        reasons.append(f"evidence{'<=' if ev_lt_ok else '=='}chunks 위반")
    return {
        "ok": not reasons,
        "nodes": len(ids),
        "edges": n_edge,
        "dangling": dangling,
        "evidence": n_ev,
        "chunks": n_chunk,
        "reasons": reasons,
    }
