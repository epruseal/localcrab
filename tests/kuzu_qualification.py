"""Read-only qualification gate for the optional Ladybug/Kùzu backend.

The production constructor is intentionally capability-negative until a
recorded transaction probe proves one connection owns begin/execute/commit/
rollback. This module validates the retained bundle without importing the
optional package or opening a database. A future qualification run may add a
positive probe behind an explicit disposable fixture entry point.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "fixtures" / "issue80" / "qualification"


def _load(name: str) -> dict[str, Any]:
    with (BUNDLE / name).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid qualification artifact: {name}")
    return value


def validate_bundle() -> dict[str, Any]:
    """Validate package identity and capability evidence, read-only."""
    manifest = _load("manifest.json")
    raw = _load("raw_pypi_metadata.json")
    capability = _load("kuzu_capability.json")
    version = manifest.get("runtime_version")
    wheel_hash = manifest.get("wheel_sha256")
    if not isinstance(version, str) or "==" not in version:
        raise RuntimeError("qualification requires an exact package version")
    if not isinstance(wheel_hash, str) or len(wheel_hash) != 64:
        raise RuntimeError("qualification requires a wheel sha256")
    if raw.get("project") != "ladybug" or raw.get("version") != version.split("==", 1)[1]:
        raise RuntimeError("qualification metadata package mismatch")
    if raw.get("wheel_sha256") != wheel_hash:
        raise RuntimeError("qualification metadata artifact mismatch")
    if capability.get("runtime_version") != version:
        raise RuntimeError("qualification capability version mismatch")
    if capability.get("atomic_write_capability") != "unavailable":
        raise RuntimeError("unverified Kùzu capability must remain unavailable")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) for item in evidence):
        raise RuntimeError("qualification evidence is missing")
    for item in evidence:
        if not (BUNDLE / item).is_file():
            raise RuntimeError(f"qualification evidence file is missing: {item}")
    validate_source_guards()
    digest = hashlib.sha256(
        json.dumps({"manifest": manifest, "raw": raw, "capability": capability}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "capability-negative",
        "bundle_digest": digest,
        "runtime_version": version,
        "source_guard": "public mutation DML is absent from the active constructor/facade",
    }


def validate_source_guards() -> None:
    """Ensure the active Kùzu classes contain no executable mutation sink."""
    source_path = ROOT.parent / "opencrab" / "stores" / "kuzu_graph_store.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    active_names = {"KuzuGraphStore", "KuzuUnavailableGraphStore"}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in active_names:
            continue
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
            if attr not in {"execute", "executemany", "run", "commit", "rollback"}:
                continue
            raise RuntimeError(f"active Kùzu class contains a mutation-capable sink: {node.name}.{attr}")


def main() -> int:
    print(json.dumps(validate_bundle(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
