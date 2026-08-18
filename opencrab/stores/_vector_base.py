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

CONTRACT — slot identity is node_id alone (last-writer-wins across packs):
    All three stores key their vector row/record by ``node_id`` — none of
    ``add_texts``/``upsert_texts`` qualifies that key by ``pack_id``. When two
    packs produce the same ``node_id`` (shared evidence/chunk id), the second
    writer's ``upsert_texts`` silently takes over the slot: vec0's
    ``upsert_texts`` does ``DELETE FROM {table} WHERE node_id = ?`` with no
    pack predicate (any pack's row at that id is deleted) then re-INSERTs the
    caller's pack/document/embedding/metadata; pgvector's ``upsert_texts`` does
    ``INSERT ... ON CONFLICT (node_id) DO UPDATE SET pack_id = EXCLUDED.pack_id,
    embedding = EXCLUDED.embedding, document = EXCLUDED.document, metadata =
    EXCLUDED.metadata`` (every column overwritten); Chroma's ``upsert_texts``
    deletes the existing record and re-adds it (see the full-replace contract
    below), so it too lands the whole slot on the caller's values.

    The upshot: going through ``upsert_texts`` never leaves a **partially**
    contaminated row (mixed pack A/B fields) — whichever caller writes last
    owns the whole slot. What it does NOT provide is pack-qualified identity:
    nothing stops a second pack from taking over a slot a first pack already
    owns, because there is no per-pack namespacing of ``node_id`` at this
    layer. Metadata-only update paths (``opencrab/pack/load.py:_vec_meta_update``)
    must not bypass this — they check the existing row's ``pack_id`` before
    patching only the metadata column/field, and fall back to
    ``upsert_texts`` (full-slot rewrite) on any mismatch, so the only surface
    that could otherwise partially contaminate a foreign pack's row (edit
    metadata in place while document/embedding/pack_id stay foreign) is
    closed. See ``_vec_meta_update``'s docstring for the exact reasoning and
    the open follow-up (pack-qualified slot identity, localcrab#172/#182).

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
      silently take over a slot another pack already owns. An id that already
      exists therefore RAISES. The exception type is each backend's own --
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
import time
from collections.abc import Callable, Sequence
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
_METADATA_VALUE_TYPES = (str, int, float, bool)

# float32's finite range. Values outside it do NOT raise on the way in --
# ``struct.pack("f", 1e40)`` saturates to ``inf`` silently (measured), and
# ``sqlite_vec.serialize_float32`` is that same call -- so sqlite-vec would
# store ``inf`` where chroma and pgvector reject the row outright. Checking
# representability here is what keeps the three backends on one domain.
_FLOAT32_MAX = 3.4028235e38


def _float32_representable(value: object) -> bool:
    """True when ``value`` is a real number that survives a float32 round-trip
    as a finite value (rejects NaN, +/-inf, and anything that saturates)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        as_float = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return False
    return -_FLOAT32_MAX <= as_float <= _FLOAT32_MAX


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
        declared = metadata.get("pack_id")
        if declared is not None and declared != pack_id:
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
