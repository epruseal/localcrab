#!/usr/bin/env python3
"""Plan or apply the qualified SQL graph-identity migration.

The default action is a read-only dry-run.  This command deliberately knows
only the public graph-store inventory and migration methods; it never opens a
database connection to issue graph-table DML itself.

``--mapping-file`` takes a JSON object whose two list members, ``mappings``
and ``property_resolutions``, are both optional.  A legacy node is addressed
by a nested ``source`` object holding exactly ``node_type`` and ``node_id``.
Under ``mappings`` that source is paired with the digest of the node in a
sibling ``source_digest`` field; a property resolution names the same kind of
source object but carries no digest::

    {
      "mappings": [
        {
          "kind": "rename",
          "source": {"node_type": "Agent", "node_id": "a"},
          "source_digest": "<node digest>",
          "target": {"node_id": "agent-a", "node_type": "Agent"}
        },
        {
          "kind": "merge",
          "sources": [
            {"source": {"node_type": "Agent", "node_id": "b"}, "source_digest": "..."},
            {"source": {"node_type": "Person", "node_id": "b"}, "source_digest": "..."}
          ],
          "target": {"node_id": "b", "node_type": "Person"}
        }
      ],
      "property_resolutions": [
        {
          "source": {"node_type": "Agent", "node_id": "b"},
          "source_property": "name",
          "source_value": "Ada",
          "target_property": "alias"
        }
      ]
    }

A target additionally accepts optional ``space_id`` and ``pack_id``, and a
merge needs at least two sources; the store rejects a shorter one.  Node
digests come from a prior dry-run receipt or from the graph inventory, and the
store rejects a mapping whose digest no longer matches the stored row.  Read
the digests out of a receipt rather than pasting its mappings in: a receipt
reports each source flattened to ``node_type``, ``node_id`` and ``digest``,
which is not the shape this file accepts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    ApplyMigrationRequest,
    DryRunMigrationRequest,
    ExplicitMerge,
    ExplicitRename,
    FrozenDict,
    LegacyNodeKey,
    PropertyResolution,
    canonical_json_bytes,
    plan_sha256,
    thaw_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and migrate a SQL graph to global node identity."
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite graph database (default: LOCAL_DATA_DIR/graph.db).",
    )
    parser.add_argument(
        "--backend", choices=("local", "pg"), default="local",
        help="Qualified SQL backend to use.",
    )
    parser.add_argument("--pg-url", default=os.environ.get("POSTGRES_URL"))
    parser.add_argument("--pg-schema", default="public")
    parser.add_argument(
        "--mapping-file",
        type=Path,
        help="JSON file containing mappings and property_resolutions for dry-run.",
    )
    parser.add_argument("--source-fingerprint", default=None)
    parser.add_argument(
        "--plan-out", type=Path,
        help="Write exact canonical dry-run plan bytes to this file.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply an exact saved plan.")
    parser.add_argument("--request-id", help="Unique apply request ID.")
    parser.add_argument("--plan-file", type=Path, help="Exact canonical plan bytes for apply.")
    parser.add_argument("--plan-sha256", default=None)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--backup-sha256", default=None)
    parser.add_argument("--receipt-out", type=Path)
    return parser


def _node_key(value: Any) -> LegacyNodeKey:
    if not isinstance(value, dict) or set(value) != {"node_type", "node_id"}:
        raise ValueError("source must contain exactly node_type and node_id")
    return LegacyNodeKey(value["node_type"], value["node_id"])


def _source_digest(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("source_digest must be a non-empty string")
    return value


def _source_entry(value: Any) -> tuple[LegacyNodeKey, str]:
    if not isinstance(value, dict) or set(value) != {"source", "source_digest"}:
        raise ValueError("merge source must contain exactly source and source_digest")
    return _node_key(value["source"]), _source_digest(value["source_digest"])


def _items(value: dict[str, Any], key: str) -> list[Any]:
    items = value.get(key, [])
    if not isinstance(items, list):
        raise ValueError(f"{key} must be a list")
    return items


def _target(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("mapping target must be an object")
    allowed = {"node_id", "node_type", "space_id", "pack_id"}
    if set(value) - allowed or not {"node_id", "node_type"}.issubset(value):
        raise ValueError("mapping target has unknown or missing fields")
    return {
        "node_id": value["node_id"],
        "node_type": value["node_type"],
        "space_id": value.get("space_id"),
        "pack_id": value.get("pack_id"),
    }


def _mapping_file(path: Path) -> tuple[tuple[ExplicitRename | ExplicitMerge, ...], tuple[PropertyResolution, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("mapping file must contain an object")
    mappings: list[ExplicitRename | ExplicitMerge] = []
    for item in _items(value, "mappings"):
        if not isinstance(item, dict):
            raise ValueError("mapping must be an object")
        target = _target(item.get("target"))
        kind = item.get("kind")
        if kind == "rename":
            mappings.append(ExplicitRename(
                _node_key(item.get("source")), _source_digest(item.get("source_digest")),
                target["node_id"], target["node_type"], target["space_id"], target["pack_id"],
            ))
        elif kind == "merge":
            sources = tuple(_source_entry(source) for source in _items(item, "sources"))
            mappings.append(ExplicitMerge(
                sources, target["node_id"], target["node_type"],
                target["space_id"], target["pack_id"],
            ))
        else:
            raise ValueError("mapping kind must be rename or merge")
    resolutions: list[PropertyResolution] = []
    for item in _items(value, "property_resolutions"):
        if not isinstance(item, dict) or set(item) != {"source", "source_property", "source_value", "target_property"}:
            raise ValueError("property resolution fields are invalid")
        resolutions.append(PropertyResolution(
            _node_key(item["source"]), item["source_property"],
            item["source_value"], item["target_property"],
        ))
    return tuple(mappings), tuple(resolutions)


def _store(args: argparse.Namespace) -> Any:
    if args.backend == "local":
        from opencrab.stores.local_graph_store import LocalGraphStore

        data_dir = Path(os.environ.get("LOCAL_DATA_DIR", "./opencrab_data"))
        db_path = Path(args.db_path) if args.db_path else data_dir / "graph.db"
        if not db_path.is_file():
            raise FileNotFoundError(db_path)
        return LocalGraphStore(str(db_path))
    if not args.pg_url:
        raise ValueError("--pg-url or POSTGRES_URL is required for --backend pg")
    from sqlalchemy import create_engine

    from opencrab.stores.pg_graph_store import PGGraphStore

    return PGGraphStore(create_engine(args.pg_url), schema=args.pg_schema)


def _json_value(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return thaw_json(value)
    return thaw_json(value)


def _receipt_json(receipt: Any) -> bytes:
    value = {
        key: _json_value(getattr(receipt, key))
        for key in (
            "request_id", "phase", "request_digest", "source_fingerprint",
            "mapping_fingerprint", "plan_sha256", "target_fingerprint_before",
            "target_fingerprint_after", "mapping_result", "edge_loss", "property_loss",
            "receipt_sha256",
        )
        if hasattr(receipt, key)
    }
    return canonical_json_bytes(value)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = _store(args)
    try:
        if args.apply:
            required = {
                "--request-id": args.request_id,
                "--source-fingerprint": args.source_fingerprint,
                "--plan-file": args.plan_file,
                "--backup-path": args.backup_path,
                "--backup-sha256": args.backup_sha256,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError("apply requires " + ", ".join(missing))
            plan_bytes = args.plan_file.read_bytes()
            supplied_plan_sha = args.plan_sha256 or plan_sha256(plan_bytes)
            receipt = store.migrate_graph_identity(ApplyMigrationRequest(
                args.request_id, args.source_fingerprint, plan_bytes,
                supplied_plan_sha, args.backup_path, args.backup_sha256,
            ))
        else:
            inventory = store.inspect_graph_identity()
            source_fingerprint = args.source_fingerprint or inventory.source_fingerprint
            mappings, resolutions = _mapping_file(args.mapping_file) if args.mapping_file else ((), ())
            receipt = store.migrate_graph_identity(DryRunMigrationRequest(
                source_fingerprint, mappings, resolutions,
            ))
            if args.plan_out:
                args.plan_out.write_bytes(receipt.plan_bytes)
        if args.receipt_out:
            args.receipt_out.write_bytes(_receipt_json(receipt))
        print(_receipt_json(receipt).decode("utf-8"))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
