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
from opencrab.ontology.builder import store_write_failures


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
    ctx.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# content_pack_list
# ---------------------------------------------------------------------------


class TestContentPackList:
    def test_normal_strips_pack_suffix_from_title(self):
        graph = MagicMock()
        graph.available = True
        graph.list_packs.return_value = [
            {"pack_id": "biomed", "node_count": 5, "sample_title": "Biomed ontology pack"},
        ]
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list(min_nodes=1)
        assert result == {
            "total": 1,
            "packs": [{"pack_id": "biomed", "node_count": 5, "title": "Biomed"}],
        }

    def test_normal_falls_back_to_pack_id_when_no_title(self):
        graph = MagicMock()
        graph.available = True
        graph.list_packs.return_value = [{"pack_id": "p1", "node_count": 2, "sample_title": ""}]
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list()
        assert result["packs"][0]["title"] == "p1"

    def test_edge_graph_unavailable_returns_error(self):
        graph = MagicMock()
        graph.available = False
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(neo4j=graph)
            result = content_pack_list()
        assert result == {"error": "graph store unavailable"}


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
        mongo.upsert_source.side_effect = RuntimeError("mongo down")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder, hybrid=hybrid, mongo=mongo)
            result = _ingest_into_pack(
                "pack-a", text="legacy text", source_id="src-3", text_as_node=False,
            )
        assert result["stores"]["mongodb"] == "error: mongo down"

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

    def test_error_pack_already_exists(self):
        with patch("opencrab.mcp.tools.content_pack_list") as mock_list:
            mock_list.return_value = {"packs": [{"pack_id": "existing-pack"}]}
            result = pack_create(title="Existing", pack_id="existing-pack")
        assert result == {
            "error": "pack already exists",
            "pack_id": "existing-pack",
            "hint": "use pack_ingest to add more content",
        }

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


# ---------------------------------------------------------------------------
# pack_ingest — error branches not covered by test_mcp.py's text-path tests
# ---------------------------------------------------------------------------


class TestPackIngestErrors:
    def test_error_pack_not_found(self):
        with patch("opencrab.mcp.tools.content_pack_list") as mock_list:
            mock_list.return_value = {"packs": []}
            result = pack_ingest("nonexistent-pack", text="hello")
        assert result == {
            "error": "pack not found; use pack_create first",
            "pack_id": "nonexistent-pack",
        }

    def test_error_no_content_provided(self):
        with patch("opencrab.mcp.tools.content_pack_list") as mock_list:
            mock_list.return_value = {"packs": [{"pack_id": "existing-pack"}]}
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
        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            mock_ctx.return_value = _base_ctx(builder=builder)
            mock_list.return_value = {"packs": [{"pack_id": "existing-pack"}]}
            result = pack_ingest(
                "existing-pack",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
            )
        assert result["status"] == "partial"
        assert result["added_nodes"] == 0
        assert result["node_errors"] == ["e1: graph: error: disk I/O"]


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
        package = dict(_VALID_PACKAGE, edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.return_value = {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"sql": "ok"}}
        builder.add_edge.return_value = {"receipt_id": "r2", "receipt_ts": "t2", "stores": {"sql": "ok"}}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
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

    def test_error_apply_node_and_edge_write_failures_recorded(self):
        """Real (non-dry-run) write failures for both nodes and edges are
        collected into errors without aborting the loop — mirrors the
        dry_run error-collection contract but for the actual write path."""
        package = dict(_VALID_PACKAGE, edges=[{
            "from_space": "resource", "from_id": "ds1", "relation": "related_to",
            "to_space": "resource", "to_id": "ds2",
        }])
        builder = MagicMock()
        builder.add_node.side_effect = RuntimeError("node write failed")
        builder.add_edge.side_effect = RuntimeError("edge write failed")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(builder=builder)
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
        empty = {"package_id": "pkg-e", "mission_id": "mis-e", "run_id": "run-e"}
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx()
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
            assert result == {"total": 0, "packs": []}
            assert set(tools_mod._context.keys()) == {
                "neo4j", "chroma", "mongo", "sql",
                "builder", "rebac", "impact", "hybrid", "billing",
            }
        finally:
            tools_mod._context.clear()
            config_mod.get_settings.cache_clear()
