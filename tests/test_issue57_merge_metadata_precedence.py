"""#57: 중복 병합 시 metadata.update() 가 후순위 leg 값으로 앞선 값을 덮어씀.

``_merge_duplicate`` (opencrab/ontology/reranker.py) 는 같은 node_id 가 여러
leg(vector, bm25, fts, graph) 에서 나올 때 메타데이터를 합친다. 수정 전에는
``metadata.update(item.get("metadata") or {})`` 로 무조건 최신 leg 값을 채택했다.
leg 는 ``[vector, bm25, fts, graph]`` 순서로 병합되므로(opencrab/ontology/query.py),
graph leg 의 노드 properties 가 ``pack_id`` 를 빈 문자열로 갖고 있으면 vector leg 가
가진 유효한 ``pack_id`` 를 덮어 버렸다.

이 파일은 병합 규칙이 falsy 값으로 truthy 값을 덮지 않음을 고정한다.
"""

from __future__ import annotations

from opencrab.ontology.reranker import Reranker, _merge_duplicate


class TestMergeDuplicateMetadataPrecedence:
    """``_merge_duplicate`` 의 메타데이터 병합 규칙 — 키 단위 단위 테스트."""

    def test_truthy_existing_survives_falsy_incoming(self):
        """정상: 기존 값이 truthy, 새 leg 값이 falsy(빈 문자열) → 기존 값 유지."""
        existing = {"metadata": {"pack_id": "p1"}, "sources": ["vector"], "score": 0.9}
        item = {"metadata": {"pack_id": ""}, "source": "graph", "score": 0.5}

        _merge_duplicate(existing, item)

        assert existing["metadata"]["pack_id"] == "p1"

    def test_falsy_existing_upgraded_by_truthy_incoming(self):
        """정상: 기존 값이 falsy, 새 leg 값이 truthy → 새 값으로 갱신."""
        existing = {"metadata": {"pack_id": ""}, "sources": ["graph"], "score": 0.5}
        item = {"metadata": {"pack_id": "p2"}, "source": "vector", "score": 0.9}

        _merge_duplicate(existing, item)

        assert existing["metadata"]["pack_id"] == "p2"

    def test_both_truthy_last_wins(self):
        """정상: 두 leg 모두 truthy 하지만 값이 다르면 후순위 leg 값을 채택한다(비회귀)."""
        existing = {"metadata": {"pack_id": "p1"}, "sources": ["vector"], "score": 0.9}
        item = {"metadata": {"pack_id": "p2"}, "source": "graph", "score": 0.5}

        _merge_duplicate(existing, item)

        assert existing["metadata"]["pack_id"] == "p2"

    def test_missing_key_filled_with_falsy_value(self):
        """엣지: 기존에 키가 아예 없고 새 leg 가 falsy 값을 주면 그 값으로 채워진다(비회귀)."""
        existing = {"metadata": {}, "sources": ["vector"], "score": 0.9}
        item = {"metadata": {"pack_id": ""}, "source": "graph", "score": 0.5}

        _merge_duplicate(existing, item)

        assert existing["metadata"]["pack_id"] == ""

    def test_rule_applies_to_arbitrary_metadata_key(self):
        """엣지: pack_id 전용 특례가 아니라 임의의 메타데이터 키에도 같은 규칙이 적용된다."""
        existing = {"metadata": {"title": "확정된 제목"}, "sources": ["vector"], "score": 0.9}
        item = {"metadata": {"title": ""}, "source": "graph", "score": 0.5}

        _merge_duplicate(existing, item)

        assert existing["metadata"]["title"] == "확정된 제목"

    def test_both_falsy_key_stays_falsy(self):
        """엣지: 두 leg 모두 falsy 값이면 falsy 값으로 남는다(정보 손실 없음, 예외 없음)."""
        existing = {"metadata": {"pack_id": ""}, "sources": ["vector"], "score": 0.9}
        item = {"metadata": {"pack_id": None}, "source": "graph", "score": 0.5}

        _merge_duplicate(existing, item)

        assert not existing["metadata"]["pack_id"]


class TestRerankIntegrationPreservesValidPackId:
    """이슈 원문 재현 시나리오를 ``Reranker.rerank()`` 전체 경로로 통과시킨다."""

    def test_graph_leg_empty_pack_id_does_not_clobber_vector_leg(self):
        vector_hits = [
            {"source": "vector", "node_id": "n1", "score": 0.8, "text": "vector text",
             "metadata": {"pack_id": "valid-pack"}},
        ]
        graph_hits = [
            {"source": "graph", "node_id": "n1", "score": 0.6, "text": "graph text",
             "metadata": {"pack_id": ""}},
        ]

        merged = Reranker().rerank("query", [vector_hits, graph_hits], top_k=10)

        assert len(merged) == 1
        assert merged[0]["metadata"]["pack_id"] == "valid-pack"
