"""
Direct handler coverage for opencrab/mcp/tools/__init__.py's LIVE tool bodies
not exercised by tests/test_mcp.py, tests/test_mcp_dispatch_extended.py, or
tests/test_mcp_pack_aware.py:
  - harness_promotion_apply (dry_run / real apply / crabharness missing /
    malformed package)
  - pack_create (new pack / already-exists / anchor-write failure / text path)
  - _ingest_into_pack internals (node/edge success+error branches, evidence
    node failure)
  - schema_pack_list/install/uninstall, content_pack_list
  - _get_context's real body (no mocking of _get_context itself)

schema_pack_install/uninstall write to a hardcoded real directory
(opencrab/schemas/types/) with no dependency injection — tests here patch
opencrab.schemas.pack_registry's functions rather than calling them for
real, so the MCP wrapper's delegation is verified without mutating the repo.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from opencrab.mcp.tools import (
    _context,
    _ingest_into_pack,
    content_pack_list,
    dispatch_tool,
    harness_promotion_apply,
    pack_create,
    pack_ingest,
)
from opencrab.ontology.builder import store_write_failures, store_write_succeeded

# #145: these handlers now call current_principal() internally; bind a fixed
# test principal for every test in this module (see conftest.py's
# bind_test_principal).
pytestmark = pytest.mark.usefixtures("bind_test_principal")


@pytest.fixture(autouse=True)
def _clear_context():
    _context.clear()
    yield
    _context.clear()


def _base_ctx(**overrides):
    ctx = {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": MagicMock(),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    # #146 P1(a): a MagicMock lookup returns a truthy MagicMock, not None,
    # so the identity-ownership probes in _ingest_into_pack/pack_create
    # would otherwise treat every item as unverifiable (fail-closed) and
    # reject it. These defaults spell out this suite's original implicit
    # assumption -- "no conflicting store" -- explicitly; tests that DO
    # want to exercise a conflict override these return_values themselves.
    ctx["neo4j"].get_node.return_value = None
    ctx["neo4j"].get_node_by_id.return_value = None
    ctx["neo4j"].get_edge.return_value = None
    # #177 R4-A: an unresolvable endpoint type is now fail-closed (rejected),
    # not skipped, so the default here must be a real type -- "endpoints
    # exist on this backend" -- matching get_edge.return_value = None above
    # ("no conflict found"). Tests that want the reject-on-None path set
    # this back to None themselves.
    ctx["neo4j"].lookup_node_type.return_value = "Entity"
    ctx["mongo"].get_node_doc.return_value = None
    ctx["mongo"].get_source.return_value = None
    ctx["chroma"].get_by_id.return_value = None
    ctx.update(overrides)
    return ctx


def _writable_ctx(pack_id, owner="test-user", **overrides):
    """A ``_base_ctx()`` with a real in-memory SQLStore carrying one packs
    registry row for ``pack_id`` owned by ``owner`` -- #146 D: pack_ingest's
    existence/ownership check is ``assert_writable`` against the real
    ``packs`` table now, not a mocked ``content_pack_list()``."""
    from opencrab.pack.ownership import create_pack as _register_pack
    from opencrab.stores.sql_store import SQLStore

    sql = SQLStore("sqlite:///:memory:")
    _register_pack(sql, owner, pack_id)
    overrides.setdefault("sql", sql)
    return _base_ctx(**overrides)


# ---------------------------------------------------------------------------
# content_pack_list
# ---------------------------------------------------------------------------


def _reg_row(pack_id, title="", description=""):
    """A ``list_packs_for`` row shape (opencrab.pack.ownership._row_to_dict)
    -- see tests/test_content_pack_list_query.py for the full query/ranking
    contract; this module's tests only cover the plumbing (title-stripping,
    fallback, the #146 registry-is-the-source join)."""
    return {
        "pack_id": pack_id,
        "owner_id": "test-user",
        "visibility": "private",
        "title": title,
        "description": description,
        "forked_from": None,
        "created_at": None,
        "updated_at": None,
    }


class TestContentPackList:
    def test_normal_strips_pack_suffix_from_title(self):
        graph = MagicMock()
        graph.available = True
        graph.list_packs.return_value = [
            {"pack_id": "biomed", "node_count": 5, "sample_title": "Biomed ontology pack"},
        ]
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.pack.ownership.list_packs_for", return_value=[_reg_row("biomed")]),
        ):
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list(min_nodes=1)
        assert result == {
            "total": 1,
            "node_count_known": True,
            "min_nodes_applied": True,
            "packs": [{"pack_id": "biomed", "node_count": 5, "title": "Biomed"}],
        }

    def test_normal_falls_back_to_pack_id_when_no_title(self):
        graph = MagicMock()
        graph.available = True
        graph.list_packs.return_value = [{"pack_id": "p1", "node_count": 2, "sample_title": ""}]
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.pack.ownership.list_packs_for", return_value=[_reg_row("p1")]),
        ):
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list()
        assert result["packs"][0]["title"] == "p1"

    def test_normal_scopes_to_registry_rows(self):
        """#146 C: candidates come from ``list_packs_for`` (the registry),
        not graph.list_packs() -- a pack_id the registry didn't return never
        appears, even though it's loaded in the graph with real nodes."""
        graph = MagicMock()
        graph.available = True
        graph.list_packs.return_value = [
            {"pack_id": "mine", "node_count": 1, "sample_title": ""},
            {"pack_id": "not-mine", "node_count": 1, "sample_title": ""},
        ]
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.pack.ownership.list_packs_for", return_value=[_reg_row("mine")]),
        ):
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list()
        assert [p["pack_id"] for p in result["packs"]] == ["mine"]

    def test_edge_graph_unavailable_returns_unknown_counts_not_error(self):
        """#146 C: graph unavailable no longer short-circuits to a
        top-level "error" -- registry-readable packs are still listed, with
        node_count_known/min_nodes_applied false and every node_count
        null (see test_content_pack_list_query.py for the full contract)."""
        graph = MagicMock()
        graph.available = False
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.pack.ownership.list_packs_for", return_value=[_reg_row("mine")]),
        ):
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list()
        assert "error" not in result
        assert result["node_count_known"] is False
        assert result["min_nodes_applied"] is False
        assert result["packs"] == [{"pack_id": "mine", "node_count": None, "title": "mine"}]


# ---------------------------------------------------------------------------
# schema_pack_list / install / uninstall — delegation contract only
# ---------------------------------------------------------------------------


class TestSchemaPackDelegation:
    def test_schema_pack_list_wraps_registry_output(self):
        with patch("opencrab.schemas.pack_registry.list_packs") as mock_list:
            mock_list.return_value = [{"name": "biomedical", "installed": False}]
            result = dispatch_tool("schema_pack_list", {})
        assert result == {"total": 1, "packs": [{"name": "biomedical", "installed": False}]}

    def test_schema_pack_install_delegates_with_name(self):
        with patch("opencrab.schemas.pack_registry.install_pack") as mock_install:
            mock_install.return_value = {"created": ["Gene.yaml"], "skipped": []}
            result = dispatch_tool("schema_pack_install", {"name": "biomedical"})
        assert result == {"created": ["Gene.yaml"], "skipped": []}
        mock_install.assert_called_once_with("biomedical")

    def test_schema_pack_uninstall_delegates_with_name_and_force(self):
        with patch("opencrab.schemas.pack_registry.uninstall_pack") as mock_uninstall:
            mock_uninstall.return_value = {"removed": ["Gene.yaml"], "kept": []}
            result = dispatch_tool(
                "schema_pack_uninstall", {"name": "biomedical", "force": True}
            )
        assert result == {"removed": ["Gene.yaml"], "kept": []}
        mock_uninstall.assert_called_once_with("biomedical", True)

    def test_schema_pack_uninstall_defaults_force_false(self):
        with patch("opencrab.schemas.pack_registry.uninstall_pack") as mock_uninstall:
            mock_uninstall.return_value = {"removed": [], "kept": ["Gene.yaml"]}
            dispatch_tool("schema_pack_uninstall", {"name": "biomedical"})
        mock_uninstall.assert_called_once_with("biomedical", False)


