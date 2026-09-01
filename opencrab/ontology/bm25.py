"""
BM25 keyword search index over ontology node properties.

Operates on the doc store (LocalDocStore / MongoStore) — no external deps.
Provides fast, deterministic keyword matching as a complement to vector search.

BM25 parameters:
  k1 = 1.5  (term frequency saturation)
  b  = 0.75 (length normalisation)
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any

from opencrab.ontology.pack_provenance import in_pack_scope, scope_pack_id
from opencrab.ontology.text_cues import tokenize as _tokenize

logger = logging.getLogger(__name__)

# BM25 hyper-parameters
_K1 = 1.5
_B = 0.75

# Properties to include when building the text representation of a node
_TEXT_FIELDS = (
    "name",
    "description",
    "text",
    "title",
    "label",
    "summary",
    "content",
    "reason",
    "rationale",
    "change_reason",
    "revision_reason",
    "applicability",
    "limitation",
    "limitations",
    "risk",
    "law",
    "standard",
    "evidence",
    "source",
    "heading_path",
)
_MAX_PROPERTY_TEXT = 1200


def _flatten_property_text(value: Any, depth: int = 0) -> list[str]:
    """Collect searchable scalar text from nested property values."""
    if value is None or depth > 2:
        return []
    if isinstance(value, str):
        return [value[:_MAX_PROPERTY_TEXT]]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:50]:
            parts.extend(_flatten_property_text(item, depth + 1))
        return parts
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:80]:
            parts.append(str(key).replace("_", " "))
            parts.extend(_flatten_property_text(item, depth + 1))
        return parts
    return [str(value)[:_MAX_PROPERTY_TEXT]]


def _node_text(node: dict[str, Any]) -> str:
    """Build a flat text string from a node document for indexing."""
    props = node.get("properties") or {}
    parts: list[str] = []
    # Include node_id and node_type as searchable terms
    if node.get("node_id"):
        parts.append(str(node["node_id"]).replace("_", " ").replace("-", " "))
    if node.get("node_type"):
        parts.append(str(node["node_type"]))
    for field in _TEXT_FIELDS:
        val = props.get(field)
        if val:
            parts.append(str(val))
    for key, val in props.items():
        if key in _TEXT_FIELDS:
            continue
        parts.append(str(key).replace("_", " "))
        parts.extend(_flatten_property_text(val))
    return " ".join(parts)


class BM25Index:
    """
    In-memory BM25 index built from a list of node documents.

    Usage:
        index = BM25Index.build(doc_store.list_nodes(limit=10000))
        results = index.search("machine learning", pack_ids=["my-pack"], limit=10)
    """

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []       # raw node docs
        self._tokens: list[list[str]] = []           # tokenised docs
        self._df: Counter[str] = Counter()           # document frequency
        self._avgdl: float = 0.0                     # average document length
        self._idf: dict[str, float] = {}             # IDF cache
        # Build-time fingerprint of the source doc set; consumers compare it
        # against the live store fingerprint to decide whether to rebuild.
        self._fingerprint: tuple[int, str] = (0, "")

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        nodes: list[dict[str, Any]],
        fingerprint: tuple[int, str] | None = None,
    ) -> BM25Index:
        """
        Build a BM25 index from a list of node dicts.

        Each dict must have at least 'node_id', 'space', 'node_type'.
        Properties are read from the 'properties' sub-dict.

        fingerprint: optional override for the recorded staleness fingerprint
        (see ``fingerprint`` property). Pass the doc store's whole-table
        ``bm25_fingerprint()`` here so the recorded value means "the store
        looked like this when the index was (re)built", not "these are the
        (possibly capped) N nodes we happened to index" (#63) — the two stop
        being comparable once the corpus exceeds the indexing cap otherwise,
        and every query would schedule a rebuild forever. Defaults to
        ``compute_fingerprint(nodes)`` for callers with no store probe (e.g.
        tests, or one-off CLI builds from an already-fetched node list).
        """
        idx = cls()
        idx._docs = nodes
        idx._tokens = [_tokenize(_node_text(n)) for n in nodes]

        # Document frequency
        for toks in idx._tokens:
            for term in set(toks):
                idx._df[term] += 1

        # Average document length
        total_len = sum(len(t) for t in idx._tokens)
        idx._avgdl = total_len / max(len(idx._tokens), 1)

        # Pre-compute IDF for all known terms
        n = len(nodes)
        for term, df in idx._df.items():
            idx._idf[term] = math.log((n - df + 0.5) / (df + 0.5) + 1)

        idx._fingerprint = fingerprint if fingerprint is not None else compute_fingerprint(nodes)
        logger.debug("BM25Index built: %d nodes, %d unique terms", n, len(idx._idf))
        return idx

    @property
    def fingerprint(self) -> tuple[int, str]:
        return self._fingerprint

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        spaces: list[str] | None = None,
        limit: int = 10,
        *,
        pack_ids: list[str],
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return top-k nodes ranked by BM25 score.

        Parameters
        ----------
        query:
            Search text.
        spaces:
            Optional space filter.
        limit:
            Maximum results.
        pack_ids:
            REQUIRED (#256). The readable pack scope (#147). Docs whose
            pack_id is not in it are skipped, and so are docs with no
            pack_id. An EMPTY list means nothing matches -- it is not "no
            filter" (#147 contract, unchanged). This index is a
            process-wide singleton holding every user's nodes, so this
            filter is the only thing separating them; there is no per-user
            index. There used to be a ``None`` default here, which made an
            unscoped call like ``search(query, limit=10)`` compile fine and
            silently search nothing -- a trap that #256 removes by making
            the parameter keyword-only with no default, so an omitted
            ``pack_ids`` now raises ``TypeError`` at the call site instead
            of returning an empty result.
        include_unpackaged:
            IGNORED (#147). Kept for signature compatibility. Data belonging
            to no pack is outside every read scope (#143 invariant 5), so
            passing True does not surface legacy rows.
        """
        q_tokens = _tokenize(query)
        if not q_tokens or not self._docs:
            return []

        # #147: an empty scope means "nothing is readable", never "no filter".
        # include_unpackaged is accepted for signature compatibility but is
        # not honoured -- rows outside every pack are outside every read
        # scope (#143 invariant 5).
        _pack_set: set[str] = set(pack_ids or ())

        scores: list[tuple[int, float]] = []

        for i, (doc, toks) in enumerate(zip(self._docs, self._tokens)):
            # Space filter
            if spaces and doc.get("space") not in spaces:
                continue
            # Pack filter (#147). Unconditional: the old `if pack_ids and`
            # guard made an EMPTY pack set mean "no filter", so a principal
            # who may read no pack would have matched every document in the
            # index. The index itself is a process-wide singleton holding
            # every user's nodes -- isolation rests entirely on this line.
            if not in_pack_scope(doc, _pack_set):
                continue

            dl = len(toks)
            tf_map = Counter(toks)
            score = 0.0

            for term in q_tokens:
                if term not in self._idf:
                    continue
                tf = tf_map.get(term, 0)
                idf = self._idf[term]
                numerator = tf * (_K1 + 1)
                denominator = tf + _K1 * (1 - _B + _B * dl / max(self._avgdl, 1))
                score += idf * (numerator / denominator)

            if score > 0:
                scores.append((i, score))

        scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in scores[:limit]:
            doc = self._docs[idx]
            # #147: the same strict rule the filter above used. Reporting a
            # pack_id derived by a looser rule than the one that admitted the
            # row would put a value in the response that no access decision
            # was ever made on.
            pid = scope_pack_id(doc)
            results.append({
                "node_id": doc.get("node_id"),
                "space": doc.get("space"),
                "node_type": doc.get("node_type"),
                "score": round(score, 4),
                "properties": doc.get("properties") or {},
                "text": _node_text(doc),
                "pack_id": pid,
            })
        return results

    def __len__(self) -> int:
        return len(self._docs)


def compute_fingerprint(nodes: list[dict[str, Any]]) -> tuple[int, str]:
    """Return ``(doc_count, max_timestamp)`` used for stale-cache detection.

    ``max_timestamp`` falls back to the empty string when nodes lack
    ``updated_at`` / ``ingested_at`` keys; the count alone is still a useful
    signal in that case.
    """
    count = len(nodes)
    latest = ""
    for node in nodes:
        for key in ("updated_at", "ingested_at"):
            value = node.get(key) if isinstance(node, dict) else None
            if value and isinstance(value, str) and value > latest:
                latest = value
    return count, latest
