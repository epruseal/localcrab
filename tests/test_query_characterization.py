"""Characterization tests for opencrab/ontology/query.py uncovered contracts.

Scope (see coverage gaps against the existing test_query_*.py / test_bm25_*.py
suite): BM25 debounced-rebuild worker lifecycle edge cases, the 3-way Chroma
where-filter fallback's final exception leg, the Neo4j(Docker) keyword_search
legacy cypher path (the local/kuzu export_nodes path is already fully covered
by test_query_keyword_local.py — 판단 보류 on asserting shape parity between
the two paths stands from Stage 1), _policy_filter, _graph_expand's pack_ids
forwarding + per-anchor exception isolation, ingest()'s early-return and
error-report branches, and a few small pure-function/orchestration branches
(_profile_for_query multihop cue, _ordered_unique limit break,
_property_text's no-known-field fallback, query()'s no-rerank flat merge).

BM25 worker tests synchronize via explicit threading.Event handshakes or a
short bounded poll (mirrors the existing pattern in test_bm25_fingerprint.py)
rather than fixed sleeps, and always call shutdown_bm25() in a finally block
so no daemon thread outlives its test.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from opencrab.ontology.query import (
    HybridQuery,
    _ordered_unique,
    _profile_for_query,
    _property_text,
)


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _node(node_id: str, text: str = "alpha beta", updated_at: str | None = None) -> dict:
    doc: dict = {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": {"name": text},
    }
    if updated_at:
        doc["updated_at"] = updated_at
    return doc


def _inert_hybrid() -> HybridQuery:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    return HybridQuery(chroma, neo4j)


def _hybrid_with_doc_store(doc_store) -> HybridQuery:
    hybrid = _inert_hybrid()
    hybrid._doc_store = doc_store
    hybrid._bm25_debounce = 0.0
    return hybrid


# ---------------------------------------------------------------------------
# BM25 debounced-rebuild worker — normal
# ---------------------------------------------------------------------------


class TestBm25WorkerNormal:
    def test_invalidate_triggers_rebuild_and_query_sees_fresh_index(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(return_value=[_node("a"), _node("b")])
        doc_store.bm25_fingerprint = MagicMock(return_value=(2, ""))
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            hybrid.invalidate_bm25_cache()
            assert _wait_until(lambda: hybrid._bm25_cache is not None)
            assert _wait_until(lambda: hybrid._bm25_cache_size == 2)
            hits = hybrid._bm25_search("alpha", spaces=None, limit=5)
            assert {h["node_id"] for h in hits} == {"a", "b"}
        finally:
            hybrid.shutdown_bm25()

    def test_worker_skips_ref_swap_when_fingerprint_unchanged(self) -> None:
        """Line contract: cache is not None and fp == cache.fingerprint -> dirty
        cleared but no new object is built (ref identity preserved)."""
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(return_value=[_node("a")])
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            hybrid._bm25_search("x", spaces=None, limit=5)  # cold synchronous build
            first_cache = hybrid._bm25_cache
            hybrid.invalidate_bm25_cache()  # corpus unchanged -> "nothing changed" leg
            assert _wait_until(lambda: doc_store.list_nodes.call_count >= 2)
            assert hybrid._bm25_cache is first_cache
            assert hybrid._bm25_dirty is False
        finally:
            hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# BM25 debounced-rebuild worker — error
# ---------------------------------------------------------------------------


class TestBm25WorkerError:
    def test_rebuild_exception_does_not_kill_worker_or_future_rebuilds(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(
            side_effect=[RuntimeError("boom"), [_node("a")]]
        )
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            hybrid.invalidate_bm25_cache()
            assert _wait_until(lambda: doc_store.list_nodes.call_count >= 1)
            assert hybrid._bm25_worker.is_alive()
            assert hybrid._bm25_cache is None  # first pass raised, cache untouched

            hybrid.invalidate_bm25_cache()  # worker still alive -> serves this too
            assert _wait_until(lambda: hybrid._bm25_cache is not None)
            assert hybrid._bm25_cache_size == 1
        finally:
            hybrid.shutdown_bm25()

    def test_bm25_search_exception_returns_empty_list(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(side_effect=RuntimeError("boom"))
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            assert hybrid._bm25_search("q", spaces=None, limit=5) == []
        finally:
            hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# BM25 debounced-rebuild worker — edge
# ---------------------------------------------------------------------------


class TestBm25WorkerEdge:
    def test_invalidate_during_inflight_rebuild_reruns(self) -> None:
        """A second invalidate that lands while a rebuild is already running
        must not be dropped: the epoch mismatch schedules exactly one more
        pass after the in-flight build settles."""
        entered_build = threading.Event()
        release_build = threading.Event()

        def slow_list_nodes(limit):
            entered_build.set()
            release_build.wait(timeout=2.0)
            return [_node("a")]

        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(side_effect=slow_list_nodes)
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            hybrid.invalidate_bm25_cache()
            assert entered_build.wait(timeout=2.0), "worker never entered the build"
            hybrid.invalidate_bm25_cache()  # arrives mid-build
            release_build.set()
            assert _wait_until(lambda: doc_store.list_nodes.call_count >= 2)
        finally:
            release_build.set()
            hybrid.shutdown_bm25()

    def test_shutdown_during_debounce_wait_aborts_before_building(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(return_value=[])
        hybrid = _hybrid_with_doc_store(doc_store)
        hybrid._bm25_debounce = 5.0  # long enough that shutdown lands mid-wait
        hybrid.invalidate_bm25_cache()
        # Bounded wait for the OS to actually schedule the new thread before we
        # race it with shutdown (not a condition-polling spin: single settle point).
        time.sleep(0.05)
        hybrid.shutdown_bm25(timeout=3.0)
        assert not hybrid._bm25_worker.is_alive()
        assert doc_store.list_nodes.call_count == 0

    def test_shutdown_is_idempotent(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(return_value=[_node("a")])
        hybrid = _hybrid_with_doc_store(doc_store)
        hybrid.invalidate_bm25_cache()
        hybrid.shutdown_bm25(timeout=2.0)
        assert not hybrid._bm25_worker.is_alive()
        hybrid.shutdown_bm25(timeout=1.0)  # second call must not raise or hang

    def test_bm25_join_waits_for_inflight_worker(self) -> None:
        doc_store = MagicMock()
        doc_store.available = True
        doc_store.list_nodes = MagicMock(return_value=[_node("a")])
        hybrid = _hybrid_with_doc_store(doc_store)
        try:
            assert hybrid._bm25_worker is None
            hybrid._bm25_join(timeout=0.1)  # no worker yet -> no-op, no raise
            hybrid.invalidate_bm25_cache()
            hybrid._bm25_join(timeout=2.0)  # worker alive -> actually joins
            assert hybrid._bm25_cache is not None
        finally:
            hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# _bm25_probe_fingerprint — normal/error/edge
# ---------------------------------------------------------------------------


class TestBm25ProbeFingerprint:
    def test_no_doc_store_returns_none(self) -> None:
        hybrid = _inert_hybrid()
        hybrid._doc_store = None
        assert hybrid._bm25_probe_fingerprint() is None

    def test_legacy_store_without_cheap_probe_falls_back_to_compute_fingerprint(
        self,
    ) -> None:
        class LegacyStore:
            def list_nodes(self, limit):
                return [{"node_id": "a", "updated_at": "2026-01-01"}]

        hybrid = _inert_hybrid()
        hybrid._doc_store = LegacyStore()
        assert hybrid._bm25_probe_fingerprint() == (1, "2026-01-01")

    def test_exception_returns_none(self) -> None:
        class BrokenStore:
            def list_nodes(self, limit):
                raise RuntimeError("boom")

        hybrid = _inert_hybrid()
        hybrid._doc_store = BrokenStore()
        assert hybrid._bm25_probe_fingerprint() is None


# ---------------------------------------------------------------------------
# _vector_search 3-way fallback — final exception leg
# ---------------------------------------------------------------------------


class TestVectorSearchDoubleFailure:
    def test_both_primary_and_fallback_raise_returns_empty(self) -> None:
        """Server-side where AND the wider post-filter fallback both raise ->
        caught by the outer except, degrades to an empty result (not a crash)."""
        chroma = MagicMock()
        chroma.available = True
        chroma.query = MagicMock(side_effect=RuntimeError("boom"))
        neo4j = MagicMock()
        neo4j.available = False
        hybrid = HybridQuery(chroma, neo4j)
        results = hybrid._vector_search(
            "x", spaces=None, limit=5, pack_ids=["pack-a"], include_unpackaged=False
        )
        assert results == []
        assert chroma.query.call_count == 2

    def test_fallback_drops_orphan_hits_when_include_unpackaged_false(self) -> None:
        """Fallback path always sets an effective_pack_filter (regardless of
        include_unpackaged); with include_unpackaged=False an orphan hit (no
        derivable pack_id) is dropped rather than passed through."""
        orphan = {"id": "v1", "document": "orphan", "metadata": {"node_id": "n1"}, "distance": 0.1}
        matching = {
            "id": "v2",
            "document": "match",
            "metadata": {"pack_id": "pack-a", "node_id": "n2"},
            "distance": 0.2,
        }
        call_state = {"first": True}

        def fake_query(**kwargs):
            if call_state["first"]:
                call_state["first"] = False
                raise RuntimeError("simulated where rejection")
            return [orphan, matching]

        chroma = MagicMock()
        chroma.available = True
        chroma.query = MagicMock(side_effect=fake_query)
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        results = hybrid._vector_search(
            "x", spaces=None, limit=5, pack_ids=["pack-a"], include_unpackaged=False
        )
        assert [r.node_id for r in results] == ["n2"]


# ---------------------------------------------------------------------------
# keyword_search legacy shim — Neo4j(Docker) cypher path
# ---------------------------------------------------------------------------


class TestKeywordSearchNeo4jPath:
    def test_neo4j_unavailable_returns_empty(self) -> None:
        neo4j = MagicMock()
        neo4j.available = False
        hybrid = HybridQuery(MagicMock(available=False), neo4j)
        assert hybrid.keyword_search("x") == []

    def test_cypher_path_returns_rows_and_forwards_params(self) -> None:
        neo4j = MagicMock()
        neo4j.available = True
        neo4j.run_cypher = MagicMock(
            return_value=[{"props": {"name": "n1"}, "label": "Concept"}]
        )
        hybrid = HybridQuery(MagicMock(available=False), neo4j)
        results = hybrid.keyword_search("term", spaces=["s1"], limit=5)
        assert results == [{"node": {"name": "n1"}, "label": "Concept"}]
        _cypher, params = neo4j.run_cypher.call_args.args
        assert params["kw"] == "term"
        assert params["spaces"] == ["s1"]
        assert params["limit"] == 5

    def test_cypher_path_exception_returns_empty(self) -> None:
        neo4j = MagicMock()
        neo4j.available = True
        neo4j.run_cypher = MagicMock(side_effect=RuntimeError("boom"))
        hybrid = HybridQuery(MagicMock(available=False), neo4j)
        assert hybrid.keyword_search("x") == []


# ---------------------------------------------------------------------------
# _policy_filter — normal/error/edge
# ---------------------------------------------------------------------------


class TestPolicyFilter:
    def test_item_without_node_id_passes_through(self) -> None:
        hybrid = _inert_hybrid()
        results = [{"node_id": None, "score": 1.0}]
        assert hybrid._policy_filter(results, "user1") == results

    def test_granted_and_denied_split(self) -> None:
        hybrid = _inert_hybrid()
        hybrid._rebac = MagicMock()
        hybrid._rebac.check = MagicMock(
            side_effect=lambda subject_id, permission, resource_id: SimpleNamespace(
                granted=(resource_id == "n1")
            )
        )
        results = [{"node_id": "n1"}, {"node_id": "n2"}]
        filtered = hybrid._policy_filter(results, "user1")
        assert [r["node_id"] for r in filtered] == ["n1"]

    def test_no_policy_registered_passes_through(self) -> None:
        hybrid = _inert_hybrid()
        hybrid._rebac = MagicMock()
        hybrid._rebac.check = MagicMock(side_effect=RuntimeError("no policy"))
        results = [{"node_id": "n1"}]
        assert hybrid._policy_filter(results, "user1") == results


# ---------------------------------------------------------------------------
# _graph_expand — normal/error/edge
# ---------------------------------------------------------------------------


class TestGraphExpand:
    def test_neo4j_unavailable_returns_empty(self) -> None:
        hybrid = _inert_hybrid()
        assert hybrid._graph_expand(["a"], depth=1, limit=10) == []

    def test_pack_ids_forwarded_only_when_active(self) -> None:
        neo4j = MagicMock()
        neo4j.available = True
        neo4j.find_neighbors = MagicMock(return_value=[])
        hybrid = HybridQuery(MagicMock(available=False), neo4j)

        hybrid._graph_expand(
            ["anchor1"], depth=1, limit=10, pack_ids=["packA"], include_unpackaged=True
        )
        kwargs = neo4j.find_neighbors.call_args.kwargs
        assert kwargs["pack_ids"] == ["packA"]
        assert kwargs["include_unpackaged"] is True

        neo4j.find_neighbors.reset_mock()
        hybrid._graph_expand(["anchor1"], depth=1, limit=10, pack_ids=None)
        kwargs = neo4j.find_neighbors.call_args.kwargs
        assert "pack_ids" not in kwargs

    def test_exception_for_one_anchor_does_not_abort_others(self) -> None:
        def side_effect(node_id, **kwargs):
            if node_id == "bad":
                raise RuntimeError("boom")
            return [
                {
                    "properties": {"id": "good_n"},
                    "relation_type": "RELATED_TO",
                    "labels": ["X"],
                    "depth": 1,
                }
            ]

        neo4j = MagicMock()
        neo4j.available = True
        neo4j.find_neighbors = MagicMock(side_effect=side_effect)
        hybrid = HybridQuery(MagicMock(available=False), neo4j)
        results = hybrid._graph_expand(["bad", "good"], depth=1, limit=10)
        assert len(results) == 1
        assert results[0].node_id == "good_n"


# ---------------------------------------------------------------------------
# ingest() — normal/error/edge
# ---------------------------------------------------------------------------


class TestIngest:
    def test_chroma_unavailable_short_circuits(self) -> None:
        chroma = MagicMock()
        chroma.available = False
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        result = hybrid.ingest("text", "src1")
        assert result["stores"]["chromadb"] == "unavailable"
        assert "vector_id" not in result

    def test_upsert_exception_reported_gracefully(self) -> None:
        chroma = MagicMock()
        chroma.available = True
        chroma.upsert_texts = MagicMock(side_effect=RuntimeError("boom"))
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        result = hybrid.ingest("text", "src1")
        assert result["stores"]["chromadb"].startswith("error:")


# ---------------------------------------------------------------------------
# query() orchestration — small uncovered branches
# ---------------------------------------------------------------------------


class TestQueryOrchestrationBranches:
    def test_flat_merge_without_rerank_sorts_by_score_desc(self) -> None:
        chroma = MagicMock()
        chroma.available = True
        chroma.query = MagicMock(
            return_value=[
                {"id": "v1", "document": "d1", "metadata": {"node_id": "n1"}, "distance": 0.8},
                {"id": "v2", "document": "d2", "metadata": {"node_id": "n2"}, "distance": 0.1},
            ]
        )
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        results = hybrid.query(
            "q", use_rerank=False, use_bm25=False, use_fts=False
        )
        assert [r.node_id for r in results] == ["n2", "n1"]

    def test_infer_pack_id_added_when_missing_from_metadata(self) -> None:
        chroma = MagicMock()
        chroma.available = True
        chroma.query = MagicMock(
            return_value=[
                {
                    "id": "v1",
                    "document": "d1",
                    "metadata": {"node_id": "n1", "source_path": "/packs/packZ/stage/x"},
                    "distance": 0.5,
                }
            ]
        )
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        results = hybrid.query("q", use_rerank=False, use_bm25=False, use_fts=False)
        assert results[0].metadata["pack_id"] == "packZ"

    def test_subject_id_and_rebac_triggers_policy_filter(self) -> None:
        chroma = MagicMock()
        chroma.available = True
        chroma.query = MagicMock(
            return_value=[
                {"id": "v1", "document": "d1", "metadata": {"node_id": "n1"}, "distance": 0.1},
                {"id": "v2", "document": "d2", "metadata": {"node_id": "n2"}, "distance": 0.2},
            ]
        )
        hybrid = HybridQuery(chroma, MagicMock(available=False))
        hybrid._rebac = MagicMock()
        hybrid._rebac.check = MagicMock(
            side_effect=lambda subject_id, permission, resource_id: SimpleNamespace(
                granted=(resource_id == "n1")
            )
        )
        results = hybrid.query(
            "q", subject_id="u1", use_rerank=False, use_bm25=False, use_fts=False
        )
        assert [r.node_id for r in results] == ["n1"]


# ---------------------------------------------------------------------------
# Pure-function helpers — small uncovered branches
# ---------------------------------------------------------------------------


class TestProfileAndHelpers:
    def test_profile_multihop_cue_forces_depth_3(self) -> None:
        profile = _profile_for_query("connect chain", limit=5, graph_depth=1)
        assert profile.graph_depth == 3

    def test_ordered_unique_breaks_at_limit(self) -> None:
        assert _ordered_unique(["a", "b", "c", "d"], limit=2) == ["a", "b"]

    def test_property_text_falls_back_to_str_props_when_no_known_field(self) -> None:
        text = _property_text({"unlisted_key": "value"}, relation_type="")
        assert "unlisted_key" in text