# ---------------------------------------------------------------------------
# store_write_failures — the shared "did this write actually happen" rule
# ---------------------------------------------------------------------------


class TestStoreWriteFailures:
    def test_normal_all_ok_is_no_failures(self):
        assert store_write_failures({"graph": "ok", "docs": "ok", "sql": "ok"}) == []

    def test_normal_optional_store_unavailable_is_not_a_failure(self):
        """docs/sql/vector being unavailable is a normal deployment shape
        (those backends are optional) — not a write failure."""
        assert store_write_failures({"graph": "ok", "docs": "unavailable", "sql": "unavailable"}) == []

    def test_error_graph_unavailable_is_a_failure(self):
        """graph is the system of record: unavailable there means the write
        landed nowhere that counts, even if optional stores succeeded."""
        assert store_write_failures({"graph": "unavailable", "docs": "ok"}) == ["graph: unavailable"]

    def test_error_store_error_status_is_a_failure(self):
        assert store_write_failures({"graph": "error: disk I/O"}) == ["graph: error: disk I/O"]

    def test_error_no_match_status_is_a_failure(self):
        assert store_write_failures({"graph": "no match (missing node: a, b)"}) == [
            "graph: no match (missing node: a, b)"
        ]


class TestStoreWriteSucceeded:
    """#66 codex re-review (4th round), finding [2]: pins the exact success
    vocabulary this function must recognize — verified against every real
    status string OntologyBuilder.add_node/add_edge and HybridQuery.ingest()
    actually assign (see store_write_succeeded's docstring in builder.py for
    the full inventory). A bare `== "ok"` comparison rejected the decorated
    "ok (id=...)" shape and silently stopped billing real successes; these
    tests exist so that regression can't ship unnoticed again."""

    def test_normal_bare_ok_is_success(self):
        assert store_write_succeeded({"graph": "ok"}, "graph") is True

    def test_normal_decorated_ok_with_id_is_success(self):
        """Real production shape for docs (builder.py add_node) and
        chromadb (query.py HybridQuery.ingest())."""
        assert store_write_succeeded({"docs": "ok (id=abc123)"}, "docs") is True
        assert store_write_succeeded({"chromadb": "ok (id=vec-1)"}) is True

    def test_error_audited_is_not_recognized_as_success(self):
        """Deliberately excluded (see the docstring): "audited" (edge audit
        log) is a different word, not "ok"-prefixed."""
        assert store_write_succeeded({"docs": "audited"}, "docs") is False

    def test_error_skipped_is_not_success(self):
        assert store_write_succeeded({"vector": "skipped (no text)"}, "vector") is False

    def test_error_unavailable_is_not_success(self):
        assert store_write_succeeded({"graph": "unavailable"}, "graph") is False

    def test_error_missing_key_is_not_success(self):
        """The exact fail-closed case #66's 2nd codex round caught: a stores
        map that simply doesn't have the requested key must not be treated
        as a positive success."""
        assert store_write_succeeded({"sql": "ok"}, "graph") is False

    def test_error_non_dict_stores_is_not_success(self):
        assert store_write_succeeded(None, "graph") is False  # type: ignore[arg-type]

    def test_normal_key_none_checks_any_store(self):
        """key=None (pack.py's legacy text-ingest gate): success if ANY
        store in the map succeeded, regardless of which one."""
        assert store_write_succeeded({"chromadb": "unavailable", "mongodb": "ok"}) is True
        assert store_write_succeeded({"chromadb": "ok (id=x)", "mongodb": "unavailable"}) is True

    def test_error_key_none_all_unavailable_is_not_success(self):
        """The exact scenario #66's 3rd codex round caught: both optional
        stores unavailable, key=None must not fall through to success."""
        assert store_write_succeeded({"chromadb": "unavailable", "mongodb": "unavailable"}) is False

    # -----------------------------------------------------------------
    # Adversarial values (#66 codex re-review, 5th round, finding [1]):
    # a bare `startswith("ok")` is WIDER than the two real success shapes
    # ("ok" and "ok (...)") and would silently bill any future status that
    # merely starts with those two letters. These pin the narrower contract.
    # -----------------------------------------------------------------

    def test_error_okay_is_not_success(self):
        """"okay" starts with "ok" but is neither of the two real shapes."""
        assert store_write_succeeded({"graph": "okay"}, "graph") is False

    def test_error_ok_dash_error_is_not_success(self):
        assert store_write_succeeded({"graph": "ok-error: disk down"}, "graph") is False

    def test_error_ok_but_failed_is_not_success(self):
        assert store_write_succeeded({"graph": "ok_but_failed"}, "graph") is False

    def test_error_ok_no_paren_with_trailing_text_is_not_success(self):
        """"ok " (trailing space, no paren) is not the decorated shape either
        — only "ok (...)" is recognized, not any "ok <anything>"."""
        assert store_write_succeeded({"graph": "ok degraded"}, "graph") is False

    def test_normal_real_shapes_still_recognized(self):
        """The narrower rule must not have thrown out the two real shapes
        while excluding the adversarial ones above."""
        assert store_write_succeeded({"graph": "ok"}, "graph") is True
        assert store_write_succeeded({"docs": "ok (id=abc123)"}, "docs") is True

    # -----------------------------------------------------------------
    # Non-string defense: a malformed stores map must never raise — a
    # billing decision blowing up would fail the write it's judging.
    # -----------------------------------------------------------------

    def test_error_none_status_value_does_not_raise(self):
        assert store_write_succeeded({"graph": None}, "graph") is False

    def test_error_dict_status_value_does_not_raise(self):
        assert store_write_succeeded({"graph": {"nested": "ok"}}, "graph") is False

    def test_error_int_status_value_does_not_raise(self):
        assert store_write_succeeded({"graph": 200}, "graph") is False

    def test_error_non_dict_stores_key_none_does_not_raise(self):
        assert store_write_succeeded("not a dict", None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _ingest_into_pack — node/edge loops + evidence-node materialisation
# ---------------------------------------------------------------------------


class TestIngestIntoPack:
    def test_normal_nodes_and_edges_all_succeed(self):
        builder = MagicMock()
        hybrid = MagicMock()
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid)
            result = _ingest_into_pack(
                "pack-a",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
                edges=[{
                    "from_space": "concept", "from_id": "e1", "relation": "related_to",
                    "to_space": "concept", "to_id": "e2",
                }],
            )
        assert result["added_nodes"] == 1
        assert result["added_edges"] == 1
        assert result["node_errors"] == []
        assert result["edge_errors"] == []
        hybrid.invalidate_bm25_cache.assert_called_once()

    def test_normal_pack_id_reaches_every_write_in_the_batch(self):
        """Issue #119: _ingest_into_pack's node loop, edge loop, and
        evidence-node write each call builder.add_node/add_edge separately.
        Pin that ALL THREE receive the same pack_id (a partial fix — e.g.
        only the node loop fixed — would make the writes land in
        inconsistent packs within one call).

        #148: builder.add_node/add_edge no longer take a `subject_id` kwarg
        at all (the real OntologyBuilder derives the writing principal
        internally via current_principal()) -- _ingest_into_pack's own
        `subject_id` parameter now feeds only the `on_ingest` billing event
        (see test_normal_bills_one_ingest_event_using_source_id and
        friends), not these three write sites. `pack_id` is the channel
        that must reach all three now."""
        builder = MagicMock()
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            _ingest_into_pack(
                "pack-a",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
                edges=[{
                    "from_space": "concept", "from_id": "e1", "relation": "related_to",
                    "to_space": "concept", "to_id": "e2",
                }],
                text="hello world", source_id="src-1",
                subject_id="actor-1",
            )
        # First add_node call is the "nodes=" loop entry, second is the
        # evidence/TextUnit node from the text= branch.
        assert builder.add_node.call_args_list[0].kwargs["pack_id"] == "pack-a"
        assert builder.add_node.call_args_list[1].kwargs["pack_id"] == "pack-a"
        assert builder.add_edge.call_args.kwargs["pack_id"] == "pack-a"

    def test_normal_omitted_subject_id_only_affects_billing(self):
        """#148: subject_id no longer reaches builder.add_node/add_edge at
        all (see test_normal_pack_id_reaches_every_write_in_the_batch above)
        -- it is exclusively an on_ingest billing argument now, so "omitted"
        vs "given" has nothing left to distinguish on the builder calls.
        Pin the part of this contract that still exists: an omitted
        subject_id reaches on_ingest as None, unchanged from before."""
        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        builder.add_edge.return_value = {"stores": {"graph": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, billing=billing)
            _ingest_into_pack(
                "pack-a",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
                edges=[{
                    "from_space": "concept", "from_id": "e1", "relation": "related_to",
                    "to_space": "concept", "to_id": "e2",
                }],
                text="hello world", source_id="src-1",
            )
        assert "subject_id" not in builder.add_node.call_args_list[0].kwargs
        assert "subject_id" not in builder.add_node.call_args_list[1].kwargs
        assert "subject_id" not in builder.add_edge.call_args.kwargs
        billing.on_ingest.assert_called_once_with("default", None, "src-1")

    def test_error_node_write_failure_recorded_without_aborting(self):
        builder = MagicMock()
        builder.add_node.side_effect = [None, RuntimeError("bad node")]
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a",
                nodes=[
                    {"space": "concept", "node_type": "Entity", "node_id": "e1"},
                    {"space": "concept", "node_type": "Entity", "node_id": "e2"},
                ],
            )
        assert result["added_nodes"] == 1
        assert result["node_errors"] == ["e2: bad node"]

    def test_error_edge_write_failure_recorded_with_arrow_format(self):
        builder = MagicMock()
        builder.add_edge.side_effect = RuntimeError("bad edge")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a",
                edges=[{
                    "from_space": "concept", "from_id": "a1", "relation": "related_to",
                    "to_space": "concept", "to_id": "a2",
                }],
            )
        assert result["added_edges"] == 0
        assert result["edge_errors"] == ["a1→a2: bad edge"]

    def test_error_evidence_node_failure_still_marks_text_ingested(self):
        """A failed evidence/TextUnit write is recorded in node_errors and
        stores['evidence_node'], but text_ingested is still True — the
        function only tracks whether a text+source_id pair was *attempted*,
        not whether it succeeded."""
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("vector store down")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a", text="hello world", source_id="src-1",
            )
        assert result["text_ingested"] is True
        assert result["evidence_node"] is None
        assert result["node_errors"] == ["src-1 (evidence/TextUnit): vector store down"]
        assert result["stores"]["evidence_node"] == "error: vector store down"

    def test_error_legacy_path_hybrid_ingest_failure_recorded(self):
        """text_as_node=False legacy path: hybrid.ingest() raising is caught
        and recorded as stores['chromadb'], not propagated."""
        builder = MagicMock()
        hybrid = MagicMock()
        hybrid.ingest.side_effect = RuntimeError("embed failed")
        mongo = MagicMock()
        mongo.available = True
        mongo.get_source.return_value = None  # #146 P1(a): no conflicting slot
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo)
            result = _ingest_into_pack(
                "pack-a", text="legacy text", source_id="src-2", text_as_node=False,
            )
        assert result["stores"]["chromadb"] == "error: embed failed"
        assert result["stores"]["mongodb"] == "ok"
        assert result["text_ingested"] is True

    def test_error_legacy_path_mongo_upsert_failure_recorded(self):
        """text_as_node=False legacy path: mongo.upsert_source() raising is
        caught and recorded as stores['mongodb'], not propagated."""
        builder = MagicMock()
        hybrid = MagicMock()
        hybrid.ingest.return_value = {"stores": {"chromadb": "ok"}}
        mongo = MagicMock()
        mongo.available = True
        mongo.get_source.return_value = None  # #146 P1(a): no conflicting slot
        mongo.upsert_source.side_effect = RuntimeError("mongo down")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo)
            result = _ingest_into_pack(
                "pack-a", text="legacy text", source_id="src-3", text_as_node=False,
            )
        assert result["stores"]["mongodb"] == "error: mongo down"

    def test_error_legacy_path_both_stores_unavailable_does_not_bill(self):
        """#66 codex re-review (3rd round): the legacy text_as_node=False
        path bypasses added_nodes/added_edges entirely (it never touches
        `graph`) — its only success signal is the `stores` dict. Before this
        fix, "unavailable" wasn't treated as a failure there (it only fails
        the "graph" store, and this path never sets that key), so a legacy
        ingest where BOTH the vector store and the doc store are unavailable
        — nothing written anywhere — still fired on_ingest. Pin: it must not."""
        builder = MagicMock()
        billing = MagicMock()
        hybrid = MagicMock()
        hybrid.ingest.return_value = {"stores": {"chromadb": "unavailable"}}
        mongo = MagicMock()
        mongo.available = False  # -> stores["mongodb"] = "unavailable"
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo, billing=billing)
            result = _ingest_into_pack(
                "pack-a", text="legacy text", source_id="src-4", text_as_node=False,
            )
        assert result["stores"] == {"chromadb": "unavailable", "mongodb": "unavailable"}
        assert result["text_ingested"] is True  # the attempt was made
        billing.on_ingest.assert_not_called()  # but nothing actually landed

    def test_normal_legacy_path_vector_only_success_still_bills(self):
        """Positive-confirmation counterpart: chromadb comes back its REAL
        production shape — "ok (id=...)" (opencrab/ontology/query.py's
        HybridQuery.ingest() decorates the status with the vector id, it
        never returns a bare "ok") — even though mongodb, an optional audit
        record, is unavailable. The text really did land in the vector
        store, so this must still bill.

        #66 codex re-review (4th round), finding [2]/[5]: an earlier version
        of this test used a bare "ok" mock, which passed against a bare
        `== "ok"` billing check that REJECTED the real decorated string and
        silently stopped billing every legacy vector-only success in
        production — the fixture was shaped to match the implementation
        instead of the real contract, so it could not catch that regression.
        This mock is now the actual shape ``ctx["hybrid"].ingest()`` returns."""
        builder = MagicMock()
        billing = MagicMock()
        hybrid = MagicMock()
        hybrid.ingest.return_value = {"stores": {"chromadb": "ok (id=vec-src-5)"}}
        mongo = MagicMock()
        mongo.available = False
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo, billing=billing)
            _ingest_into_pack("pack-a", text="legacy text", source_id="src-5", text_as_node=False)
        billing.on_ingest.assert_called_once_with("default", None, "src-5")

    def test_normal_real_hybrid_ingest_output_shape_bills(self):
        """Not a mock at all — exercises the REAL
        opencrab.ontology.query.HybridQuery.ingest() (only its Chroma client
        stubbed) so this test cannot drift from production's actual status
        string shape the way a hand-built dict can. Guards against the exact
        class of bug the finding above describes recurring."""
        from opencrab.ontology.query import HybridQuery

        builder = MagicMock()
        billing = MagicMock()
        chroma = MagicMock()
        chroma.available = True
        chroma.upsert_texts.return_value = ["vec-real-1"]
        hybrid = HybridQuery(chroma=chroma, neo4j=MagicMock(available=False))
        mongo = MagicMock()
        mongo.available = False
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo, billing=billing)
            result = _ingest_into_pack(
                "pack-a", text="legacy text", source_id="src-6", text_as_node=False,
            )
        assert result["stores"]["chromadb"] == "ok (id=vec-real-1)"
        billing.on_ingest.assert_called_once_with("default", None, "src-6")

    def test_edge_no_content_is_a_noop(self):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx()
            result = _ingest_into_pack("pack-a")
        assert result == {
            "status": "ok",
            "pack_id": "pack-a", "added_nodes": 0, "added_edges": 0,
            "node_errors": [], "edge_errors": [], "stores": {},
            "text_ingested": False, "evidence_node": None,
        }

    def test_error_node_store_partial_failure_not_counted(self):
        """The real failure shape from OntologyBuilder.add_node: it returns
        normally (no exception) with an "error: ..." entry inside the
        `stores` map when one backend write fails. A bare try/except around
        the call cannot catch this — the return value itself must be
        inspected, or the failed write gets counted as added."""
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "error: disk I/O", "docs": "ok", "sql": "ok"}
        }
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        assert result["added_nodes"] == 0
        assert result["node_errors"] == ["e1: graph: error: disk I/O"]
        assert result["status"] == "partial"

    def test_error_edge_missing_endpoint_not_counted(self):
        """The real failure shape from OntologyBuilder.add_edge for a missing
        endpoint: it returns normally with
        stores["graph"] = "no match (missing node: ...)" — no exception, so
        this must not be counted as added_edges."""
        builder = MagicMock()
        builder.add_edge.return_value = {
            "stores": {
                "graph": "no match (missing node: concept/no-such-a, concept/no-such-b)",
                "sql": "skipped (missing node)",
                "docs": "unavailable",
            }
        }
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a",
                edges=[{
                    "from_space": "concept", "from_id": "no-such-a", "relation": "related_to",
                    "to_space": "concept", "to_id": "no-such-b",
                }],
            )
        assert result["added_edges"] == 0
        assert result["edge_errors"] == [
            "no-such-a→no-such-b: graph: no match "
            "(missing node: concept/no-such-a, concept/no-such-b)"
        ]
        assert result["status"] == "partial"

    def test_normal_status_ok_when_all_stores_succeed(self):
        """status must reflect real per-store outcomes, not just be a fixed
        "ok" — this asserts the success side of that contract."""
        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok", "docs": "ok", "sql": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
            result = _ingest_into_pack(
                "pack-a",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        assert result["added_nodes"] == 1
        assert result["node_errors"] == []
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# pack_create
# ---------------------------------------------------------------------------


class TestPackCreate:
    def test_normal_creates_anchor_with_no_content(self):
        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="My Pack", pack_id="my-pack")
        assert result["status"] == "ok"
        assert result["pack_id"] == "my-pack"
        assert result["anchor_node"] == "dataset:my-pack"
        builder.add_node.assert_called_once()
        assert builder.add_node.call_args.kwargs["space"] == "resource"
        assert builder.add_node.call_args.kwargs["node_type"] == "Dataset"

    def test_normal_forwards_principal_to_anchor_node_audit(self):
        """Issue #119, inverted for #145: pack_create's anchor node write (a
        builder.add_node call separate from _ingest_into_pack's own
        node/edge/evidence loops) had the same gap — subject_id reached the
        ingest billing event but not this write's own audit event. Pin that
        the SAME principal now governs it -- sourced from the caller's
        server-derived principal (pack_create takes no subject_id argument
        at all anymore, #145).

        #148: builder.add_node no longer takes a `subject_id` kwarg at all
        (the real OntologyBuilder derives the principal internally via
        current_principal()) -- with a mocked builder there is nothing left
        to inspect on that call directly. The principal instead now governs
        WHO OWNS THE REGISTERED PACK the anchor node is stamped into
        (opencrab.pack.ownership.create_pack(owner_id=subject_id, ...)), so
        this uses a real SQLStore (not the class default MagicMock) and
        checks the registry row's owner_id."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        sql = SQLStore("sqlite:///:memory:")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, sql=sql)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="actor-1", is_local=True, disabled=False)):
                result = pack_create(title="My Pack", pack_id="my-pack")
        assert builder.add_node.call_args.kwargs["pack_id"] == result["pack_id"]
        pack_row = get_pack(sql, result["pack_id"])
        assert pack_row is not None
        assert pack_row["owner_id"] == "actor-1"

    def test_normal_uses_bound_principal_by_default(self):
        """#145: there is no "omitted subject_id" state anymore -- pack_create
        always uses whatever current_principal() resolves to (bound here by
        the bind_test_principal fixture).

        #148: see test_normal_forwards_principal_to_anchor_node_audit above
        for why this now checks the registered pack's owner_id via a real
        SQLStore rather than a `subject_id` kwarg on the mocked builder."""
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        sql = SQLStore("sqlite:///:memory:")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, sql=sql)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="My Pack", pack_id="my-pack")
        assert builder.add_node.call_args.kwargs["pack_id"] == result["pack_id"]
        pack_row = get_pack(sql, result["pack_id"])
        assert pack_row is not None
        assert pack_row["owner_id"] == "test-user"

    def test_taken_slug_is_quietly_suffixed_not_an_error(self):
        """#146: the registry (packs table), not a content_pack_list() scan,
        now decides slug collisions -- and a collision is never an error
        (#143 invariant 7: an "already exists" error would tell the caller
        someone else owns that exact slug). See tests/test_packs_registry.py
        for the full registry-level collision/ownership contract."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.pack.ownership import create_pack as _register_pack
        from opencrab.stores.sql_store import SQLStore

        sql = SQLStore("sqlite:///:memory:")
        _register_pack(sql, "someone-else", "existing-pack", title="Existing")

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, sql=sql)
            with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
                result = pack_create(title="Existing", pack_id="existing-pack")
        # #146 B: random suffix on collision, not sequential -2.
        assert "error" not in result
        assert result["pack_id"] != "existing-pack"
        assert result["pack_id"].startswith("existing-pack-")
        assert result["pack_id"] != "existing-pack-2"
        assert result["anchor_node"] == f"dataset:{result['pack_id']}"

    def test_error_anchor_node_write_failure(self):
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("graph down")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Broken", pack_id="broken-pack")
        assert result == {"error": "anchor node failed: graph down"}

    def test_error_anchor_node_store_failure_without_raising(self):
        """The anchor node write can also fail the way _ingest_into_pack's
        node/edge loops do: add_node returns normally (no exception) with an
        "error: ..." status inside stores. A missing/broken anchor means the
        pack doesn't really exist, so this must be a hard error, not a
        reported-as-created pack."""
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "error: disk I/O", "docs": "ok", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Broken Store", pack_id="broken-store-pack")
        assert result == {"error": "anchor node failed: graph: error: disk I/O"}

    def test_error_anchor_node_graph_unavailable(self):
        """graph unavailable for the anchor write is also a failure — the
        anchor would land nowhere that counts even if docs/sql wrote."""
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "unavailable", "docs": "ok", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="No Graph", pack_id="no-graph-pack")
        assert result == {"error": "anchor node failed: graph: unavailable"}

    def test_normal_anchor_optional_store_failure_still_reports_created(self):
        """graph write for the anchor succeeded but an optional store
        (docs/sql/vector) failed: the pack DOES exist (graph has it), so this
        must be reported as created (no top-level "error"), with status
        "partial" and the failed store surfaced in anchor_errors — not
        downgraded to the hard "anchor node failed" error, which would tell
        the caller to retry into a "pack already exists" dead end."""
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "ok", "docs": "error: mongo down", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Partial Store", pack_id="partial-store-pack")
        assert "error" not in result
        assert result["pack_id"] == "partial-store-pack"
        assert result["anchor_node"] == "dataset:partial-store-pack"
        assert result["status"] == "partial"
        assert result["anchor_errors"] == ["docs: error: mongo down"]


    def test_normal_with_text_materialises_evidence_node(self):
        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        hybrid = MagicMock()
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Doc Pack", pack_id="doc-pack", text="some content")
        assert result["status"] == "ok"
        assert result["evidence_node"] is not None
        assert result["evidence_node"].startswith("doc-pack:doc:")
        # anchor node + evidence node -> 2 add_node calls
        assert builder.add_node.call_count == 2

    def test_normal_bills_one_ingest_event_using_source_id(self):
        """#66: pack_create/pack_ingest never called billing.on_ingest — every
        pack write went unbilled. One call to _ingest_into_pack -> one
        on_ingest event, using the text's source_id when text is given.

        builder.add_node's mocked return must include a real "graph": "ok"
        (the only status OntologyBuilder.add_node ever assigns that key —
        see builder.py) — the billing gate now positively confirms the
        graph write landed (issue #66 codex re-review), so a bare
        MagicMock() return (no "graph" key at all) would no longer count as
        billable, same as it wouldn't in production.

        #145: tenant_id/subject_id are no longer pack_create arguments --
        tenant_id is fixed at 'default' and subject_id comes from the
        caller's server-derived principal (bound here via principal_scope
        instead of passed as a kwarg)."""
        from opencrab.auth import Principal, principal_scope

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        billing = MagicMock()
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, billing=billing)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="u1", is_local=True, disabled=False)):
                result = pack_create(
                    title="Doc Pack", pack_id="doc-pack", text="some content",
                )
        billing.on_ingest.assert_called_once_with("default", "u1", result["evidence_node"])

    def test_normal_bills_ingest_using_pack_id_when_nodes_given_but_no_text(self):
        """nodes given, no text -> no source_id, so on_ingest must fall back
        to pack_id (its signature requires a non-None string). subject_id is
        the bind_test_principal fixture's principal (#145: no longer a
        client argument)."""
        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        billing = MagicMock()
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, billing=billing)
            mock_list.return_value = {"packs": []}
            pack_create(
                title="Node Pack", pack_id="node-pack",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        billing.on_ingest.assert_called_once_with("default", "test-user", "node-pack")

    def test_normal_empty_pack_create_does_not_bill_ingest(self):
        """#66/#105 review: a pack_create with no nodes/edges/text creates
        only the anchor node (a separate write, outside _ingest_into_pack) —
        there is nothing for _ingest_into_pack itself to have ingested, so it
        must not fire a phantom on_ingest event for zero content."""
        builder = MagicMock()
        billing = MagicMock()
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder, billing=billing)
            mock_list.return_value = {"packs": []}
            pack_create(title="Empty Pack", pack_id="empty-pack")
        billing.on_ingest.assert_not_called()

    def test_error_all_writes_failed_does_not_bill_ingest(self):
        """#66/#105 review: builder.add_node/add_edge don't raise for a
        per-store failure — they report "error: ..."/"no match" inside the
        returned stores map (see builder.py's module docstring). A call
        where every provided item failed that way must not bill, even though
        no exception was ever raised."""
        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "error: disk down"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _writable_ctx("existing-pack", builder=builder, billing=billing)
            result = pack_ingest(
                "existing-pack",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        assert result["added_nodes"] == 0
        billing.on_ingest.assert_not_called()


class TestPackCreateCompensatingDelete:
    """#146 A: a failed anchor write must not leave a phantom registry row
    behind (create_pack already inserted the packs row before the anchor is
    attempted). Uses a real in-memory SQLStore -- not the MagicMock ``sql``
    the class above uses -- so the registry-row-gone assertion actually
    exercises ``delete_pack_row``'s SQL, not just the response shape."""

    @staticmethod
    def _ctx(sql, builder):
        return _base_ctx(builder=builder, sql=sql)

    def _sql(self):
        from opencrab.stores.sql_store import SQLStore

        return SQLStore("sqlite:///:memory:")

    def test_a1_anchor_exception_deletes_compensating_registry_row(self):
        from opencrab.pack.ownership import get_pack

        sql = self._sql()
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("graph down")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Broken", pack_id="broken-pack")
        assert result == {"error": "anchor node failed: graph down"}
        assert get_pack(sql, "broken-pack") is None

    def test_a2i_graph_available_write_failed_deletes_compensating_registry_row(self):
        """graph available but the write reports an "error: ..." status
        (not raised) -- same compensation as the exception path."""
        from opencrab.pack.ownership import get_pack

        sql = self._sql()
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "error: disk I/O", "docs": "ok", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Broken Store", pack_id="broken-store-pack")
        assert result == {"error": "anchor node failed: graph: error: disk I/O"}
        assert get_pack(sql, "broken-store-pack") is None

    def test_a2ii_graph_unavailable_deletes_compensating_registry_row(self):
        """graph unavailable is also "did not land", distinct from an
        "error: ..." status but compensated the same way."""
        from opencrab.pack.ownership import get_pack

        sql = self._sql()
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "unavailable", "docs": "ok", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="No Graph", pack_id="no-graph-pack")
        assert result == {"error": "anchor node failed: graph: unavailable"}
        assert get_pack(sql, "no-graph-pack") is None

    def test_a_graph_success_keeps_registry_row_even_with_optional_failure(self):
        """Once graph.add_node has actually succeeded the branch is
        unreachable -- an optional-store-only failure (docs here) must
        leave the registry row in place, never compensate-delete a pack
        that really exists."""
        from opencrab.pack.ownership import get_pack

        sql = self._sql()
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "ok", "docs": "error: mongo down", "sql": "ok"}
        }
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Partial Store", pack_id="partial-store-pack")
        assert "error" not in result
        assert get_pack(sql, "partial-store-pack") is not None

    def test_a6_failed_bob_compensation_never_touches_alices_row(self):
        """#146 A6: delete_pack_row's WHERE clause requires BOTH pack_id and
        owner_id to match. Alice successfully owns "shared-pack"; Bob's
        later attempt at the same title gets a random-suffixed slug (#146
        B) whose anchor then fails -- the compensating delete must remove
        only Bob's own row, never touch Alice's, in either creation order."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.pack.ownership import get_pack

        # Order 1: Alice succeeds first, then Bob's attempt fails & compensates.
        sql = self._sql()
        good_builder = MagicMock()
        good_builder.add_node.return_value = {"stores": {"graph": "ok"}}
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, good_builder)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                alice_result = pack_create(title="Shared", pack_id="shared-pack")
        assert "error" not in alice_result
        assert alice_result["pack_id"] == "shared-pack"

        bad_builder = MagicMock()
        bad_builder.add_node.side_effect = RuntimeError("graph down")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql, bad_builder)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="bob", is_local=True, disabled=False)):
                bob_result = pack_create(title="Shared", pack_id="shared-pack")
        assert "error" in bob_result
        # Alice's real row survives Bob's failed+compensated attempt.
        assert get_pack(sql, "shared-pack")["owner_id"] == "alice"

        # Order 2 (independent sql/registry): Bob fails first, then Alice
        # creates successfully at the exact requested slug -- Bob's failed
        # compensated attempt must not have poisoned the slug for Alice.
        sql2 = self._sql()
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql2, bad_builder)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="bob", is_local=True, disabled=False)):
                bob_first = pack_create(title="Shared", pack_id="shared-pack")
        assert "error" in bob_first
        assert get_pack(sql2, "shared-pack") is None  # compensated away

        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = self._ctx(sql2, good_builder)
            mock_list.return_value = {"packs": []}
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                alice_after = pack_create(title="Shared", pack_id="shared-pack")
        assert "error" not in alice_after
        assert alice_after["pack_id"] == "shared-pack"
        assert get_pack(sql2, "shared-pack")["owner_id"] == "alice"

    def test_a7_compensating_delete_failure_returns_original_error_and_warns(self, caplog):
        """#146 A7: SQL fault injection on the compensating delete itself --
        delete_pack_row raising must not mask the real anchor failure with a
        confusing delete-layer exception; the original error is still
        returned, and the delete failure is logged as a WARNING so an
        orphaned registry row can be found operationally."""
        import logging

        from opencrab.pack.ownership import get_pack

        sql = self._sql()
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("graph down")
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
            patch(
                "opencrab.pack.ownership.delete_pack_row",
                side_effect=RuntimeError("db exploded"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            mock_ctx.return_value = self._ctx(sql, builder)
            mock_list.return_value = {"packs": []}
            result = pack_create(title="Broken", pack_id="broken-pack")
        assert result == {"error": "anchor node failed: graph down"}
        assert any(
            "compensating delete failed" in rec.message and "db exploded" in rec.message
            for rec in caplog.records
        )
        # The delete never actually ran (it was replaced with a raise), so
        # the orphaned row is still there -- documents the real-world
        # consequence of a failed compensation, not asserting it's "fine".
        assert get_pack(sql, "broken-pack") is not None


# ---------------------------------------------------------------------------
# pack_ingest — error branches not covered by test_mcp.py's text-path tests
# ---------------------------------------------------------------------------


class TestPackIngestErrors:
    def test_error_pack_not_found(self):
        from opencrab.stores.sql_store import SQLStore

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql=SQLStore("sqlite:///:memory:"))
            result = pack_ingest("nonexistent-pack", text="hello")
        assert result == {
            "error": "pack not found; use pack_create first",
            "pack_id": "nonexistent-pack",
        }

    def test_error_no_content_provided(self):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _writable_ctx("existing-pack")
            result = pack_ingest("existing-pack")
        assert result == {
            "error": "no content provided: supply at least one of nodes, edges, or text"
        }

    def test_error_store_partial_failure_propagates_to_top_level_status(self):
        """pack_ingest hardcodes {"status": "ok", ..., **ingest_result} — this
        checks that _ingest_into_pack's real "partial" status wins over that
        literal default (dict literal: a later **-spread key overrides an
        earlier one), so callers see partial failure instead of always "ok"."""
        builder = MagicMock()
        builder.add_node.return_value = {
            "stores": {"graph": "error: disk I/O", "docs": "ok", "sql": "ok"}
        }
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _writable_ctx("existing-pack", builder=builder)
            result = pack_ingest(
                "existing-pack",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        assert result["status"] == "partial"
        assert result["added_nodes"] == 0
        assert result["node_errors"] == ["e1: graph: error: disk I/O"]

    def test_normal_bills_ingest_event(self):
        """#66: pack_ingest never called billing.on_ingest.

        #145: tenant_id/subject_id are no longer pack_ingest arguments --
        tenant_id is fixed at 'default' and subject_id comes from the
        caller's server-derived principal (bound here via principal_scope)."""
        from opencrab.auth import Principal, principal_scope

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        billing = MagicMock()
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _writable_ctx(
                "existing-pack", owner="u1", builder=builder, billing=billing
            )
            with principal_scope(Principal(user_id="u1", is_local=True, disabled=False)):
                pack_ingest(
                    "existing-pack",
                    nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
                )
        billing.on_ingest.assert_called_once_with("default", "u1", "existing-pack")

    def test_normal_billing_persist_failure_is_logged_but_ingest_still_succeeds(self, caplog):
        """#105: on_ingest's returned {"ok": ...} must actually be inspected,
        not discarded — a failed persist is logged, and does not fail the
        (already-succeeded) content write."""
        import logging

        builder = MagicMock()
        builder.add_node.return_value = {"stores": {"graph": "ok"}}
        billing = MagicMock()
        billing.on_ingest.return_value = {"ok": False, "error": "database is locked"}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _writable_ctx("existing-pack", builder=builder, billing=billing)
            with caplog.at_level(logging.WARNING):
                result = pack_ingest(
                    "existing-pack",
                    nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
                )
        assert result["status"] == "ok"
        assert any("on_ingest" in rec.message and "database is locked" in rec.message
                    for rec in caplog.records)


