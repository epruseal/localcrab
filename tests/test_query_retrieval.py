from __future__ import annotations

from opencrab.ontology.bm25 import BM25Index
from opencrab.ontology.query import HybridQuery
from opencrab.ontology.reranker import Reranker


def test_bm25_handles_korean_relation_questions() -> None:
    nodes = [
        {
            "node_id": "fireproof-reason",
            "space": "claim",
            "node_type": "Claim",
            "properties": {
                "title": "가구표면 방염 변경 사유",
                "description": "방염공사 시 가구표면 방염으로 변경된 이유와 사내 기준 배경.",
            },
        },
        {
            "node_id": "balcony-waterproofing",
            "space": "resource",
            "node_type": "Document",
            "properties": {
                "title": "발코니 바닥 방수 시공 기준",
                "description": "난방 발코니의 방수 시공 기준과 체크사항.",
            },
        },
    ]

    hits = BM25Index.build(nodes).search(
        "방염공사 시 가구표면 방염으로 변경된 이유 알려줘",
        limit=2,
    )

    assert hits
    assert hits[0]["node_id"] == "fireproof-reason"


def test_hybrid_query_expands_graph_from_bm25_anchor_for_relation_intent() -> None:
    class FakeChroma:
        available = False

    class FakeDocStore:
        def list_nodes(self, limit: int = 100):
            assert limit >= 50000
            return [
                {
                    "node_id": "rock-panel",
                    "space": "claim",
                    "node_type": "Claim",
                    "properties": {
                        "title": "Rock Panel 적용 불가 사유",
                        "description": "Rock Panel을 우리회사에서 적용 불가능한 이유와 관련 기준.",
                    },
                }
            ]

    class FakeGraph:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, int | str]] = []

        def find_neighbors(self, node_id: str, direction: str = "both", depth: int = 1, limit: int = 50):
            self.calls.append({"node_id": node_id, "depth": depth, "limit": limit})
            return [
                {
                    "properties": {
                        "id": "rock-panel-standard",
                        "title": "Rock Panel 사내 적용 제한 기준",
                        "reason": "내화성능과 유지관리 리스크 때문에 적용 불가로 관리한다.",
                    },
                    "labels": ["Claim"],
                    "relation_type": "supports",
                    "relationship_types": ["supports"],
                    "depth": 1,
                }
            ]

    graph = FakeGraph()
    hybrid = HybridQuery(FakeChroma(), graph)  # type: ignore[arg-type]
    hybrid._doc_store = FakeDocStore()

    results = hybrid.query("Rock Panel을 우리회사에서 적용 불가능한 이유 알려줘", limit=3)

    assert graph.calls
    assert graph.calls[0]["node_id"] == "rock-panel"
    assert graph.calls[0]["depth"] >= 2
    assert any(result.node_id == "rock-panel-standard" for result in results)


def test_reranker_boosts_consensus_and_relation_evidence() -> None:
    reranked = Reranker().rerank(
        "호이스트 편성기준 개정 이유 알려줘",
        [
            [
                {
                    "source": "bm25",
                    "node_id": "revision-reason",
                    "score": 3.0,
                    "text": "호이스트 편성기준 개정 이유와 기준 변경 배경",
                    "metadata": {},
                }
            ],
            [
                {
                    "source": "graph",
                    "node_id": "revision-reason",
                    "score": 0.6,
                    "text": "개정 배경을 supports 관계로 설명",
                    "metadata": {},
                    "graph_context": {"relation_type": "supports"},
                },
                {
                    "source": "vector",
                    "node_id": "generic-hoist",
                    "score": 0.9,
                    "text": "호이스트 일반 설치 기준",
                    "metadata": {},
                },
            ],
        ],
        top_k=2,
    )

    assert reranked[0]["node_id"] == "revision-reason"
    assert set(reranked[0]["sources"]) == {"bm25", "graph"}
