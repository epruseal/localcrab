"""
Billing coverage for crabharness's CLI/library promotion-apply path.

Issue #66 review (codex adversarial verification): the first audit pass only
looked at MCP-exposed tools and concluded `on_promotion` had "no tool to call
it from". `crabharness/crabharness/apply.py#apply_promotion_package` (reachable
via `crabharness apply` on the CLI, see crabharness/crabharness/cli.py) is a
second, non-MCP write surface that applies a whole PromotionPackage via
OntologyBuilder directly — same shape as opencrab/mcp/tools/harness.py's
`harness_promotion_apply`, so it is billed the same way: as `harness_apply`,
not by resurrecting `on_promotion` (whose per-node-id signature doesn't match
either surface).

These tests patch the store factories and BillingHooks so no real DB/graph
connection is needed — same approach as tests/test_tools_handlers_direct.py's
TestHarnessPromotionApply, which this file's fixture below is copied from
(see that fixture's docstring for why the sys.path juggling is necessary).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _real_crabharness_import_root():
    """See tests/test_tools_handlers_direct.py's TestHarnessPromotionApply
    fixture of the same name for the full rationale: crabharness/ has no
    __init__.py, so whichever sys.path entry is scanned first "wins" the
    `crabharness` top-level name for the rest of the process. Force the repo
    root to win here regardless of what already ran earlier in the suite.
    """
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


_VALID_PACKAGE = {
    "package_id": "pkg-1",
    "mission_id": "mis-1",
    "run_id": "run-1",
    "nodes": [{"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}}],
    "edges": [],
}


def _write_package(tmp_path: Path, package: dict) -> str:
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    return str(path)


def _patch_factories(builder_add_node_return):
    """Patch the three store factories apply_promotion_package calls plus
    BillingHooks, and configure the mocked OntologyBuilder's add_node."""
    graph, docs, sql = MagicMock(), MagicMock(), MagicMock()
    builder_instance = MagicMock()
    builder_instance.add_node.return_value = builder_add_node_return
    billing_instance = MagicMock()
    return graph, docs, sql, builder_instance, billing_instance