# ---------------------------------------------------------------------------
# harness_promotion_apply
# ---------------------------------------------------------------------------


_VALID_PACKAGE = {
    "package_id": "pkg-1",
    "mission_id": "mis-1",
    "run_id": "run-1",
    "nodes": [{"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}}],
    "edges": [],
}


class TestHarnessPromotionApply:
    @pytest.fixture(autouse=True)
    def _real_crabharness_import_root(self):
        """opencrab.mcp.tools.harness_promotion_apply does
        ``from crabharness.crabharness.models import PromotionPackage``,
        which needs the OUTER crabharness/ directory as the top-level
        ``crabharness`` package. tests/test_common_utils_characterization.py
        and tests/test_structural_characterization.py deliberately put
        crabharness/crabharness/ on sys.path instead (so crabharness's own
        internal ``from crabharness.models import ...`` imports resolve) —
        once either module has been collected in the same session,
        sys.modules['crabharness'] is pinned to that other root for the rest
        of the process, breaking this import. Force-clear the cached entries
        and prioritise the repo root on sys.path so this class's imports
        resolve correctly regardless of what already ran earlier in the
        full-suite ordering (this is a real cross-file environment hazard,
        not something introduced by these tests — see the two files above).

        Just re-prioritising the repo root on sys.path is NOT enough:
        crabharness/ has no __init__.py (a PEP 420 namespace portion when
        found via the repo root), and a namespace-package candidate never
        wins over a regular package found later in the scan — so as long as
        crabharness/crabharness/ (a *regular* package, __init__.py present)
        is anywhere on sys.path, `import crabharness` resolves to it
        regardless of ordering. The conflicting entry must be removed from
        sys.path entirely for the duration of this test, not just outranked.
        """
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        conflicting_entry = str(repo_root / "crabharness")
        removed = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "crabharness" or name.startswith("crabharness.")
        }
        old_path = list(sys.path)
        sys.path[:] = [p for p in sys.path if p != conflicting_entry]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        try:
            yield
        finally:
            sys.path[:] = old_path
            for name in list(sys.modules):
                if name == "crabharness" or name.startswith("crabharness."):
                    del sys.modules[name]
            sys.modules.update(removed)

    def test_normal_apply_writes_nodes_and_edges(self):
        from opencrab.stores.sql_store import SQLStore

        package = dict(_VALID_PACKAGE, edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"sql": "ok"}}
        builder.add_edge.return_value = {"receipt_id": "r2", "receipt_ts": "t2", "stores": {"sql": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # #148: harness_promotion_apply now resolves/authorizes a
            # pack_id (assert_writable) before writing -- needs a real
            # SQLStore so resolve_write_pack's ensure_default_pack call
            # actually creates and owns a pack for the bound principal
            # instead of hitting a MagicMock's opaque comparisons.
            mock_ctx.return_value = _base_ctx(builder=builder, sql=SQLStore("sqlite:///:memory:"))
            result = harness_promotion_apply(package, dry_run=False)
        assert result["package_id"] == "pkg-1"
        assert result["dry_run"] is False
        assert result["summary"]["nodes_written"] == 1
        assert result["summary"]["edges_written"] == 1
        assert result["summary"]["errors"] == 0
        assert result["node_receipts"][0]["node_id"] == "ds1"
        assert result["edge_receipts"][0] == {
            "from_id": "ds1", "relation": "related_to", "to_id": "ds2",
            "receipt_id": "r2", "receipt_ts": "t2", "stores": {"sql": "ok"},
        }

    def test_normal_bills_harness_apply_event(self):
        """#66: on_harness_apply had zero callers repo-wide before this fix.

        #145: tenant_id/subject_id are no longer harness_promotion_apply
        arguments -- tenant_id is fixed at 'default' and subject_id comes
        from the caller's server-derived principal (bound here via
        principal_scope)."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(
                builder=builder, billing=billing, sql=SQLStore("sqlite:///:memory:")
            )
            with principal_scope(Principal(user_id="u1", is_local=True, disabled=False)):
                harness_promotion_apply(_VALID_PACKAGE, dry_run=False)
        billing.on_harness_apply.assert_called_once_with("default", "u1", "pkg-1", 1)

    def test_normal_pack_id_reaches_every_node_and_edge_write(self):
        """Issue #119: subject_id reached on_harness_apply (billing) but never
        builder.add_node/add_edge (audit) in the promotion loop — every write
        in a promotion was audited with a null actor while the billing row
        named one.

        #148: builder.add_node/add_edge no longer take `subject_id` at all
        (the real OntologyBuilder derives the principal internally via
        current_principal()) -- the channel this loop now forwards
        consistently to every node and edge write is `pack_id`, resolved
        once from the SAME principal via resolve_write_pack. Pin BOTH the
        node and edge writes receive the SAME pack_id (a partial fix across
        a multi-write op is worse than the original bug)."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.pack.ownership import resolve_write_pack
        from opencrab.stores.sql_store import SQLStore

        package = dict(_VALID_PACKAGE, nodes=[
            {"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}},
            {"space": "resource", "node_type": "Dataset", "node_id": "ds2", "properties": {}},
        ], edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        builder.add_edge.return_value = {"receipt_id": "r2", "receipt_ts": "t2", "stores": {"graph": "ok"}}
        sql = SQLStore("sqlite:///:memory:")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, sql=sql)
            actor = Principal(user_id="actor-1", is_local=True, disabled=False)
            with principal_scope(actor):
                harness_promotion_apply(package, dry_run=False)
                expected_pack_id = resolve_write_pack(sql, actor, None)
        for call in builder.add_node.call_args_list:
            assert call.kwargs["pack_id"] == expected_pack_id
        assert builder.add_edge.call_args.kwargs["pack_id"] == expected_pack_id

    def test_normal_uses_bound_principal_by_default(self):
        """#145: there is no "omitted subject_id" state anymore --
        harness_promotion_apply always uses whatever current_principal()
        resolves to (bound here by the bind_test_principal fixture).

        #148: see test_normal_pack_id_reaches_every_node_and_edge_write
        above for why this checks `pack_id` (resolved from the bound
        principal) rather than a `subject_id` kwarg that no longer exists
        on builder.add_node/add_edge."""
        from opencrab.auth import current_principal
        from opencrab.pack.ownership import resolve_write_pack
        from opencrab.stores.sql_store import SQLStore

        package = dict(_VALID_PACKAGE, edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        builder.add_edge.return_value = {"receipt_id": "r2", "receipt_ts": "t2", "stores": {"graph": "ok"}}
        sql = SQLStore("sqlite:///:memory:")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, sql=sql)
            harness_promotion_apply(package, dry_run=False)
            expected_pack_id = resolve_write_pack(sql, current_principal(), None)
        assert builder.add_node.call_args.kwargs["pack_id"] == expected_pack_id
        assert builder.add_edge.call_args.kwargs["pack_id"] == expected_pack_id

    def test_error_malformed_receipt_does_not_bill(self):
        """#66 codex re-review, finding [3]: a "stores" map with no "graph"
        key at all (e.g. only optional stores reported) is a receipt shape
        this code doesn't recognize — fail-closed means that must NOT bill,
        not fall through to "no known failure -> bill it"."""
        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"sql": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(
                builder=builder, billing=billing, sql=SQLStore("sqlite:///:memory:")
            )
            result = harness_promotion_apply(_VALID_PACKAGE, dry_run=False)
        assert len(result["node_receipts"]) == 1  # receipt still recorded, unbilled
        billing.on_harness_apply.assert_not_called()

    def test_normal_billing_persist_failure_is_logged_but_apply_still_succeeds(self, caplog):
        """#105: on_harness_apply's returned {"ok": ...} must actually be
        inspected, not discarded — a failed persist is logged, and does not
        fail the (already-applied) promotion package."""
        import logging

        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        billing.on_harness_apply.return_value = {"ok": False, "error": "database is locked"}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(
                builder=builder, billing=billing, sql=SQLStore("sqlite:///:memory:")
            )
            with caplog.at_level(logging.WARNING):
                result = harness_promotion_apply(_VALID_PACKAGE, dry_run=False)
        assert result["summary"]["errors"] == 0
        assert any("on_harness_apply" in rec.message and "database is locked" in rec.message
                    for rec in caplog.records)

    def test_normal_dry_run_does_not_bill(self):
        """dry_run never calls _get_context (asserted above), so billing must
        stay untouched too — nothing was written."""
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            harness_promotion_apply(_VALID_PACKAGE, dry_run=True)
        mock_ctx.assert_not_called()

    def test_error_graph_write_failure_does_not_bill(self):
        """#66/#105 review: builder.add_node() doesn't raise for a per-store
        failure — it returns normally with "error: ..." inside stores
        (builder.py's module docstring). node_receipts still gets an entry
        (unchanged contract), but billing must not count it: no exception was
        raised, yet nothing actually landed in the graph."""
        from opencrab.stores.sql_store import SQLStore

        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.return_value = {
            "receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "error: disk down"}
        }
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(
                builder=builder, billing=billing, sql=SQLStore("sqlite:///:memory:")
            )
            result = harness_promotion_apply(_VALID_PACKAGE, dry_run=False)
        assert len(result["node_receipts"]) == 1  # receipt still recorded, unbilled
        billing.on_harness_apply.assert_not_called()

    def test_normal_partial_success_bills_only_the_nodes_that_landed(self):
        """Mixed batch: one node write succeeds, one fails at the store
        level. Billing must count only the successful one, not
        len(node_receipts) (which would be 2)."""
        from opencrab.stores.sql_store import SQLStore

        package = dict(_VALID_PACKAGE, nodes=[
            {"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}},
            {"space": "resource", "node_type": "Dataset", "node_id": "ds2", "properties": {}},
        ])
        builder = MagicMock()
        billing = MagicMock()
        builder.add_node.side_effect = [
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}},
            {"receipt_id": "r2", "receipt_ts": "t2", "stores": {"graph": "error: disk down"}},
        ]
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(
                builder=builder, billing=billing, sql=SQLStore("sqlite:///:memory:")
            )
            result = harness_promotion_apply(package, dry_run=False)
        assert len(result["node_receipts"]) == 2
        billing.on_harness_apply.assert_called_once_with("default", "test-user", "pkg-1", 1)

    def test_error_apply_node_and_edge_write_failures_recorded(self):
        """Real (non-dry-run) write failures for both nodes and edges are
        collected into errors without aborting the loop — mirrors the
        dry_run error-collection contract but for the actual write path."""
        from opencrab.stores.sql_store import SQLStore

        package = dict(_VALID_PACKAGE, edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("node write failed")
        builder.add_edge.side_effect = RuntimeError("edge write failed")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, sql=SQLStore("sqlite:///:memory:"))
            result = harness_promotion_apply(package, dry_run=False)
        assert result["node_receipts"] == []
        assert result["edge_receipts"] == []
        assert result["errors"] == [
            {"node_id": "ds1", "error": "node write failed"},
            {"edge": "ds1-[related_to]->ds2", "error": "edge write failed"},
        ]
        assert result["summary"] == {"nodes_written": 0, "edges_written": 0, "errors": 2}

    def test_normal_dry_run_validates_without_writing(self):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_get_ctx = mock_ctx  # not expected to be called
            result = harness_promotion_apply(_VALID_PACKAGE, dry_run=True)
        mock_get_ctx.assert_not_called()
        assert result["dry_run"] is True
        assert result["errors"] == []
        assert result["node_receipts"] == [
            {"node_id": "ds1", "space": "resource", "node_type": "Dataset", "status": "dry_run_valid"}
        ]

    def test_error_dry_run_invalid_space_recorded_in_errors(self):
        package = dict(_VALID_PACKAGE, nodes=[
            {"space": "not-a-space", "node_type": "Dataset", "node_id": "bad1", "properties": {}}
        ])
        result = harness_promotion_apply(package, dry_run=True)
        assert result["node_receipts"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["node_id"] == "bad1"

    def test_error_dry_run_invalid_properties_recorded_in_errors(self):
        """Valid (space, node_type) combo but properties fail the registered
        Type Schema (Entity requires name + entity_type) — the pr.valid=False
        branch, distinct from the r.valid=False (bad space) branch above."""
        package = dict(_VALID_PACKAGE, nodes=[
            {"space": "concept", "node_type": "Entity", "node_id": "bad2", "properties": {}}
        ])
        result = harness_promotion_apply(package, dry_run=True)
        assert result["node_receipts"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["node_id"] == "bad2"

    def test_error_crabharness_not_installed(self, monkeypatch):
        # Python's import machinery looks up sys.modules by the *full* dotted
        # name first ("crabharness.crabharness.models") — once that submodule
        # has been cached by an earlier test's successful import, blanking
        # only the top-level "crabharness" entry has no effect. Block the
        # exact name the `from ... import` statement resolves.
        monkeypatch.setitem(sys.modules, "crabharness.crabharness.models", None)
        result = harness_promotion_apply(_VALID_PACKAGE)
        assert result == {
            "error": "crabharness package not installed. Run: pip install -e crabharness/"
        }

    def test_error_malformed_package_returns_validation_message(self):
        result = harness_promotion_apply({"not": "a valid package"})
        assert "error" in result
        assert "Invalid PromotionPackage" in result["error"]

    def test_edge_empty_package_apply(self):
        from opencrab.stores.sql_store import SQLStore

        empty = {"package_id": "pkg-e", "mission_id": "mis-e", "run_id": "run-e"}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql=SQLStore("sqlite:///:memory:"))
            result = harness_promotion_apply(empty, dry_run=False)
        assert result["node_receipts"] == []
        assert result["edge_receipts"] == []
        assert result["summary"] == {"nodes_written": 0, "edges_written": 0, "errors": 0}

    def test_edge_empty_package_dry_run(self):
        empty = {"package_id": "pkg-e", "mission_id": "mis-e", "run_id": "run-e"}
        result = harness_promotion_apply(empty, dry_run=True)
        assert result == {
            "package_id": "pkg-e", "dry_run": True,
            "node_receipts": [], "edge_receipts": [], "errors": [],
        }


# ---------------------------------------------------------------------------
# _get_context — the real (unmocked) wiring path
# ---------------------------------------------------------------------------


class TestGetContextRealWiring:
    def test_real_context_builds_all_store_keys_without_network(self, tmp_path, monkeypatch):
        """Exercises _get_context()'s actual body (no patching of
        _get_context itself): make_graph_store/make_vector_store/
        make_doc_store/make_sql_store + engine construction, against a fresh
        LOCAL_DATA_DIR. The KURE embedding function factory is mocked (not
        the vector store itself) to avoid any GGUF download or remote HTTP
        call — see tests/_vec_helpers.py's MockEF convention."""
        import opencrab.config as config_mod
        import opencrab.stores.factory as factory_mod
        from opencrab.mcp import tools as tools_mod
        from tests._vec_helpers import MockEF

        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        config_mod.get_settings.cache_clear()
        monkeypatch.setattr(
            factory_mod, "_make_kure_embedding_function", lambda settings: MockEF()
        )

        tools_mod._context.clear()
        try:
            result = tools_mod.dispatch_tool("content_pack_list", {})
            # #146 C: a fresh LOCAL_DATA_DIR has no `packs` registry rows,
            # so the registry-sourced candidate list is empty regardless of
            # the (real, available) graph store's own state.
            assert result["total"] == 0
            assert result["packs"] == []
            assert set(tools_mod._context.keys()) == {
                "neo4j", "chroma", "mongo", "sql",
                "builder", "rebac", "impact", "hybrid", "billing",
            }
        finally:
            tools_mod._context.clear()
            config_mod.get_settings.cache_clear()
