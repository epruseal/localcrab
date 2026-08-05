"""
Hybrid Query Engine.

Combines vector similarity search (ChromaDB) with graph traversal
(Neo4j) to answer natural language questions about the ontology.

Query pipeline:
  1. Embed the question and perform a vector similarity search in ChromaDB.
  2. Extract node IDs from the top vector hits.
  3. Use those IDs as anchors for a graph neighbourhood expansion.
  4. Merge, deduplicate, and rank results.
  5. Return a unified result list.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from opencrab.ontology.pack_provenance import infer_pack_id
from opencrab.ontology.text_cues import QUERY_MULTIHOP_CUES as _MULTIHOP_QUERY_CUES
from opencrab.ontology.text_cues import RELATION_CUES as _RELATION_QUERY_CUES
from opencrab.stores.chroma_store import ChromaStore
from opencrab.stores.neo4j_store import Neo4jStore

logger = logging.getLogger(__name__)

# Edge-type weights for graph expansion scoring
_EDGE_WEIGHTS: dict[str, float] = {
    "SUPPORTS": 0.7,
    "DEPENDS_ON": 0.7,
    "RELATED_TO": 0.6,
    "CONTAINS": 0.65,
    "INFLUENCES": 0.65,
    "CONTRADICTS": 0.5,
}
_DEFAULT_EDGE_SCORE: float = 0.5
_BM25_NODE_LIMIT = int(os.getenv("OPENCRAB_BM25_NODE_LIMIT", "50000"))

# Lazily imported Phase 4 modules to avoid circular deps at module load
_BM25Index: Any = None
_Reranker: Any = None


def _get_bm25():
    global _BM25Index
    if _BM25Index is None:
        from opencrab.ontology.bm25 import BM25Index
        _BM25Index = BM25Index
    return _BM25Index


def _get_reranker():
    global _Reranker
    if _Reranker is None:
        from opencrab.ontology.reranker import Reranker
        _Reranker = Reranker
    return _Reranker


@dataclass
class QueryResult:
    """A single result item from a hybrid query."""

    source: str          # "vector", "graph", or "hybrid"
    node_id: str | None
    score: float
    text: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    graph_context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "node_id": self.node_id,
            "score": self.score,
            "text": self.text,
            "metadata": self.metadata,
            "graph_context": self.graph_context,
        }


@dataclass
class QueryOutcome:
    """Return value of :meth:`HybridQuery.query`.

    #51: warnings must travel in the return value, not instance state —
    ``HybridQuery`` is a process-lifetime singleton (see ``_get_context()``)
    served from a threadpool (sync MCP/HTTP handlers), so an instance
    attribute like the old ``self._last_warnings`` is shared and clobbered
    across concurrent requests (same defect class as ``PackSelection``
    in pack_selection.py was built to avoid — a local list returned per call).

    Delegates ``__iter__``/``__len__``/``__getitem__`` to ``results`` so
    existing call sites that treat the return value as ``list[QueryResult]``
    (``for r in results``, ``len(results)``, ``results[0]``) keep working
    unchanged; only code that wants the transitional warnings needs to know
    about this type.
    """

    results: list[QueryResult]
    warnings: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, idx):
        return self.results[idx]


@dataclass(frozen=True)
class _QueryProfile:
    """Adaptive retrieval settings for the current question."""

    vector_limit: int
    bm25_limit: int
    graph_limit: int
    graph_depth: int
    anchor_limit: int
    rerank_limit: int


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in cues)


def _profile_for_query(question: str, limit: int, graph_depth: int) -> _QueryProfile:
    """Use higher recall for relationship and multi-hop questions."""
    relation_intent = _contains_any(question, _RELATION_QUERY_CUES)
    multihop_intent = _contains_any(question, _MULTIHOP_QUERY_CUES)
    depth = graph_depth
    if relation_intent:
        depth = max(depth, 2)
    if multihop_intent:
        depth = max(depth, 3)

    multiplier = 8 if relation_intent or multihop_intent else 4
    return _QueryProfile(
        vector_limit=min(max(limit * multiplier, 24), 80),
        bm25_limit=min(max(limit * (multiplier + 2), 40), 180),
        graph_limit=min(max(limit * (multiplier + 2), 50), 220),
        graph_depth=min(depth, 3),
        anchor_limit=12 if relation_intent or multihop_intent else 6,
        rerank_limit=min(max(limit * 4, 20), 80),
    )


def _ordered_unique(values: list[str | None], limit: int) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def _build_chroma_where(
    spaces: list[str] | None = None,
    pack_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Compose a ChromaDB ``where`` clause from spaces and pack_ids filters.

    Single-condition queries return a flat ``{field: value-or-$in}`` dict to
    stay compatible with older Chroma versions; multi-condition queries
    return ``{"$and": [...]}``. When both inputs are empty/None, returns
    ``None`` (caller skips the where clause).
    """
    clauses: list[dict[str, Any]] = []
    if spaces:
        if len(spaces) == 1:
            clauses.append({"space": spaces[0]})
        else:
            clauses.append({"space": {"$in": list(spaces)}})
    if pack_ids:
        if len(pack_ids) == 1:
            clauses.append({"pack_id": pack_ids[0]})
        else:
            clauses.append({"pack_id": {"$in": list(pack_ids)}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _property_text(props: dict[str, Any], relation_type: str = "") -> str:
    parts = [relation_type.replace("_", " ")]
    for key in (
        "text",
        "name",
        "title",
        "label",
        "summary",
        "description",
        "reason",
        "rationale",
        "change_reason",
        "revision_reason",
        "evidence",
        "source",
        "heading_path",
    ):
        value = props.get(key)
        if value:
            parts.append(str(value))
    if len(parts) <= 1:
        parts.append(str(props))
    return " ".join(parts)[:4000]


class _Bm25CacheWorker:
    """Debounced background rebuild worker for a BM25 index cache.

    Owns the cache slot, staleness flag, and worker-thread lifecycle that
    :class:`HybridQuery` exposes on itself (``_bm25_cache``, ``_bm25_dirty``,
    etc.) — the query hot path stays rebuild-free: rebuilds run on a
    background thread, debounced and coalesced via a generation counter, and
    re-run if a new invalidation arrives mid-build so the final index always
    reflects the last write. ``doc_store_getter`` is called at rebuild time
    (not cached) since the doc store may be attached to the owning
    ``HybridQuery`` after construction (see ``_get_context()`` in tools.py).
    """

    def __init__(self, doc_store_getter: Any, debounce: float) -> None:
        self._doc_store_getter = doc_store_getter
        self.cache: Any = None
        self.cache_size: int = 0
        self.dirty: bool = True
        self.debounce = debounce
        self._lock = threading.Lock()      # guards scheduling state
        self._wake = threading.Event()      # invalidate → wake the worker
        self._stop = threading.Event()      # shutdown signal
        self._epoch = 0                      # generation counter (coalescing)
        self.thread: threading.Thread | None = None

    def invalidate(self) -> None:
        """Mark the index stale and schedule a debounced background rebuild.

        Cheap and non-blocking: called from inside write handlers (holding the
        process write lock), it only bumps a generation counter and wakes the
        worker. Inert instances (no doc store attached, e.g. the FastAPI
        ``ApiContext.hybrid``) never spawn a thread.
        """
        self.dirty = True
        if self._doc_store_getter() is None:
            return
        with self._lock:
            self._epoch += 1
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(
                    target=self._rebuild_loop,
                    name="bm25-rebuild",
                    daemon=True,
                )
                self.thread.start()
            self._wake.set()

    def _rebuild_loop(self) -> None:
        """Background worker: debounce, then rebuild the BM25 index off-path.

        Coalesces bursts of invalidations via ``debounce`` + a generation
        counter, and re-runs if a new invalidation arrived during a build so the
        final index always reflects the last write.
        """
        BM25Index = _get_bm25()  # noqa: N806
        from opencrab.ontology.bm25 import compute_fingerprint

        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            self._wake.clear()
            # Debounce: collapse a burst of invalidations into one rebuild.
            self._stop.wait(self.debounce)
            if self._stop.is_set():
                break
            build_epoch = self._epoch
            try:
                ds = self._doc_store_getter()
                # Fingerprint FIRST, nodes SECOND (#63 follow-up): a write
                # landing between the two calls must make the recorded
                # fingerprint OLDER than the nodes we index, not newer. Newer
                # (the reversed order) would bake that write's effect into
                # the nodes while stamping a fingerprint that already
                # matches the post-write store state — the next probe would
                # then agree "nothing changed" and the write is never
                # reflected. Older is safe: the next probe sees a mismatch
                # and schedules one extra (harmless) rebuild.
                probe = getattr(ds, "bm25_fingerprint", None)
                fp = probe(limit=_BM25_NODE_LIMIT) if probe is not None else None
                nodes = ds.list_nodes(limit=_BM25_NODE_LIMIT)
                if fp is None:
                    fp = compute_fingerprint(nodes)
                cache = self.cache
                if cache is not None and fp == cache.fingerprint:
                    self.dirty = False          # nothing actually changed
                else:
                    new_index = BM25Index.build(nodes, fingerprint=fp)
                    self.cache = new_index        # atomic ref swap (GIL)
                    self.cache_size = len(nodes)
                    self.dirty = False
                    logger.debug("BM25 index rebuilt in background (%d nodes)", len(nodes))
            except Exception as exc:  # keep serving the old cache on failure
                logger.warning("BM25 background rebuild failed: %s", exc)
            # A write landed mid-build → schedule one more pass.
            if self._epoch != build_epoch:
                self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for any in-flight background rebuild to settle (tests/shutdown)."""
        thread = self.thread
        if thread is not None and thread.is_alive():
            # Give the worker a chance to pick up a pending wake + debounce.
            thread.join(timeout)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop the background worker (called from server shutdown hooks)."""
        self._stop.set()
        self._wake.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)


class HybridQuery:
    """Orchestrates hybrid vector + graph queries."""

    def __init__(self, chroma: ChromaStore, neo4j: Neo4jStore) -> None:
        self._chroma = chroma
        self._neo4j = neo4j
        # Optional stores attached at runtime by _get_context() in tools.py
        self._doc_store: Any = None
        self._rebac: Any = None
        # BM25 index cache — rebuilt off the query hot path by a background
        # worker thread (debounced). Queries serve the (possibly slightly
        # stale) cached index immediately; a write or an out-of-band
        # fingerprint mismatch schedules a rebuild that atomically swaps it in.
        debounce = float(os.getenv("OPENCRAB_BM25_DEBOUNCE", "1.5"))
        self._bm25 = _Bm25CacheWorker(lambda: self._doc_store, debounce)

    # -- BM25 cache/worker state, forwarded to the extracted worker so the
    #    public/private surface (and tests pinning it) stays unchanged. --

    @property
    def _bm25_cache(self) -> Any:
        return self._bm25.cache

    @_bm25_cache.setter
    def _bm25_cache(self, value: Any) -> None:
        self._bm25.cache = value

    @property
    def _bm25_cache_size(self) -> int:
        return self._bm25.cache_size

    @_bm25_cache_size.setter
    def _bm25_cache_size(self, value: int) -> None:
        self._bm25.cache_size = value

    @property
    def _bm25_dirty(self) -> bool:
        return self._bm25.dirty

    @_bm25_dirty.setter
    def _bm25_dirty(self, value: bool) -> None:
        self._bm25.dirty = value

    @property
    def _bm25_debounce(self) -> float:
        return self._bm25.debounce

    @_bm25_debounce.setter
    def _bm25_debounce(self, value: float) -> None:
        self._bm25.debounce = value

    @property
    def _bm25_worker(self) -> threading.Thread | None:
        return self._bm25.thread

    def invalidate_bm25_cache(self) -> None:
        """Mark the BM25 index stale and schedule a debounced background rebuild."""
        self._bm25.invalidate()

    def _bm25_join(self, timeout: float | None = None) -> None:
        """Wait for any in-flight background rebuild to settle (tests/shutdown)."""
        self._bm25.join(timeout)

    def shutdown_bm25(self, timeout: float = 2.0) -> None:
        """Stop the background worker (called from server shutdown hooks)."""
        self._bm25.shutdown(timeout)

    def query(
        self,
        question: str,
        spaces: list[str] | None = None,
        limit: int = 10,
        graph_depth: int = 1,
        subject_id: str | None = None,
        use_bm25: bool = True,
        use_rerank: bool = True,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        use_fts: bool = True,
    ) -> QueryOutcome:
        """
        Execute a hybrid query: vector + BM25 + graph expansion, then rerank.

        Parameters
        ----------
        question:
            Natural language question or search text.
        spaces:
            Optional list of space IDs to filter results.
        limit:
            Maximum number of results to return.
        graph_depth:
            Neighbourhood expansion depth from vector-hit anchors.
        subject_id:
            If set, policy-aware filter: removes nodes the subject cannot view.
            Requires self._rebac to be attached (done by _get_context in tools.py).
        use_bm25:
            Include BM25 keyword results in the merge (default True).
        use_rerank:
            Apply RRF + BM25 cross-score reranking (default True).

        Returns
        -------
        QueryOutcome — ``.results`` (list[QueryResult], descending score) plus
        ``.warnings`` (list[str]). Acts like the old bare list[QueryResult] for
        iteration/len/indexing (see QueryOutcome docstring), so existing callers
        that only care about results are unaffected.
        """
        result_lists: list[list[dict[str, Any]]] = []
        profile = _profile_for_query(question, limit, graph_depth)

        # #51: 벡터 store 는 "space" 키가 없는 메타데이터를 매치 실패로 처리한다(무시가
        # 아님 — Chroma missing-key 시맨틱의 의도적 복제, sqlite_vec_store.py 참조).
        # builder.py 는 이 픽스 이후 신규 벡터에만 space 를 기록하므로, 백필 전까지는
        # 기존 벡터가 space 필터에서 조용히 빠진다. "0건"과 "필터 미적용"을 호출자가
        # 구분할 수 있도록 지역 변수에 담아 반환값(QueryOutcome)으로 전달한다 — 인스턴스
        # 상태(self.*)는 프로세스 수명 싱글턴 + 스레드풀 실행 환경에서 요청 간 경합이
        # 생기므로 쓰지 않는다(BM25/FTS 레그는 영향 없음).
        warnings: list[str] = []
        if spaces:
            warnings.append(
                "spaces filter: vectors ingested before this fix carry no 'space' "
                "metadata and are excluded from the vector search leg until a "
                "backfill runs (see issue #51); BM25/FTS legs are unaffected."
            )

        # --- Stage 1: Vector similarity search ---
        vector_hits = self._vector_search(
            question, spaces, profile.vector_limit,
            pack_ids=pack_ids, include_unpackaged=include_unpackaged,
        )
        if vector_hits:
            result_lists.append([r.to_dict() for r in vector_hits])

        # --- Stage 2: BM25 keyword search ---
        bm25_hits: list[dict[str, Any]] = []
        if use_bm25 and self._doc_store is not None:
            bm25_hits = self._bm25_search(
                question, spaces, profile.bm25_limit,
                pack_ids=pack_ids, include_unpackaged=include_unpackaged,
            )
            if bm25_hits:
                result_lists.append(bm25_hits)

        # --- Stage 2b: FTS5 keyword search over doc bodies (capability-gated) ---
        fts_hits: list[dict[str, Any]] = []
        if use_fts:
            fts_hits = self._fts_search(
                question, spaces, profile.bm25_limit,
                pack_ids=pack_ids, include_unpackaged=include_unpackaged,
            )
            if fts_hits:
                result_lists.append(fts_hits)

        # --- Stage 3: Graph expansion from vector/BM25/FTS anchor nodes ---
        anchor_ids = _ordered_unique(
            [hit.node_id for hit in vector_hits if hit.node_id]
            + [hit.get("node_id") for hit in bm25_hits]
            + [hit.get("node_id") for hit in fts_hits],
            profile.anchor_limit,
        )
        if anchor_ids and self._neo4j.available:
            graph_results = self._graph_expand(
                anchor_ids, profile.graph_depth, profile.graph_limit,
                pack_ids=pack_ids, include_unpackaged=include_unpackaged,
                spaces=spaces,
            )
            if graph_results:
                result_lists.append([r.to_dict() for r in graph_results])

        # --- Stage 4: Rerank ---
        if use_rerank and result_lists:
            reranker = _get_reranker()()
            merged = reranker.rerank(question, result_lists, top_k=profile.rerank_limit)
        else:
            # Flat merge without reranking
            seen: set[str | None] = set()
            merged = []
            for lst in result_lists:
                for item in lst:
                    if item.get("node_id") not in seen:
                        seen.add(item.get("node_id"))
                        merged.append(item)
            merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        # --- Stage 5: Policy-aware filter ---
        if subject_id and self._rebac is not None:
            merged = self._policy_filter(merged, subject_id)

        # Convert back to QueryResult
        results = []
        for item in merged[:limit]:
            metadata = dict(item.get("metadata") or {})
            if "pack_id" not in metadata:
                pid = infer_pack_id(item)
                if pid:
                    metadata["pack_id"] = pid
            results.append(QueryResult(
                source=item.get("source", "hybrid"),
                node_id=item.get("node_id"),
                score=item.get("rerank_score") or item.get("score", 0.0),
                text=item.get("text"),
                metadata=metadata,
                graph_context=item.get("graph_context"),
            ))
        return QueryOutcome(results=results, warnings=warnings)

    def _bm25_probe_fingerprint(self) -> tuple[int, str] | None:
        """Cheap ``(count, max_updated_at)`` probe for stale-cache detection.

        Prefers ``doc_store.bm25_fingerprint()`` (a ``COUNT(*), MAX(updated_at)``
        query with no row parsing) so the query hot path avoids the 50k
        ``list_nodes`` scan. Falls back to the heavy ``compute_fingerprint`` for
        legacy stores without the capability. Returns ``None`` on error so the
        caller simply skips the staleness check and serves the current index.

        NOTE (#63): ``doc_store.bm25_fingerprint()`` reports the WHOLE table,
        deliberately ignoring ``_BM25_NODE_LIMIT`` — a capped fingerprint pins
        at exactly the cap once the corpus exceeds it, so count-based change
        detection would never fire again regardless of row ordering.

        Must stay byte-for-byte comparable with the fingerprint recorded on
        ``self._bm25_cache`` — both the cold-start build (see ``_bm25_search``)
        and the background rebuild (``_Bm25CacheWorker._rebuild_loop``) stamp
        the index with *this same* whole-table probe result via
        ``BM25Index.build(nodes, fingerprint=...)``, not
        ``compute_fingerprint(nodes)`` on the (possibly capped) indexed nodes.
        That's what keeps this comparison meaningful once the corpus exceeds
        the cap: both sides measure "what does the store look like", not "what
        did we index" vs "what does the store look like" (which would never
        agree again and would schedule a rebuild on every query). Legacy
        stores without ``bm25_fingerprint()`` fall back to the capped
        ``compute_fingerprint`` below on both sides consistently, so they
        don't have this problem in the first place.
        """
        ds = self._doc_store
        if ds is None:
            return None
        try:
            probe = getattr(ds, "bm25_fingerprint", None)
            if probe is not None:
                return probe(limit=_BM25_NODE_LIMIT)
            from opencrab.ontology.bm25 import compute_fingerprint
            return compute_fingerprint(ds.list_nodes(limit=_BM25_NODE_LIMIT))
        except Exception as exc:
            logger.debug("BM25 fingerprint probe failed: %s", exc)
            return None

    def _bm25_search(
        self,
        question: str,
        spaces: list[str] | None,
        limit: int,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """Run BM25 search against doc store nodes, using a cached index.

        Hot path is rebuild-free: rebuilds run on a background worker
        (:meth:`invalidate_bm25_cache`). The only synchronous build is the
        cold start (no cache yet). On every query we run a *cheap* fingerprint
        probe (``doc_store.bm25_fingerprint()`` — ``COUNT(*), MAX(updated_at)``,
        no row parsing) to catch out-of-band writes (e.g. a separate pack-ingest
        process) that never called ``invalidate_bm25_cache()``; on mismatch we
        schedule a background rebuild and keep serving the current index.
        """
        try:
            BM25Index = _get_bm25()  # noqa: N806

            if self._bm25_cache is None:
                # Cold start: nothing to serve yet, so build synchronously once.
                # Fingerprint FIRST, nodes SECOND (#63 follow-up) — see the
                # matching comment in _Bm25CacheWorker._rebuild_loop for why
                # this order (not the reverse) is the safe one: a write
                # landing in between must make the fingerprint stale relative
                # to the nodes, never the other way round, or that write is
                # never picked up. None (probe failed) falls back to
                # compute_fingerprint(nodes) inside BM25Index.build().
                fp = self._bm25_probe_fingerprint()
                nodes = self._doc_store.list_nodes(limit=_BM25_NODE_LIMIT)
                self._bm25_cache = BM25Index.build(nodes, fingerprint=fp)
                self._bm25_cache_size = len(nodes)
                self._bm25_dirty = False
                logger.debug("BM25 index cold-built (%d nodes)", self._bm25_cache_size)
            else:
                # Cheap staleness probe — detects out-of-band writes without a
                # full 50k list_nodes scan. Falls back to the heavy fingerprint
                # only if the store predates bm25_fingerprint().
                fp = self._bm25_probe_fingerprint()
                if fp is not None and fp != self._bm25_cache.fingerprint:
                    self.invalidate_bm25_cache()  # schedule bg rebuild; serve stale

            hits = self._bm25_cache.search(
                question,
                spaces=spaces,
                limit=limit,
                pack_ids=pack_ids,
                include_unpackaged=include_unpackaged,
            )
            for h in hits:
                h["source"] = "bm25"
            return hits
        except Exception as exc:
            logger.warning("BM25 search error: %s", exc)
            return []

    def _fts_search(
        self,
        question: str,
        spaces: list[str] | None,
        limit: int,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """FTS5 키워드 검색 레그(본문 다중어/약어/표준번호 정확매칭).

        백엔드-중립: doc store가 ``supports_keyword`` capability를 노출할 때만 사용.
        미지원·예외 시 빈 리스트로 graceful fallback(기존 하이브리드 무손상).

        issue #52: ``spaces`` was accepted here but never forwarded to
        ``keyword_search`` — this leg silently ignored the caller's space
        filter. Now forwarded straight through; the doc store pushes it into
        its own SQL WHERE clause (see ``LocalSQLDocStore.keyword_search``).
        """
        ds = self._doc_store
        if ds is None or not getattr(ds, "supports_keyword", False):
            return []
        try:
            hits = ds.keyword_search(
                question,
                pack_ids=pack_ids,
                include_unpackaged=include_unpackaged,
                limit=limit,
                spaces=spaces,
            )
        except Exception as exc:
            logger.warning("FTS keyword search error: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for h in hits:
            out.append({
                "source": "keyword",
                "node_id": h.get("node_id"),
                "score": h.get("score", 0.0),
                "text": h.get("text"),
                "metadata": h.get("metadata") or {},
            })
        return out

    def _policy_filter(
        self,
        results: list[dict[str, Any]],
        subject_id: str,
    ) -> list[dict[str, Any]]:
        """
        Remove results the subject cannot view.

        Uses ReBAC 'view' permission check. Nodes with no registered policy
        are passed through (open by default).
        """
        filtered = []
        for item in results:
            nid = item.get("node_id")
            if not nid:
                filtered.append(item)
                continue
            try:
                decision = self._rebac.check(
                    subject_id=subject_id,
                    permission="view",
                    resource_id=nid,
                )
                if decision.granted:
                    filtered.append(item)
                else:
                    logger.debug(
                        "Policy filter: %s denied view on %s", subject_id, nid
                    )
            except Exception:
                # No policy registered = pass through
                filtered.append(item)
        return filtered

    def _vector_search(
        self,
        question: str,
        spaces: list[str] | None,
        limit: int,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[QueryResult]:
        """Run ChromaDB semantic similarity search.

        Pack filtering uses Chroma's ``where`` clause when ``include_unpackaged``
        is False (server-side enforcement). When the server rejects the
        combined clause (older Chroma builds), we fall back to a wider scan
        and apply the filter in Python. ``include_unpackaged=True`` always
        uses the Python post-filter path since Chroma cannot express
        "in set OR is null" directly in a single clause.
        """
        if not self._chroma.available:
            logger.debug("ChromaDB unavailable, skipping vector search.")
            return []

        try:
            use_post_filter = bool(pack_ids) and include_unpackaged
            if use_post_filter:
                where = _build_chroma_where(spaces=spaces, pack_ids=None)
                effective_pack_filter: list[str] | None = list(pack_ids) if pack_ids else None
            else:
                where = _build_chroma_where(spaces=spaces, pack_ids=pack_ids)
                effective_pack_filter = None

            if use_post_filter:
                n_results = max(min(limit, 20) * 4, 20)
            else:
                n_results = min(limit, 20)
            try:
                hits = self._chroma.query(
                    query_text=question,
                    n_results=n_results,
                    where=where,
                )
            except Exception as exc:
                # Some Chroma versions error on $and / $in clauses. Fall back
                # to a wider scan + Python post-filter.
                logger.warning("Chroma where filter rejected (%s); using post-filter fallback.", exc)
                fallback_where = _build_chroma_where(spaces=spaces, pack_ids=None)
                hits = self._chroma.query(
                    query_text=question,
                    n_results=max(n_results * 4, 20),
                    where=fallback_where,
                )
                effective_pack_filter = list(pack_ids) if pack_ids else None

            results: list[QueryResult] = []
            for hit in hits:
                meta = hit.get("metadata") or {}
                if effective_pack_filter is not None:
                    pid = infer_pack_id({"metadata": meta, **hit})
                    if pid is None:
                        if not include_unpackaged:
                            continue
                    elif pid not in set(effective_pack_filter):
                        continue
                # Convert cosine distance to similarity score (1 - distance)
                distance = hit.get("distance") or 0.0
                score = max(0.0, 1.0 - float(distance))
                results.append(
                    QueryResult(
                        source="vector",
                        node_id=meta.get("node_id") or hit.get("id"),
                        score=score,
                        text=hit.get("document"),
                        metadata=meta,
                    )
                )
            return results
        except Exception as exc:
            logger.warning("Vector search error: %s", exc)
            return []

    def _graph_expand(
        self,
        anchor_ids: list[str],
        depth: int,
        limit: int,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        spaces: list[str] | None = None,
    ) -> list[QueryResult]:
        """Expand graph neighbourhood from anchor node IDs.

        Uses at most 3 anchors for depth > 1 (multi-hop) to keep result sets
        manageable. Edge-type weights adjust the baseline score; a per-hop
        decay of 0.85 reduces scores for deeper neighbours.

        issue #52: this method previously had no ``spaces`` parameter at
        all — the graph leg silently ignored the caller's space filter and
        mixed in neighbours from every space. Now pushed straight into
        ``find_neighbors`` (real ``space``/``space_id`` column or property
        on every backend, so no post-filter-after-LIMIT class of bug — see
        each store's ``find_neighbors`` docstring).
        """
        if not self._neo4j.available:
            return []

        expanded: list[QueryResult] = []
        seen: set[str] = set(anchor_ids)
        max_anchors = 3 if depth > 1 else 5
        hop_decay = 0.85 ** (depth - 1)

        for anchor_id in anchor_ids[:max_anchors]:
            try:
                # Only forward pack/space kwargs when active so older graph
                # store stubs (without the new signature) keep working.
                extra: dict[str, Any] = {}
                if pack_ids:
                    extra["pack_ids"] = pack_ids
                    extra["include_unpackaged"] = include_unpackaged
                if spaces:
                    extra["spaces"] = spaces
                neighbours = self._neo4j.find_neighbors(
                    node_id=anchor_id,
                    direction="both",
                    depth=depth,
                    limit=limit,
                    **extra,
                )
                for n in neighbours:
                    props = n.get("properties", {})
                    nid = props.get("id")
                    if nid and nid not in seen:
                        seen.add(nid)
                        rel_type = n.get("relation_type") or n.get("relationship_type") or ""
                        rel_key = str(rel_type).upper()
                        base_score = _EDGE_WEIGHTS.get(rel_key, _DEFAULT_EDGE_SCORE) * hop_decay
                        context: dict[str, Any] = {
                            "anchor_id": anchor_id,
                            "labels": n.get("labels", []),
                            "relation_type": rel_type,
                            "relationship_types": n.get("relationship_types"),
                            "depth": n.get("depth") or depth,
                        }
                        # anchor_id is the BFS root, NOT the traversed edge's
                        # source (they differ at every depth > 1, and even at
                        # depth 1 the direction is unknown for direction="both").
                        # Carry the store's real endpoints when it reports them.
                        if n.get("from_id") and n.get("to_id"):
                            context["edge_endpoints"] = {
                                "from_id": n["from_id"],
                                "to_id": n["to_id"],
                            }
                        expanded.append(
                            QueryResult(
                                source="graph",
                                node_id=nid,
                                score=base_score,
                                text=_property_text(props, rel_type),
                                metadata=props,
                                graph_context=context,
                            )
                        )
            except Exception as exc:
                logger.debug("Graph expand error for anchor %s: %s", anchor_id, exc)

        return expanded

    def ingest(
        self,
        text: str,
        source_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Ingest a text chunk into the vector store.

        Parameters
        ----------
        text:
            The text to embed and store.
        source_id:
            Stable identifier for this source (used as the document ID).
        metadata:
            Additional metadata attached to the vector (e.g. space, node_id).

        Returns
        -------
        dict with ingestion status.
        """
        meta = metadata or {}
        meta["source_id"] = source_id

        result: dict[str, Any] = {"source_id": source_id, "stores": {}}

        if not self._chroma.available:
            result["stores"]["chromadb"] = "unavailable"
            return result

        try:
            ids = self._chroma.upsert_texts(
                texts=[text],
                metadatas=[meta],
                ids=[source_id],
            )
            result["stores"]["chromadb"] = f"ok (id={ids[0]})"
            result["vector_id"] = ids[0]
        except Exception as exc:
            logger.warning("Ingest to ChromaDB failed: %s", exc)
            result["stores"]["chromadb"] = f"error: {exc}"

        # Doc-store mutations happen outside ingest() today, but vector
        # additions still warrant a BM25 rebuild on the next query because
        # the doc store may have been written alongside this call.
        self.invalidate_bm25_cache()
        return result

    def keyword_search(
        self,
        keyword: str,
        spaces: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Simple keyword search in the Neo4j graph using CONTAINS.

        Parameters
        ----------
        keyword:
            Search term.
        spaces:
            Optional list of spaces to filter.
        limit:
            Max results.
        """
        if not self._neo4j.available:
            return []

        # --- 로컬 모드: LocalGraphStore는 run_cypher()가 no-op이므로
        #     export_nodes() + Python-side 키워드 필터로 대체한다.
        # NOTE: GraphStore Protocol(opencrab/stores/_graph_protocol.py)에는
        # keyword_search 메서드가 없어 이 isinstance 분기를 아직 제거할 수 없다
        # (R5는 그 Protocol에 이미 있는 7개 메서드만 다뤘다) — Stage 8에서 정리 예정.
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore  # noqa: PLC0415
        from opencrab.stores.local_graph_store import LocalGraphStore  # noqa: PLC0415
        if isinstance(self._neo4j, (LocalGraphStore, KuzuGraphStore)):
            kw_lower = keyword.lower()
            search_fields = ["name", "description", "text", "title", "label", "summary"]
            candidate_rows = self._neo4j.export_nodes(limit=_BM25_NODE_LIMIT)
            results: list[dict[str, Any]] = []
            for row in candidate_rows:
                props = row.get("props", {})
                labels = row.get("labels", [""])
                # spaces 필터
                if spaces and props.get("space") not in spaces:
                    continue
                for field in search_fields:
                    val = props.get(field, "")
                    if val and kw_lower in str(val).lower():
                        results.append({"node": props, "label": labels[0] if labels else ""})
                        break
                if len(results) >= limit:
                    break
            return results

        # --- Neo4j(Docker) 모드: 기존 Cypher 경로 유지
        space_filter = ""
        params: dict[str, Any] = {"kw": keyword.lower(), "limit": limit}
        if spaces:
            space_filter = "AND n.space IN $spaces"
            params["spaces"] = spaces

        cypher = f"""
            MATCH (n)
            WHERE toLower(n.name) CONTAINS $kw
               OR toLower(n.description) CONTAINS $kw
               OR toLower(n.text) CONTAINS $kw
               {space_filter}
            RETURN properties(n) AS props, labels(n)[0] AS label
            LIMIT $limit
        """
        try:
            rows = self._neo4j.run_cypher(cypher, params)
            return [
                {"node": dict(r.get("props") or {}), "label": r.get("label")}
                for r in rows
            ]
        except Exception as exc:
            logger.warning("Keyword search error: %s", exc)
            return []
