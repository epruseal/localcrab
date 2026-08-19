from __future__ import annotations

from unittest.mock import MagicMock

from opencrab.ontology.query import HybridQuery, _build_chroma_where

# ---------------------------------------------------------------------------
# T3 — _build_chroma_where four cases
# ---------------------------------------------------------------------------


def test_t3_build_where_none() -> None:
    assert _build_chroma_where() is None
    assert _build_chroma_where(spaces=None, pack_ids=None) is None


def test_t3_build_where_spaces_only_single() -> None:
    assert _build_chroma_where(spaces=["claim"]) == {"space": "claim"}


def test_t3_build_where_spaces_only_multi() -> None:
    where = _build_chroma_where(spaces=["claim", "policy"])
    assert where == {"space": {"$in": ["claim", "policy"]}}


def test_t3_build_where_pack_ids_only_single() -> None:
    assert _build_chroma_where(pack_ids=["pack-a"]) == {"pack_id": "pack-a"}


def test_t3_build_where_pack_ids_only_multi() -> None:
    where = _build_chroma_where(pack_ids=["pack-a", "pack-b"])
    assert where == {"pack_id": {"$in": ["pack-a", "pack-b"]}}


def test_t3_build_where_combined() -> None:
    where = _build_chroma_where(spaces=["claim"], pack_ids=["pack-a"])
    assert where == {
        "$and": [{"space": "claim"}, {"pack_id": "pack-a"}],
    }


# ---------------------------------------------------------------------------
# T12 — Chroma where fallback on exception
# ---------------------------------------------------------------------------


def _make_hybrid_with_chroma(query_mock: MagicMock) -> HybridQuery:
    chroma = MagicMock()
    chroma.available = True
    chroma.query = query_mock
    neo4j = MagicMock()
    neo4j.available = False
    return HybridQuery(chroma, neo4j)


def test_t12_vector_search_server_side_when_no_unpackaged() -> None:
    hit = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    query_mock = MagicMock(return_value=[hit])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "alpha", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=False,
    )
    # server-side where used
    kwargs = query_mock.call_args.kwargs
    assert kwargs["where"] == {"pack_id": "pack-a"}
    assert len(results) == 1
    assert results[0].node_id == "n1"


def test_t12_vector_search_fallback_on_exception() -> None:
    hit_a = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    hit_b = {
        "id": "v2",
        "document": "beta",
        "metadata": {"pack_id": "pack-b", "node_id": "n2"},
        "distance": 0.2,
    }
    call_state = {"first": True}

    def fake_query(**kwargs):
        if call_state["first"]:
            call_state["first"] = False
            raise RuntimeError("simulated where rejection")
        return [hit_a, hit_b]

    query_mock = MagicMock(side_effect=fake_query)
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "alpha", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=False,
    )
    # First call asked for server-side filter; the second was a wider scan.
    assert query_mock.call_count == 2
    # Only pack-a hit survives the Python post-filter.
    assert [r.node_id for r in results] == ["n1"]


def test_t12_vector_search_post_filter_when_unpackaged() -> None:
    """issue #147 §3.3/§3.6: the Python post-filter now uses ``in_pack_scope``
    instead of ``infer_pack_id``, and ``in_pack_scope`` never admits an item
    with no detectable pack_id (#143 invariant 5) -- ``include_unpackaged``
    can no longer widen a scope to include unpackaged rows, since doing so
    would let a caller read data no pack grants them access to. Only the
    hit whose pack_id is actually IN the caller's scope survives; the
    orphan (no pack_id) and the foreign-pack hit are both excluded."""
    hit_a = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    hit_orphan = {
        "id": "v2",
        "document": "orphan",
        "metadata": {"node_id": "n2"},
        "distance": 0.3,
    }
    hit_foreign = {
        "id": "v3",
        "document": "foreign",
        "metadata": {"pack_id": "pack-b", "node_id": "n3"},
        "distance": 0.4,
    }
    query_mock = MagicMock(return_value=[hit_a, hit_orphan, hit_foreign])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "x", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=True,
    )
    # Server-side where dropped pack_ids; Python post-filter keeps only
    # pack-a and rejects both the unpackaged orphan and foreign pack-b.
    kwargs = query_mock.call_args.kwargs
    assert kwargs["where"] is None
    ids = sorted(r.node_id for r in results)
    assert ids == ["n1"]


