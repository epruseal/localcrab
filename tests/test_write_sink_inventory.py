"""Every store mutation is either a gated writer or a declared exception (#148).

This test exists because a hand-maintained list of "places that write" missed
one every single time in this work -- and the miss that reached a review was
exactly this shape: `write_source` was added as "the second writer" and shipped
without the authorization call, with the whole suite green, because nothing
asserted which functions are allowed to touch a store at all.

So the list is derived, not written: walk the source, find every call to a
store mutation method, and require each call site to appear below with a
reason. A new sink fails here until someone classifies it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Mutating methods on the store classes. Read methods are deliberately absent:
# this guards writes, and #143's read scoping is a separate contract.
WATCHED_METHODS = frozenset({
    "upsert_node",
    "upsert_node_doc",
    "upsert_edge",
    "upsert_nodes_batch",
    "upsert_edges_batch",
    "upsert_source",
    "add_texts",
    "upsert_texts",
    "register_node",
    "register_edge",
})

SCANNED_ROOTS = ("opencrab", "apps", "scripts", "crabharness")

# (module path, enclosing function) -> why this is allowed to write directly.
# Keyed at function granularity on purpose: a module-level allowlist would let
# a new direct write slip into an already-blessed file.
ALLOWED: dict[tuple[str, str], str] = {
    # --- writer 1: the ontology builder ---
    ("opencrab/ontology/builder.py", "add_node"): "writer 1 -- gate runs here",
    ("opencrab/ontology/builder.py", "add_edge"): "writer 1 -- gate runs here",
    # --- writer 2: the source writer, and the vector leg it drives ---
    ("opencrab/pack/source_writer.py", "write_source"): "writer 2 -- gate runs here",
    ("opencrab/ontology/query.py", "ingest"): (
        "vector leg of writer 2; reachable only through write_source, which "
        "authorizes and stamps before calling it"
    ),
    # --- bulk pack loader: principal enforced, pack authorization is a known gap ---
    ("opencrab/pack/load.py", "flush"): "bulk loader; see design section 13.9",
    ("opencrab/pack/load.py", "flush_single"): "bulk loader; see design section 13.9",
    ("opencrab/pack/load.py", "load_chunks_incremental"): (
        "bulk loader; see design section 13.9"
    ),
    # --- explicitly out of scope (issue #148 acceptance narrowed to pack
    #     content writes; these are operator tools run locally, not client
    #     surfaces) ---
    ("scripts/migrate_to_local.py", "migrate_docs"): "migration tool, --apply gated",
    ("scripts/migrate_to_local.py", "migrate_graph"): "migration tool, --apply gated",
    ("scripts/migrate_pack_ownership.py", "_backfill_vector"): (
        "migration tool, --apply gated"
    ),
    ("scripts/bench_graph_backends.py", "ingest_to_store"): "developer benchmark",
    ("scripts/seed_ontology.py", "seed"): "operator seeding tool, binds a principal",
    ("scripts/import_obsidian_vault.py", "_import_vault_unlocked"): (
        "operator import tool, binds a principal"
    ),
}


def _call_sites() -> dict[tuple[str, str], set[str]]:
    """(module, function) -> the watched methods it calls."""
    sites: dict[tuple[str, str], set[str]] = {}
    for root in SCANNED_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            # The store classes implement these methods; they are the sink,
            # not a call site.
            if "/stores/" in f"/{rel}":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover -- would fail collection anyway
                continue
            enclosing: list[str] = []

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    enclosing.append(node.name)
                    self.generic_visit(node)
                    enclosing.pop()

                visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

                def visit_Call(self, node):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in WATCHED_METHODS:
                        key = (rel, enclosing[-1] if enclosing else "<module>")
                        sites.setdefault(key, set()).add(func.attr)
                    self.generic_visit(node)

            Visitor().visit(tree)
    return sites


def test_every_store_mutation_is_declared():
    sites = _call_sites()
    undeclared = sorted(k for k in sites if k not in ALLOWED)
    assert not undeclared, (
        "these call a store mutation method directly and are not declared in "
        "ALLOWED. Route them through a writer, or add an entry with the reason "
        "they are exempt (see issue #148's scope table):\n"
        + "\n".join(f"  {m}::{fn} -> {sorted(sites[(m, fn)])}" for m, fn in undeclared)
    )


def test_allowlist_has_no_stale_entries():
    """A dead entry is worse than none: it reads as coverage that is gone."""
    sites = _call_sites()
    stale = sorted(k for k in ALLOWED if k not in sites)
    assert not stale, (
        "these ALLOWED entries no longer call any watched method -- remove "
        f"them: {stale}"
    )


def test_the_scan_actually_finds_the_writers():
    """Guards the guard: a broken walk would make both tests above vacuous."""
    sites = _call_sites()
    assert ("opencrab/ontology/builder.py", "add_node") in sites
    assert ("opencrab/pack/source_writer.py", "write_source") in sites


@pytest.mark.parametrize("writer", [
    ("opencrab/ontology/builder.py", "add_node"),
    ("opencrab/ontology/builder.py", "add_edge"),
    ("opencrab/pack/source_writer.py", "write_source"),
])
def test_each_writer_authorizes(writer):
    """The writers must call the gate's authorize, not merely stamp.

    Pinned because the shipped-and-reviewed version of write_source stamped
    without authorizing, and no test noticed.
    """
    module, func = writer
    tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "authorize" in called, f"{module}::{func} does not call authorize()"
            return
    pytest.fail(f"{func} not found in {module}")
