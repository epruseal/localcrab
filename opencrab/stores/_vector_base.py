"""
Shared vector-store helpers: ID generation, embedding dim-validation, and
add/upsert input-length validation.

EXTRACTED FROM (diffed byte-for-byte before extraction):
    - opencrab/stores/chroma_store.py     (~109-170, add_texts/upsert_texts)
    - opencrab/stores/pg_vector_store.py  (~247-318, _embed/add_texts/upsert_texts)
    - opencrab/stores/sqlite_vec_store.py (~329-436, _embed/add_texts/upsert_texts)

DESIGN CHOICE (plain functions, not a base class): the three stores keep
    unrelated attribute names for the same concepts (``self._ef`` vs
    ``self._embedding_function``, engine vs sqlite3 connection, etc.) and
    have divergent write paths beyond this shared preamble (vec0
    DELETE-then-INSERT vs pgvector native UPSERT vs Chroma's collection API).
    Forcing them under one base class would need either attribute-name
    coupling via a mixin protocol or constructor gymnastics for no benefit —
    these are pure input-transformation/validation steps with no state, so
    plain functions keep each store's adopter change to "call this instead of
    inlining it," which is the mechanical-dedup goal.

CONTRACT — an owned slot may only be rewritten by its owner [#197]:
    All three stores key their vector row/record by ``node_id``, and that key
    is NOT qualified by ``pack_id``. Two packs can therefore produce the same
    ``node_id`` (a shared evidence/chunk id) and land on one slot. Until #197
    the second writer silently won it: vec0's ``upsert_texts`` deleted by
    ``node_id`` with no pack predicate then re-INSERTed the caller's row;
    pgvector's ``ON CONFLICT (node_id) DO UPDATE`` overwrote every column
    including ``pack_id``; Chroma's replaced the record outright. The first
    pack's document and embedding were gone, and a query scoped to that pack
    returned nothing — measured on all three backends.

    ``upsert_texts`` now REFUSES such a write. Per id: no existing row -> pass;
    existing row unowned (``pack_id`` empty, absent, or None) -> pass, because
    backfill and migration take those over on purpose; existing owner equals
    the incoming pack -> pass, the ordinary re-ingest; otherwise -> ValueError,
    the incoming pack being unowned included. One batch that claims two
    different OWNERSHIP STATES for one id is refused as well, before the store
    is read -- unowned is one of those states, so a pack and a no-pack record
    fighting over one id is a conflict just as two packs are. On an empty slot
    every record looks new, and neither write order nor the backend must be
    what decides the owner. The whole batch is judged before any of it is
    applied, so a rejected batch leaves no partial write. See ``slot_owner``,
    ``reject_batch_pack_conflicts`` and ``reject_foreign_slot_writes``.

    ENFORCEMENT IS TWO LAYERS, and they are not redundant. The pre-check above
    judges the batch and produces the caller's error. On top of it the SQL
    backends put the ownership predicate in the write statement itself — vec0
    deletes only its own or an unowned row (a foreign row survives and the
    following INSERT fails on the primary key), pgvector guards its
    ``DO UPDATE`` with a ``WHERE`` on the stored ``pack_id`` and reads
    ``rowcount == 0`` as the violation. That layer closes the window between
    the pre-check's unlocked SELECT and the write, in which another process
    can change the slot's owner; both raise inside a transaction, so the batch
    rolls back. Chroma has no equivalent: no conditional write, no
    transaction, and its cross-process lock is shared and MCP-only (#140), so
    there the pre-check plus the store's in-process lock is the whole of it.
    Cross-process serialisation was never offered at this layer and still is
    not — that stays the caller's ``opencrab/locking.py`` write.lock
    discipline, as ``chroma_store.upsert_texts`` documents at length.

    WHAT THIS DOES NOT COVER. Writes only. ``delete(ids)`` still removes any
    pack's row at a given id; scoping deletion is a separate axis.
    ``add_texts`` is not gated either — its ids are time-salted, and the SQL
    backends already reject a duplicate primary key. A slot that a foreign
    pack took over BEFORE this gate existed is not healed by it; the gate only
    stops new ones. The same applies to one specific legacy shape: a pgvector
    row whose owner column holds the literal ``'None'``, because the pre-gate
    writer ran ``str(meta.get("pack_id", ""))`` over a ``pack_id`` that was
    present and ``None``. That row reads as owned by a pack named ``None``
    here, so re-ingesting the same metadata is refused. sqlite-vec ran the
    same expression but sanitises the metadata first, which folds ``None`` to
    ``""`` before the insert, so the shape does not arise there; chroma has no
    owner column at all.

    This layer does not fold that string to unowned on its own. Not because it
    could not tell the two apart -- the stored ``metadata``'s ``pack_id`` being
    SQL NULL separates a legacy row from a pack genuinely named ``None``, and
    the repair in ``docs/vector-backends.md`` uses exactly that. It is because
    reading that second column on every write would put a one-off legacy shape
    into the permanent contract, and because rewriting stored rows is a data
    change an operator decides. So the gate reads the stored value as it
    stands, and the docs carry the query that finds those rows plus the repair
    for each backend. Metadata-only update paths
    (``opencrab/pack/load.py:_vec_meta_update``) do not bypass any of this:
    they check the existing row's ``pack_id`` before patching the metadata
    column and fall back to ``upsert_texts`` on a mismatch, which now refuses
    rather than rewriting the slot. See ``docs/vector-backends.md`` §8.2.

CONTRACT — ``upsert_texts(texts, metadatas, ids)`` full-replace semantics
    [#175]: for an id that already exists, the store MUST replace the
    document/metadata wholesale, not merge the new metadata into the old.
    Verified per backend:
      - sqlite_vec_store.py: DELETE-then-INSERT per id (vec0 has no native
        UPSERT) — replace by construction.
      - pg_vector_store.py: ``INSERT ... ON CONFLICT (node_id) DO UPDATE SET
        metadata = EXCLUDED.metadata`` (whole-column assignment, not a jsonb
        merge) — replace.
      - chroma_store.py: chromadb's native ``collection.upsert()`` MERGES
        metadata into the existing record (empirically verified against
        chromadb 1.5.7/1.5.9 — ``update()`` and ``upsert()`` both merge; only
        delete-then-add replaces). Prior to #175 this store called
        ``upsert()`` directly, breaking the cross-backend contract — a stale
        key dropped by the caller's canonical metadata transform would
        survive forever. Fixed by delete()-then-add() for exactly the ids
        that would lose a key; where the existing metadata's keys are a
        subset of the new one's (every brand-new id, and every caller that
        passes the full canonical dict) the merge already equals a replace,
        so those go through the single atomic ``upsert()`` — same observable
        contract, no delete window. EXCEPTION [#175 v2]:
        an id that already carries a chromadb uri (never produced by this
        codebase, so always externally written) is routed through native
        ``upsert()`` (merge) instead, because delete()+add() cannot carry the
        uri over without an embedding/document mismatch — see
        chroma_store.py's ``upsert_texts`` docstring for the full argument;
        this matches the fallback ``opencrab/pack/load.py``'s
        ``_vec_meta_update`` already documents for its own uri branch.
    Callers (opencrab/ontology/builder.py, opencrab/ontology/query.py,
    opencrab/pack/load.py) all pass the full canonical metadata dict on every
    upsert_texts call, i.e. they already assume replace semantics — the
    chroma fix makes actual behavior match what callers assumed all along.

CONTRACT -- pack-scoped raw vector export/import [#200]
    ``export_pack_vectors(pack_id)`` / ``import_vectors(records, *, pack_id)``
    let a caller copy a whole pack's vectors WITHOUT re-embedding (the
    consumer is ``pack_fork``, localcrab#201). Three properties make this a
    cross-store contract rather than three coincidences:

    - **ADD semantics, never upsert.** Slot identity is ``node_id`` alone and
      global (see the CONTRACT above), so an upsert-flavoured import would
      aim at a slot another pack may already own. An id that already
      exists therefore RAISES -- unconditionally, which is STRICTER than the
      write gate above: that one lets a pack rewrite its own slot, while an
      import that lands on ANY existing id is a fork with unmapped ids and is
      refused whoever owns it. The exception type is each backend's own --
      sqlite-vec ``OperationalError`` (UNIQUE), pgvector ``IntegrityError``,
      chroma ``ValueError`` from this layer's own pre-check, because chroma's
      ``add()`` on an existing id is a SILENT no-op (measured on 1.5.9) and
      would otherwise be the one fail-open backend. Callers must treat "any
      exception" as "this batch failed", not switch on the type.
    - **The embedding function is never called.** Embeddings are taken
      verbatim from the records, so a fork costs no re-embedding and works
      while the embedding backend is down.
    - **The target pack is declared by the caller and stamped here.**
      ``metadata["pack_id"]`` absent or equal -> assigned; present and
      different -> rejected. This is the same STAMPED rule the write gate
      uses, and it is what makes a forgotten metadata rewrite fail closed
      instead of landing the copies back in the source pack.

    What this contract does NOT do: rewrite any other metadata key. Values
    that name entities in the source pack's id-space -- ``node_id``,
    ``source_id``, ``document_id``, ``source`` -- stay as they are and are the
    caller's job. Enforcing ``metadata["node_id"] == id`` was tried and
    reverted: that equality only holds for ``ontology/builder.py``'s node
    vectors, while chunk vectors legitimately point at their OWNING node
    (``ontology/query.py`` resolves a hit's identity from that key first), so
    enforcing it rejected normal packs. Docs: docs/vector-backends.md.

INTER-COPY FINDINGS:
    - ID generation is IDENTICAL in all three: add-path uses
      ``sha256(f"{text}{time.time_ns()}")[:16]`` (time-salted, so repeated
      identical text never collides); upsert-path uses ``sha256(text)[:16]``
      (content-deterministic, so re-upserting the same text reuses the same
      id). No parameterisation needed.
    - ``_embed``'s dim-validation error message is IDENTICAL between
      pg_vector_store.py and sqlite_vec_store.py:
      ``f"Embedding dim {len(vec)} != table dim {dim}."`` — unified here
      as-is, no per-store message parameter needed.
      chroma_store.py has NO app-side ``_embed``/dim-check at all (Chroma
      embeds internally via its own EmbeddingFunction, so there's nothing to
      validate at this layer) — this helper is simply unused by that store.
    - The texts/metadatas/ids length-mismatch preamble
      (``"texts, metadatas, and ids must have the same length."``) is
      IDENTICAL between pg_vector_store.py and sqlite_vec_store.py.
      chroma_store.py does NOT perform this check (it passes lists straight
      to the chromadb collection API, which does its own validation) — a
      structural difference, not a bug to unify; chroma's adopter is not
      required to call ``validate_lengths``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opencrab.common.pack_tags import LEGACY_PACK_ALIAS_KEY, strip_retired_keys

logger = logging.getLogger(__name__)


def generate_add_ids(texts: list[str]) -> list[str]:
    """Time-salted content hash IDs for the ``add`` path (never collides on
    repeated identical text within the same process)."""
    return [
        hashlib.sha256(f"{t}{time.time_ns()}".encode()).hexdigest()[:16]
        for t in texts
    ]


def generate_upsert_ids(texts: list[str]) -> list[str]:
    """Content-deterministic hash IDs for the ``upsert`` path (re-upserting
    the same text reuses the same id)."""
    return [hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts]


def default_metadatas(
    texts: list[str], metadatas: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """``[{} for _ in texts]`` when metadatas is omitted, else passthrough."""
    if metadatas is None:
        return [{} for _ in texts]
    return metadatas


def validate_lengths(
    texts: list[str], metadatas: list[dict[str, Any]], ids: list[str]
) -> None:
    """Raise ValueError when texts/metadatas/ids lengths disagree.

    NOTE: chroma_store.py does not perform this check — see module docstring.
    """
    if len(ids) != len(texts) or len(metadatas) != len(texts):
        raise ValueError("texts, metadatas, and ids must have the same length.")


def slot_owner(metadata: Mapping[str, Any] | None) -> str:
    """The pack that a metadata dict claims, normalised to a plain ``str``.

    ``""`` means UNOWNED. Three shapes collapse to it: the key is absent, its
    value is ``None``, and its value is the empty string. They are the same
    state -- ``builder.py`` writes ``str(props.get("pack_id") or "")`` for a
    node with no pack, and the SQL stores put that empty string in the
    ``pack_id`` column -- so the gate must not tell them apart.

    Any other falsy value (``0``, ``False``) collapses to unowned as well,
    because of the ``or ""``. That is deliberate and it matches the graph
    side's own ``str(props.get("pack_id") or "")``. It is safe here for one
    reason: this is the single function BOTH the reader and the writer of the
    ownership tag go through, so the two can never disagree about a value.
    Note the ``metadata`` JSON keeps whatever the caller passed, so a caller
    that puts ``0`` there leaves a column and a JSON field that read
    differently -- ``pack_id`` names a pack, and no producer in this codebase
    writes a number into it.
    """
    if not metadata:
        return ""
    return str(metadata.get("pack_id") or "")


def reject_batch_pack_conflicts(
    ids: Sequence[str], metadatas: Sequence[Mapping[str, Any]]
) -> None:
    """Raise when one batch has an id claimed by two different packs [#197].

    This runs BEFORE the store is read, not only before it is written, for two
    reasons. Chroma raises ``DuplicateIDError`` on a duplicate id inside the
    ``get()`` this layer uses to look owners up, so a later check would never
    see the conflict; and an existing-row check cannot see it anyway -- on an
    empty slot both records read back as "no row" and both would pass, leaving
    the backend to pick the winner (measured: sqlite-vec and pgvector keep the
    last write, chroma refuses the batch). Whose slot it becomes is not a
    decision to leave to write order.

    UNOWNED TAKES PART IN THIS COMPARISON. An id claimed once by a pack and
    once with no pack is a conflict too, even though one side names no pack.
    Skipping the unowned side made acceptance depend on record ORDER and on
    the backend: on an empty SQL store ``[unowned, A]`` went through and left A
    owning the slot, while ``[A, unowned]`` reached the write guard and rolled
    the batch back, and chroma refused both orderings inside its own ``get()``.
    Order is exactly what this rule exists to keep out of the decision.

    A duplicate id whose records name the SAME state is NOT rejected here --
    two claims of pack A, or two claims of no pack. That is not a cross-pack
    conflict, and each backend's existing behaviour for it is left exactly as
    it was; see ``docs/vector-backends.md`` for that divergence and the
    follow-up that may unify it.
    """
    owners: dict[str, str] = {}
    for doc_id, metadata in zip(ids, metadatas):
        owner = slot_owner(metadata)
        previous = owners.setdefault(doc_id, owner)
        if previous != owner:
            raise ValueError(
                f"upsert_texts: id {doc_id!r} appears twice in one batch under "
                f"different packs ({previous or '<unowned>'!r} and "
                f"{owner or '<unowned>'!r}); one batch cannot decide which pack "
                "owns a slot -- split it per pack"
            )


def reject_foreign_slot_writes(
    ids: Sequence[str],
    metadatas: Sequence[Mapping[str, Any]],
    existing_owners: Mapping[str, str | None],
) -> None:
    """Raise when a batch would take over a slot another pack already owns [#197].

    THE RULE, in one sentence: an owned slot may only be rewritten by its
    owner. Spelled out per id:

    - no existing row                      -> pass (a brand-new slot)
    - existing row is UNOWNED (``""``)     -> pass (backfill and migration
      take over these on purpose; ``scripts/migrate_pack_ownership.py``'s
      ``_backfill_vector`` writes ONLY rows whose ``pack_id`` is falsy)
    - existing owner == incoming pack      -> pass (a pack re-ingesting itself)
    - existing owner != incoming pack      -> RAISE, the incoming pack being
      unowned included

    WHY IT EXISTS: slot identity here is ``node_id`` alone and is not
    qualified by pack. Without this gate the second writer silently owned the
    whole slot and the first pack's document and embedding were gone -- a
    pack-scoped query for the first pack then returned nothing (measured on
    all three backends, localcrab#197).

    ``existing_owners`` maps id -> the pack that currently holds that slot, for
    the ids that exist; an id absent from the mapping has no row. Each store
    builds it from whatever it already reads: chroma from the ``get()`` its
    rollback snapshot needs anyway (so the gate costs it no extra read), the
    SQL stores from one ``SELECT node_id, pack_id``.

    The whole batch is judged before any of it is applied, so a rejected batch
    never leaves a partial write behind -- the same rule
    ``validate_import_records`` states, and it matters most on chroma, which
    has no transaction to roll back.

    This gate governs WRITES ONLY. ``delete(ids)`` still removes any pack's row
    at a given id; scoping deletion is a separate axis (see
    ``docs/vector-backends.md``).
    """
    conflicts: list[str] = []
    for doc_id, metadata in zip(ids, metadatas):
        current = str(existing_owners.get(doc_id) or "")
        if not current:
            continue
        incoming = slot_owner(metadata)
        if current != incoming:
            conflicts.append(
                f"{doc_id!r} is owned by pack {current!r}, "
                f"write claims {incoming or '<unowned>'!r}"
            )
    if conflicts:
        raise ValueError(
            "upsert_texts: refusing to take over a slot owned by another pack "
            f"({len(conflicts)} of {len(ids)}): " + "; ".join(conflicts[:5])
            + ("; ..." if len(conflicts) > 5 else "")
        )


def embed_and_validate(
    embedding_function: Callable[[list[str]], list[list[float]]],
    dim: int,
    texts: list[str],
) -> list[list[float]]:
    """Embed ``texts`` and raise RuntimeError if any vector's length != dim.

    NOTE: chroma_store.py has no equivalent — it has no app-side embedding
    function/dim to validate against (see module docstring).
    """
    vectors = embedding_function(list(texts))
    for vec in vectors:
        if len(vec) != dim:
            raise RuntimeError(f"Embedding dim {len(vec)} != table dim {dim}.")
    return vectors


# Keys a vector record may carry. ``uris`` is chroma-only (see
# ``validate_import_records``'s ``allow_uris``); everything else is shared.
_RECORD_KEYS = frozenset({"id", "embedding", "document", "metadata", "uris"})


def _float32_representable(value: object) -> bool:
    """True when ``value`` is a real number that stays finite through a float32
    round-trip.

    Out-of-range values do NOT raise on the way in: ``struct.pack("f", 1e40)``
    saturates to ``inf`` silently (measured), and
    ``sqlite_vec.serialize_float32`` is that same call -- so sqlite-vec would
    store ``inf`` for a row chroma and pgvector reject outright. Doing the
    round-trip here is what keeps the three backends on one domain, and it
    puts the boundary exactly where float32 puts it rather than at a
    hand-written constant.

    ``bool`` is excluded deliberately: it is an ``int`` subclass, so
    ``[True, False, ...]`` would otherwise sail through as ``1.0``/``0.0`` and
    store a vector the caller never meant to write.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        round_tripped = struct.unpack("f", struct.pack("f", float(value)))[0]
    except (TypeError, ValueError, OverflowError):
        # On this platform ``struct.pack`` saturates an out-of-range value to
        # inf rather than raising (measured), and the isfinite check below
        # catches that. It only raises where CPython falls back to its generic
        # float packing, so the OverflowError arm is portability cover -- and
        # an overflow means exactly "not representable", the same answer.
        return False
    return math.isfinite(round_tripped)


def validate_import_records(
    records: Any,
    *,
    pack_id: str,
    dim: int | None = None,
    allow_uris: bool = False,
) -> list[dict[str, Any]]:
    """Validate a whole ``import_vectors`` batch and return normalised copies.

    Everything is checked BEFORE any store is touched, so a bad batch cannot
    leave a partial write behind. That matters most on chroma, which has no
    transaction and splits large batches into chunks: without this, a single
    bad record at position 5462 would land the first 5461 and then fail.

    Returns NEW record dicts with NEW metadata dicts -- the caller's input is
    never mutated. Mutating in place would leave the caller's records carrying
    the new ``pack_id`` even for a batch that was rejected.

    The normalisation applied to ``metadata`` is exactly two things:

    - the target ``pack_id`` is stamped (absent or equal -> assign, present and
      different -> ValueError), and
    - retired ownership aliases are dropped (``pack``, localcrab#159/#171).
      Exported legacy rows can still carry one, and re-importing it would
      plant the SOURCE pack's name in the target pack -- recreating the very
      state #171 removed. ``apply_pack_tag``/``canonicalize_pack_alias`` are
      deliberately not used here: the former assigns ``pack_id``
      unconditionally (which would defeat the mismatch rejection above) and
      mutates in place, and the latter raises on a legacy row whose alias
      disagrees, which would block the fork entirely.

    ``dim`` is passed by the stores that know their own (sqlite-vec, pgvector);
    chroma has no app-side dim and passes ``None``. Uniform embedding LENGTH is
    checked either way -- it needs no declared dim, and it is the chroma
    partial-apply axis that matters most.

    Metadata VALUES are not type-checked. ``_sanitize_metadata`` coerces them
    for the stores that need it, and pgvector stores nested JSON as-is, so
    narrowing values to chroma's scalar set here would reject records pgvector
    itself produced -- the same over-rejection the ``node_id`` rule was
    reverted for. Metadata KEYS are checked, because a non-str key raises in
    chroma even after sanitising.
    """
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError(
            f"import_vectors: pack_id must be a non-empty str, got {pack_id!r}"
        )
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise ValueError(
            f"import_vectors: records must be a sequence, got {type(records).__name__}"
        )

    normalised: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    first_len: int | None = None

    for pos, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(
                f"import_vectors: record {pos} must be a dict, "
                f"got {type(record).__name__}"
            )
        unknown = set(record) - _RECORD_KEYS
        if unknown:
            raise ValueError(
                f"import_vectors: record {pos} has unknown key(s) "
                f"{sorted(unknown)}; allowed: {sorted(_RECORD_KEYS)}"
            )
        missing = {"id", "embedding"} - set(record)
        if missing:
            raise ValueError(
                f"import_vectors: record {pos} is missing {sorted(missing)}"
            )

        record_id = record["id"]
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(
                f"import_vectors: record {pos} id must be a non-empty str, "
                f"got {record_id!r}"
            )
        if record_id in seen_ids:
            raise ValueError(
                f"import_vectors: duplicate id {record_id!r} within the batch "
                f"(position {pos})"
            )
        seen_ids.add(record_id)

        document = record.get("document")
        if document is not None and not isinstance(document, str):
            raise ValueError(
                f"import_vectors: record {pos} document must be str or None, "
                f"got {type(document).__name__}"
            )

        uris = record.get("uris")
        if uris is not None:
            if not allow_uris:
                raise ValueError(
                    f"import_vectors: record {pos} carries uris "
                    f"({uris!r}) but this backend cannot store them; "
                    "dropping it silently would lose data"
                )
            if not isinstance(uris, str):
                raise ValueError(
                    f"import_vectors: record {pos} uris must be str or None, "
                    f"got {type(uris).__name__}"
                )

        embedding = record["embedding"]
        if isinstance(embedding, (str, bytes)) or not isinstance(embedding, Sequence):
            raise ValueError(
                f"import_vectors: record {pos} embedding must be a sequence of "
                f"numbers, got {type(embedding).__name__}"
            )
        if not embedding:
            raise ValueError(f"import_vectors: record {pos} embedding is empty")
        if first_len is None:
            first_len = len(embedding)
        elif len(embedding) != first_len:
            raise ValueError(
                f"import_vectors: record {pos} embedding has length "
                f"{len(embedding)} but record 0 has {first_len}; a batch must "
                "be dimensionally uniform"
            )
        if dim is not None and len(embedding) != dim:
            raise ValueError(
                f"import_vectors: record {pos} embedding dim {len(embedding)} "
                f"!= table dim {dim}."
            )
        for index, component in enumerate(embedding):
            if not _float32_representable(component):
                raise ValueError(
                    f"import_vectors: record {pos} embedding[{index}] "
                    f"({component!r}) is not representable as a finite float32"
                )

        metadata = record.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError(
                f"import_vectors: record {pos} metadata must be a dict, "
                f"got {type(metadata).__name__}"
            )
        bad_keys = [key for key in metadata if not isinstance(key, str)]
        if bad_keys:
            raise ValueError(
                f"import_vectors: record {pos} metadata has non-str key(s) "
                f"{bad_keys!r}"
            )
        # "absent or equal -> assign, present and different -> reject". A
        # present ``pack_id`` of None is DIFFERENT, not absent: the key is
        # there and does not name the target pack, so it is rejected like any
        # other mismatch rather than quietly overwritten.
        declared = metadata["pack_id"] if "pack_id" in metadata else pack_id
        if declared != pack_id:
            raise ValueError(
                f"import_vectors: record {pos} metadata pack_id {declared!r} "
                f"disagrees with the declared target pack {pack_id!r}; rewrite "
                "the record's metadata or import into its own pack"
            )
        clean_metadata = strip_retired_keys(metadata)
        dropped = metadata.get(LEGACY_PACK_ALIAS_KEY)
        if dropped is not None and dropped != pack_id:
            logger.warning(
                "import_vectors: dropped retired alias %s=%r on %r (target pack %r)",
                LEGACY_PACK_ALIAS_KEY, dropped, record_id, pack_id,
            )
        clean_metadata["pack_id"] = pack_id

        entry: dict[str, Any] = {
            "id": record_id,
            "embedding": [float(component) for component in embedding],
            "document": document,
            "metadata": clean_metadata,
        }
        if allow_uris:
            entry["uris"] = uris
        normalised.append(entry)

    return normalised
