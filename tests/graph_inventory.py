"""Deterministic issue #80 graph-writer inventory.

This module is an evidence producer, not a hand-maintained allowlist.  It
walks tracked Python source, resolves the small set of graph mutation sinks
used by this repository, and emits source locations and query digests.  The
qualification command is intentionally read-only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
EXCLUDED_PARTS = frozenset({"generated", "vendor", ".venv", "build"})
GRAPH_METHODS = frozenset({
    "upsert_node", "update_node", "upsert_nodes_batch", "update_nodes_batch",
    "reclassify_node", "migrate_graph_identity",
    "upsert_edge", "update_edge", "upsert_edges_batch", "update_edges_batch",
    "delete_node", "delete_edge", "backfill_pack_provenance", "ensure_constraints",
})
GRAPH_TOKENS = re.compile(r"\b(?:graph_nodes|graph_edges|OpenCrabNode|OntologyNode|OntologyEdge)\b", re.I)
MUTATION_TOKENS = re.compile(r"\b(?:CREATE|MERGE|SET|REMOVE|DELETE|DETACH|DROP|ALTER|LOAD|FOREACH)\b", re.I)


def _candidate_id(item: dict[str, Any]) -> str:
    """Give each collected sink a concrete, reproducible identity."""
    payload = {
        key: item[key]
        for key in ("locator", "owner", "sink", "query_digest", "query_kind", "resolved")
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _tracked_python() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    paths = []
    for raw in sorted(set(result.stdout.splitlines())):
        path = Path(raw)
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        absolute = REPO / path
        if absolute.is_file():
            paths.append(path)
    return paths


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("<dynamic>")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _query_literal(node: ast.AST | None) -> str | None:
    """Resolve the query argument when callers wrap it in ``sqlalchemy.text``."""
    value = _literal(node)
    if value is not None:
        return value
    if isinstance(node, ast.Call) and node.args:
        return _literal(node.args[0])
    return None


def _owner(path: Path, stack: list[str]) -> str:
    module = ".".join(path.with_suffix("").parts)
    return ":".join([module, *stack]) if stack else f"{module}:<module>"


def _query_kind(text: str | None) -> list[str]:
    if not text:
        return ["unresolved"]
    upper = text.upper()
    kinds: list[str] = []
    if re.search(r"\b(?:SELECT|WITH)\b", upper):
        kinds.append("read_sql")
    if re.search(r"\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", upper):
        kinds.append("write_sql")
    if re.search(r"\b(?:MATCH|RETURN|WITH|UNWIND|CALL)\b", upper):
        kinds.append("read_cypher")
    if MUTATION_TOKENS.search(text):
        kinds.append("write_cypher")
    return sorted(set(kinds or ["unknown"]))


def inventory() -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    for relative in _tracked_python():
        try:
            tree = ast.parse((REPO / relative).read_text(encoding="utf-8"), filename=str(relative))
        except SyntaxError:
            continue
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                query = _query_literal(node.args[0]) if node.args else None
                graph_method = attr in GRAPH_METHODS
                graph_text = query is not None and bool(GRAPH_TOKENS.search(query))
                receiver = _literal(func.value) if isinstance(func, ast.Attribute) else None
                proven_graph_receiver = isinstance(func, ast.Attribute) and (
                    attr in GRAPH_METHODS or str(receiver or "").lower() in {"graph", "store", "tx", "session"}
                )
                if (
                    attr == "execute"
                    and relative.as_posix() == "opencrab/auth.py"
                    and query
                    and re.search(r"\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", query, re.I)
                ):
                    negatives.append({
                        "locator": f"{relative.as_posix()}:{node.lineno}",
                        "reason": "known non-graph SQL receiver",
                        "query_digest": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    })
                elif graph_method or graph_text or (attr in {"execute", "executemany", "run"} and proven_graph_receiver):
                    owner = _owner(relative, stack)
                    source = ast.get_source_segment((REPO / relative).read_text(encoding="utf-8"), node) or ""
                    query_digest = hashlib.sha256((query or source).encode("utf-8")).hexdigest()
                    candidates.append({
                        "locator": f"{relative.as_posix()}:{node.lineno}",
                        "owner": owner,
                        "sink": attr or "call",
                        "query_digest": query_digest,
                        "query_kind": _query_kind(query or source),
                        "resolved": query is not None or graph_method,
                        "candidate_id": "",
                    })
                self.generic_visit(node)

        Visitor().visit(tree)
    candidates.sort(key=lambda item: (item["locator"], item["owner"], item["sink"]))
    for item in candidates:
        item["candidate_id"] = _candidate_id(item)
    negatives.sort(key=lambda item: item["locator"])
    return {
        "schema_version": "issue80.graph-inventory.v1",
        "exclusions": sorted(EXCLUDED_PARTS),
        "candidates": candidates,
        "negative_classifications": negatives,
        "gates": {
            "graph_candidate_unclassified": "derived from candidates.resolved",
            "owned_graph_writer_set": sorted({item["owner"] for item in candidates}),
            "known_negative_classification_drift": sorted(item["locator"] for item in negatives),
        },
    }


def validate_inventory(value: dict[str, Any]) -> None:
    """Check the bidirectional candidate identity invariant and fixture drift."""
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        raise RuntimeError("graph inventory candidates are malformed")
    ids = [item.get("candidate_id") for item in candidates]
    if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
        raise RuntimeError("graph inventory candidate identity is missing")
    if len(set(ids)) != len(ids):
        raise RuntimeError("graph inventory candidate identity is not unique")
    for item in candidates:
        expected = _candidate_id(item)
        if item["candidate_id"] != expected:
            raise RuntimeError(f"graph inventory candidate identity drift: {item.get('locator')}")

    negative_fixture = REPO / "tests" / "fixtures" / "issue80_graph_negative_inventory.v1.json"
    if negative_fixture.is_file():
        fixture = json.loads(negative_fixture.read_text(encoding="utf-8"))
        negatives = {item.get("locator") for item in value.get("negative_classifications", [])}
        for example in fixture.get("required_examples", []):
            locator = example.get("locator")
            if locator not in negatives:
                raise RuntimeError(f"graph inventory negative classification drift: {locator}")


def main() -> None:
    result = inventory()
    validate_inventory(result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