_VALID_PACKAGE_WITH_EDGE = {
    "package_id": "pkg-1",
    "mission_id": "mis-1",
    "run_id": "run-1",
    "nodes": [{"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}}],
    "edges": [{
        "from_space": "resource", "from_id": "ds1", "relation": "related_to",
        "to_space": "resource", "to_id": "ds2",
    }],
}


class TestApplyPromotionPackageSubjectIdAudit:
    """Issue #119: apply_promotion_package (crabharness apply's non-MCP twin
    of harness.py#harness_promotion_apply) had the same gap: subject_id
    reached on_harness_apply (billing) but not builder.add_node/add_edge
    (audit) in its promotion loop."""

    def test_normal_subject_id_reaches_both_node_and_edge_write(self, tmp_path):
        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        )
        builder_instance.add_edge.return_value = {
            "receipt_id": "r2", "receipt_ts": "t2", "stores": {"graph": "ok"}
        }
        package_path = _write_package(tmp_path, _VALID_PACKAGE_WITH_EDGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            apply_promotion_package(package_path, dry_run=False, tenant_id="acme", subject_id="u1")
        assert builder_instance.add_node.call_args.kwargs["subject_id"] == "u1"
        assert builder_instance.add_edge.call_args.kwargs["subject_id"] == "u1"

    def test_normal_omitted_subject_id_keeps_existing_behaviour(self, tmp_path):
        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        )
        builder_instance.add_edge.return_value = {
            "receipt_id": "r2", "receipt_ts": "t2", "stores": {"graph": "ok"}
        }
        package_path = _write_package(tmp_path, _VALID_PACKAGE_WITH_EDGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            apply_promotion_package(package_path, dry_run=False)
        assert builder_instance.add_node.call_args.kwargs["subject_id"] is None
        assert builder_instance.add_edge.call_args.kwargs["subject_id"] is None


class TestApplyPromotionPackageBilling:
    def test_normal_bills_harness_apply_event(self, tmp_path):
        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        )
        package_path = _write_package(tmp_path, _VALID_PACKAGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            result = apply_promotion_package(package_path, dry_run=False, tenant_id="acme", subject_id="u1")
        assert result["summary"]["nodes_written"] == 1
        billing_instance.on_harness_apply.assert_called_once_with("acme", "u1", "pkg-1", 1)
        assert result["billing"] == {"billed_node_count": 1, "ok": True}

    def test_normal_billing_persist_failure_is_visible_in_result_not_just_a_log(self, tmp_path, caplog):
        """#66 codex re-review, finding [4]: apply_promotion_package's outer
        try/except only catches raised exceptions — on_harness_apply()
        itself never raises (it returns {"ok": False, ...} on a failed
        persist, same fire-and-forget contract as every other on_* wrapper).
        Before this fix that soft failure was discarded with ZERO
        visibility, not even a log line. Now it must show up both as a log
        line (#105's minimum bar) and in the returned/printed result dict
        (crabharness/cli.py's `_write()` prints this dict as the CLI's own
        output) — a log alone is what #105 already flagged as insufficient."""
        import logging

        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "ok"}}
        )
        billing_instance.on_harness_apply.return_value = {"ok": False, "error": "database is locked"}
        package_path = _write_package(tmp_path, _VALID_PACKAGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            with caplog.at_level(logging.WARNING):
                result = apply_promotion_package(package_path, dry_run=False)
        assert result["summary"]["nodes_written"] == 1  # apply itself still succeeded
        assert result["billing"] == {
            "billed_node_count": 1, "ok": False, "error": "database is locked",
        }
        assert any("harness_apply" in rec.message and "database is locked" in rec.message
                    for rec in caplog.records)

    def test_error_graph_write_failure_does_not_bill(self, tmp_path):
        """Same accuracy fix as harness.py: OntologyBuilder.add_node() doesn't
        raise for a per-store failure, so a graph "error: ..." status must not
        be billed even though node_receipts still records the attempt."""
        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"graph": "error: disk down"}}
        )
        package_path = _write_package(tmp_path, _VALID_PACKAGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            result = apply_promotion_package(package_path, dry_run=False)
        assert len(result["node_receipts"]) == 1  # receipt still recorded, unbilled
        billing_instance.on_harness_apply.assert_not_called()

    def test_error_malformed_receipt_does_not_bill(self, tmp_path):
        """Fail-closed pin (#66 codex re-review, finding [3]): a "stores" map
        with no "graph" key at all must not bill — an unrecognized receipt
        shape is not a positive success signal."""
        graph, docs, sql, builder_instance, billing_instance = _patch_factories(
            {"receipt_id": "r1", "receipt_ts": "t1", "stores": {"sql": "ok"}}
        )
        package_path = _write_package(tmp_path, _VALID_PACKAGE)
        with (
            patch("opencrab.stores.factory.make_graph_store", return_value=graph),
            patch("opencrab.stores.factory.make_doc_store", return_value=docs),
            patch("opencrab.stores.factory.make_sql_store", return_value=sql),
            patch("opencrab.ontology.builder.OntologyBuilder", return_value=builder_instance),
            patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance),
        ):
            from crabharness.crabharness.apply import apply_promotion_package

            apply_promotion_package(package_path, dry_run=False)
        billing_instance.on_harness_apply.assert_not_called()

    def test_normal_dry_run_does_not_bill(self, tmp_path):
        billing_instance = MagicMock()
        package_path = _write_package(tmp_path, _VALID_PACKAGE)
        with patch("opencrab.billing.hooks.BillingHooks", return_value=billing_instance):
            from crabharness.crabharness.apply import apply_promotion_package

            result = apply_promotion_package(package_path, dry_run=True)
        assert result["dry_run"] is True
        billing_instance.on_harness_apply.assert_not_called()