def test_t12_post_filter_overfetch_n_results() -> None:
    """include_unpackaged=True causes over-fetched n_results: max(min(limit,20)*4, 20)."""
    hit = {"id": "v1", "document": "a", "metadata": {"pack_id": "pack-a", "node_id": "n1"}, "distance": 0.1}
    query_mock = MagicMock(return_value=[hit])
    hybrid = _make_hybrid_with_chroma(query_mock)
    hybrid._vector_search("x", spaces=None, limit=5, pack_ids=["pack-a"], include_unpackaged=True)
    kwargs = query_mock.call_args.kwargs
    # max(min(5, 20) * 4, 20) = max(20, 20) = 20
    assert kwargs["n_results"] == 20


def test_t12_post_filter_matching_behind_foreign_hits_survives() -> None:
    """With include_unpackaged over-fetch, a matching hit ranked after many foreign hits is found."""
    foreign_hits = [
        {
            "id": f"v{i}",
            "document": f"foreign{i}",
            "metadata": {"pack_id": "pack-b", "node_id": f"n{i}"},
            "distance": 0.1 + i * 0.01,
        }
        for i in range(10)
    ]
    matching = {"id": "v99", "document": "match", "metadata": {"pack_id": "pack-a", "node_id": "n99"}, "distance": 0.5}
    query_mock = MagicMock(return_value=foreign_hits + [matching])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search("x", spaces=None, limit=5, pack_ids=["pack-a"], include_unpackaged=True)
    assert any(r.node_id == "n99" for r in results)


def test_t12_no_overfetch_server_side_path() -> None:
    """Without include_unpackaged, server-side filter is used and n_results is not over-fetched."""
    hit = {"id": "v1", "document": "a", "metadata": {"pack_id": "pack-a", "node_id": "n1"}, "distance": 0.1}
    query_mock = MagicMock(return_value=[hit])
    hybrid = _make_hybrid_with_chroma(query_mock)
    hybrid._vector_search("x", spaces=None, limit=5, pack_ids=["pack-a"], include_unpackaged=False)
    kwargs = query_mock.call_args.kwargs
    # No over-fetch: min(5, 20) = 5
    assert kwargs["n_results"] == 5


# ---------------------------------------------------------------------------
# #51 — spaces filter: request builds the right where clause, and the
# transitional "filter may be incomplete" signal is surfaced (not silently 0).
# ---------------------------------------------------------------------------


def test_51_vector_search_forwards_spaces_where_clause() -> None:
    """_vector_search asks the backend for the 'space' where clause it built.

    ``pack_ids`` is now a required kwarg (issue #147); with a single pack
    scope and no ``include_unpackaged``, the server-side where clause
    combines the spaces AND pack predicates via ``_build_chroma_where``'s
    ``$and`` form -- the spaces predicate is still exactly what's asserted,
    just alongside the now-mandatory pack predicate rather than alone."""
    hit = {"id": "v1", "document": "a", "metadata": {"space": "claim", "node_id": "n1"}, "distance": 0.1}
    query_mock = MagicMock(return_value=[hit])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search("x", spaces=["claim"], limit=5, pack_ids=["pack-a"])
    kwargs = query_mock.call_args.kwargs
    assert kwargs["where"] == {"$and": [{"space": "claim"}, {"pack_id": "pack-a"}]}
    assert [r.node_id for r in results] == ["n1"]


