"""The source-text writer: the second of the two write chokepoints (#148).

``OntologyBuilder`` covers graph node/edge writes. Source text does NOT go
through it -- ``HybridQuery.ingest`` writes only the vector (its own comment
says so: "Doc-store mutations happen outside ingest() today"), and the
``doc_sources`` row is a separate ``docs.upsert_source`` call at every site
that ingests text: REST ``/api/ingest``, the CLI, ``pack_ingest``'s
``text_as_node=False`` path, and the pack loader.

Four sites, two calls each, and the ownership stamp landing on only one of
them is how the free-tier quota came to depend on a key (``metadata.user_id``)
that no server-side code was responsible for setting. This module makes the
pair one call with one stamp.

Order matters: the doc row is written FIRST. An earlier draft wrote the vector
first and skipped the doc row when the vector failed, which silently turned
ingest into a no-op on any deployment without a vector store -- ``ingest()``
returns ``{"chromadb": "unavailable"}`` there rather than raising. The doc
store is the system of record for a source, so it leads, exactly as the graph
store leads in the builder.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opencrab.pack.write_gate import (
    SOURCE_STAMPED,
    authorize,
    identity_reject_message,
    source_identity_conflict,
    stamp,
)

# Sources carry no space of their own, and the FTS space filter drops anything
# without one. Only the pack ingest path used to fill this in, so REST-ingested
# text was invisible to a space-filtered query (#52/#110). Filled here so every
# source path gets it.
_DEFAULT_SPACE = "evidence"


def _docs_available(docs: Any) -> bool:
    return bool(getattr(docs, "available", False))


def write_source(
    sql: Any,
    hybrid: Any,
    docs: Any,
    vector: Any,
    *,
    text: str,
    source_id: str,
    metadata: Mapping[str, Any] | None = None,
    pack_id: str,
) -> dict[str, Any]:
    """Write one source's doc row and its vector under one ownership stamp.

    Returns a receipt shaped like the builder's: ``{"source_id", "metadata",
    "stores": {"documents": ..., "chromadb": ...}}``. Per-store failures are
    reported in ``stores`` rather than raised, matching the #158 contract that
    callers read the receipt.

    ``pack_id`` is keyword-REQUIRED. A source with no pack is outside every
    pack-scoped read (#143 invariant 5), so "no pack" must not be expressible.

    A doc-store *error* stops the vector write: a vector row pointing at a
    source that failed to record is an orphan no read path can hydrate. A doc
    store that is merely *unavailable* does not -- that is a deployment shape,
    not a failure, and refusing there would break vector-only deployments the
    same way the earlier vector-first ordering broke doc-only ones.
    """
    principal = _principal()

    # Owner-only (#143 invariant 4). `sql` is a REQUIRED positional, not an
    # optional one: an authorize that runs "when the caller happens to pass a
    # store" is fail-open, which is the pattern this work already had to walk
    # back once (see pack/load.py's _require_bound_principal).
    authorize(sql, principal, pack_id)

    # A source_id is a global slot on both sinks -- the doc store upserts on
    # source_id with no pack predicate, and every vector store keys on the id
    # alone -- so writing someone else's id silently takes their row.
    reason = source_identity_conflict(
        docs, vector, source_id=source_id, pack_id=pack_id
    )
    if reason:
        raise ValueError(identity_reject_message("source", source_id, reason))

    meta = stamp(metadata, principal=principal, pack_id=pack_id, keys=SOURCE_STAMPED)
    meta.setdefault("space", _DEFAULT_SPACE)

    receipt: dict[str, Any] = {"source_id": source_id, "metadata": meta, "stores": {}}

    doc_failed = False
    if _docs_available(docs):
        try:
            created = docs.upsert_source(source_id, text, meta)
            receipt["stores"]["documents"] = f"ok (id={created or source_id})"
        except Exception as exc:  # noqa: BLE001 -- reported, not raised (#158)
            receipt["stores"]["documents"] = f"error: {exc}"
            doc_failed = True
    else:
        receipt["stores"]["documents"] = "unavailable"

    if doc_failed:
        receipt["stores"]["chromadb"] = "skipped (source record failed)"
        return receipt

    # `ingest` mutates the mapping it is handed (it sets `source_id` on it), so
    # the doc row above and the vector below deliberately share one dict: the
    # two must not drift apart.
    vector_result = hybrid.ingest(text=text, source_id=source_id, metadata=meta)
    receipt["stores"].update(vector_result.get("stores") or {})
    if "vector_id" in vector_result:
        receipt["vector_id"] = vector_result["vector_id"]
    return receipt


def _principal() -> Any:
    from opencrab.auth import current_principal

    return current_principal()
