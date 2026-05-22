#!/usr/bin/env python3
"""Backfill top-level owner_id / user_id columns on MongoDB collections.

Counters in `apps/api/main.py` query an OR across top-level (`owner_id` /
`user_id`) and the legacy nested paths (`properties.owner_id` /
`metadata.user_id`). Docs written before the mirror landed only have the
nested fields. This script copies the nested value up, and optionally tags
docs that have neither with a default owner.

Defaults to dry-run. Pass --apply to actually write.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from opencrab.config import get_settings
from opencrab.stores.factory import make_doc_store


def _patch_collection(
    db: Any,
    *,
    collection: str,
    top_field: str,
    nested_path: str,
    default_owner: str | None,
    apply: bool,
) -> dict[str, int]:
    coll = db[collection]
    total = coll.estimated_document_count()
    needs_mirror_q = {top_field: {"$exists": False}, nested_path: {"$exists": True}}
    needs_default_q = {top_field: {"$exists": False}, nested_path: {"$exists": False}}
    mirror_candidates = coll.count_documents(needs_mirror_q)
    default_candidates = coll.count_documents(needs_default_q)

    print(
        f"  {collection}: total={total}  "
        f"missing-top-with-nested={mirror_candidates}  "
        f"missing-both={default_candidates}",
        flush=True,
    )

    mirrored = 0
    defaulted = 0

    if apply:
        # 1) Mirror nested → top-level. Mongo supports referring to another
        # field via $set + aggregation pipeline (server >= 4.2).
        if mirror_candidates:
            pipeline = [
                {
                    "$set": {
                        top_field: f"${nested_path}",
                    }
                }
            ]
            result = coll.update_many(needs_mirror_q, pipeline)
            mirrored = result.modified_count

        # 2) Default-tag docs with neither value.
        if default_candidates and default_owner is not None:
            nested_root, _, nested_leaf = nested_path.partition(".")
            update: dict[str, Any] = {"$set": {top_field: default_owner}}
            if nested_root and nested_leaf:
                update["$set"][nested_path] = default_owner
            result = coll.update_many(needs_default_q, update)
            defaulted = result.modified_count

    return {
        "total": total,
        "mirror_candidates": mirror_candidates,
        "default_candidates": default_candidates,
        "mirrored": mirrored,
        "defaulted": defaulted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes. Without this flag the script is dry-run.",
    )
    parser.add_argument(
        "--default-owner",
        default=None,
        help="Owner id to assign to docs that have neither top-level nor nested owner. "
             "If omitted, such docs are reported but not touched.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    docs = make_doc_store(settings)

    if not getattr(docs, "available", False) or not hasattr(docs, "_db"):
        print("ERROR: doc store is unavailable or not Mongo-backed.", file=sys.stderr)
        return 2

    db = docs._db
    print(f"Mode: {'APPLY (writes)' if args.apply else 'DRY-RUN'}")
    print(f"Default owner: {args.default_owner or '<not set>'}")
    print("Collections:")

    nodes_stats = _patch_collection(
        db,
        collection="nodes",
        top_field="owner_id",
        nested_path="properties.owner_id",
        default_owner=args.default_owner,
        apply=args.apply,
    )
    sources_stats = _patch_collection(
        db,
        collection="sources",
        top_field="user_id",
        nested_path="metadata.user_id",
        default_owner=args.default_owner,
        apply=args.apply,
    )

    print()
    print("Summary:")
    print(f"  nodes:   {nodes_stats}")
    print(f"  sources: {sources_stats}")
    if not args.apply:
        print()
        print("Dry-run. Re-run with --apply to perform writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