def test_51_query_sets_transitional_warning_when_spaces_filter_used() -> None:
    """query() surfaces a caller-visible warning when a spaces filter is active.

    Root cause: vectors ingested before builder.py wrote 'space' into metadata
    have no such key, and SqliteVecStore/Chroma both treat a missing key as a
    match failure (not "ignored"), so a spaces filter silently zeroes the
    vector leg for pre-fix data until a backfill runs. Callers must be able to
    tell "no results" apart from "filter could not be applied".

    The warning travels in QueryOutcome.warnings (query()'s return value), not
    instance state — HybridQuery is a process-lifetime singleton served from a
    threadpool, so instance state would race across concurrent requests.
    """
    chroma = MagicMock()
    chroma.available = True
    chroma.query = MagicMock(return_value=[])
    hybrid = HybridQuery(chroma, MagicMock(available=False))
    outcome = hybrid.query(
        "q", pack_ids=["pack-a"], spaces=["claim"],
        use_rerank=False, use_bm25=False, use_fts=False,
    )
    assert outcome.warnings
    assert "space" in outcome.warnings[0]


def test_51_query_no_warning_when_spaces_not_used() -> None:
    """No spaces filter → no transitional warning (existing spaces=None behaviour)."""
    chroma = MagicMock()
    chroma.available = True
    chroma.query = MagicMock(return_value=[])
    hybrid = HybridQuery(chroma, MagicMock(available=False))
    outcome = hybrid.query(
        "q", pack_ids=["pack-a"], spaces=None,
        use_rerank=False, use_bm25=False, use_fts=False,
    )
    assert outcome.warnings == []


def test_51_query_outcome_still_iterates_len_and_indexes_like_a_list() -> None:
    """QueryOutcome must stay a drop-in replacement for the old list[QueryResult]
    return value so unrelated call sites (cli.py, existing tests) don't break."""
    hit = {"id": "v1", "document": "a", "metadata": {"node_id": "n1"}, "distance": 0.1}
    chroma = MagicMock()
    chroma.available = True
    chroma.query = MagicMock(return_value=[hit])
    hybrid = HybridQuery(chroma, MagicMock(available=False))
    outcome = hybrid.query(
        "q", pack_ids=["pack-a"], use_rerank=False, use_bm25=False, use_fts=False,
    )
    assert len(outcome) == 1
    assert [r.node_id for r in outcome] == ["n1"]
    assert outcome[0].node_id == "n1"
    assert bool(outcome) is True


def test_51_builder_writes_space_into_vector_metadata() -> None:
    """Root fix: OntologyBuilder.add_node must write 'space' into vector metadata
    so the where-clause SqliteVecStore/_build_chroma_where builds actually has a
    key to match against, for nodes ingested from here on.

    #148: add_node now calls current_principal() and authorize(sql,
    principal, pack_id) internally, both BEFORE the vector-store branch this
    test inspects -- needs a bound principal and a real, queryable SQL store
    with a pack it owns (authorize() fails closed -- RuntimeError -- when
    sql.available is falsy). #148 point 6 also means graph must report
    available=True: a graph-unavailable node write now refuses the whole
    fan-out (including the vector write), where this test used to rely on
    graph=unavailable being harmless to the vector-only path it cares about.
    """
    from opencrab.auth import Principal, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack
    from opencrab.stores.sql_store import SQLStore

    vec = MagicMock()
    vec.available = True
    vec.upsert_texts = MagicMock()
    vec.get_by_id.return_value = None
    graph = MagicMock(available=True)
    # #148 identity guard: a MagicMock answers every probe with another
    # MagicMock, which is an unrecognised shape and so fail-closed.
    graph.get_node.return_value = None
    graph.get_nodes_by_id.return_value = []
    docs = MagicMock(available=False)
    sql = SQLStore("sqlite:///:memory:")
    pack_id = create_pack(sql, "test-user", "pack-a")
    builder = OntologyBuilder(graph, docs, sql, vec=vec)

    with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
        builder.add_node(
            "resource", "Document", "n1", {"title": "hello world"}, pack_id=pack_id
        )

    kwargs = vec.upsert_texts.call_args.kwargs
    assert kwargs["metadatas"][0]["space"] == "resource"
