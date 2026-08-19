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
from typing import Any, Literal

from opencrab.common.pack_tags import canonicalize_pack_alias
from opencrab.pack.write_gate import (
    SOURCE_STAMPED,
    authorize,
    authorize_fork_copy,
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
    origin: Literal["client", "server"] = "client",
    fork_copy: bool = False,
    write_vector: bool = True,
) -> dict[str, Any]:
    """Write one source's doc row and its vector under one ownership stamp.

    Returns a receipt shaped like the builder's: ``{"source_id", "metadata",
    "stores": {"documents": ..., "chromadb": ...}}``. *Store* failures are
    reported in ``stores`` rather than raised, matching the #158 contract that
    callers read the receipt.

    Rejections are different and DO raise: a caller who is not the pack owner,
    a source_id already attributed elsewhere, or a payload that violates the
    ownership-tag invariant. Those happen before anything is written, so the
    caller gets an exception and no partial row -- which is why the alias check
    below runs here rather than being left to ``HybridQuery.ingest``. That
    method raises it too, but it runs AFTER the doc row is committed, and an
    earlier version of this function let exactly that through: a 422 to the
    client with the doc row already persisted.

    ``pack_id`` is keyword-REQUIRED. A source with no pack is outside every
    pack-scoped read (#143 invariant 5), so "no pack" must not be expressible.

    ``origin="server"`` (design v7 §4-C-1) is for the same reason ``add_node``
    has it: a copied source carries the ORIGINAL owner's ``user_id`` and the
    SOURCE pack's ``pack_id`` in its metadata, both of which differ from the
    forker's own values -- ``origin="client"`` (the default) would reject
    that as forged identity. ``pack_fork`` is the only intended caller.

    ``fork_copy`` (design v7 §4-C-2) routes authorization through
    ``write_gate.authorize_fork_copy`` instead of ``authorize``, widening the
    allowed status to a pack's own fork-reserved ``creating`` window -- see
    that function's docstring. Everything else, including the identity guard
    and the stamp, is unchanged.

    ``write_vector`` (design v7 §4-C-3), default True, skips the vector leg
    entirely when False and records ``"skipped (raw copy)"`` under
    ``stores["chromadb"]`` -- NOT ``stores["vector"]``; that key differs from
    ``OntologyBuilder.add_node``'s because this function's receipt already
    used ``chromadb`` for the vector leg before this parameter existed. Fork
    sets this False because ``hybrid.ingest`` re-embeds, which issue #200
    forbids for a fork; the original vector is imported raw elsewhere instead.

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
    if fork_copy:
        authorize_fork_copy(sql, principal, pack_id)
    else:
        authorize(sql, principal, pack_id)

    # A source_id is a global slot on both sinks -- the doc store upserts on
    # source_id with no pack predicate, and every vector store keys on the id
    # alone -- so writing someone else's id silently takes their row.
    reason = source_identity_conflict(
        docs, vector, source_id=source_id, pack_id=pack_id
    )
    if reason:
        raise ValueError(identity_reject_message("source", source_id, reason))

    meta = stamp(
        metadata, principal=principal, pack_id=pack_id, keys=SOURCE_STAMPED,
        origin=origin,
    )
    meta.setdefault("space", _DEFAULT_SPACE)

    # #171 ownership-tag invariant, checked BEFORE the first write. `pack` is
    # not a reserved boundary key, so a client can still send it; leaving the
    # check to the vector leg means the doc row is already committed when it
    # fires.
    canonicalize_pack_alias(meta)

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

    if not write_vector:
        # design v7 §4-C-3: fork imports the original vector raw elsewhere;
        # re-embedding it here would violate #200. Recorded explicitly so
        # `_fork_leg_ok` (opencrab/pack/fork.py) can require exactly this
        # value rather than treating an untouched "unavailable" as
        # equivalent.
        receipt["stores"]["chromadb"] = "skipped (raw copy)"
        return receipt

    # `ingest` mutates the mapping it is handed (it sets `source_id` on it), so
    # the doc row above and the vector below deliberately share one dict: the
    # two must not drift apart.
    try:
        vector_result = hybrid.ingest(text=text, source_id=source_id, metadata=meta)
    except Exception as exc:  # noqa: BLE001 -- store failure, reported (#158)
        receipt["stores"]["chromadb"] = f"error: {exc}"
        return receipt
    receipt["stores"].update(vector_result.get("stores") or {})
    if "vector_id" in vector_result:
        receipt["vector_id"] = vector_result["vector_id"]
    return receipt


def _principal() -> Any:
    from opencrab.auth import current_principal

    return current_principal()
