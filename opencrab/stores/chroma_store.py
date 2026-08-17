"""
ChromaDB vector store adapter.

LocalCrab uses ChromaDB PersistentClient by default, so no Chroma server is
required. HttpClient remains available for direct adapter use, but the
LocalCrab factory always selects persistent local mode.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from opencrab.stores._vector_base import generate_add_ids, generate_upsert_ids

logger = logging.getLogger(__name__)

_COLLECTION_LOCKS: dict[str, threading.Lock] = {}
_COLLECTION_LOCKS_GUARD = threading.Lock()


def _collection_lock(key: str) -> threading.Lock:
    """One lock per (client target, collection name), shared by every
    ChromaStore in this process that points at the same collection.

    Per-instance locking is not enough: nothing guarantees a single store
    instance per collection — ``opencrab/mcp/tools/__init__.py:_get_context()``
    memoises its stores but does not guard the initialisation itself, so two
    concurrent first calls each build their own. Mirrors the per-path registry
    in ``opencrab/locking.py:_process_lock``, including its no-eviction
    property (bounded by the collections this process opens).

    The key deliberately excludes ``embedding_function``: instances over one
    collection may legitimately carry different EFs, and sharing a *lock*
    between them is harmless where sharing state would not be.
    """
    with _COLLECTION_LOCKS_GUARD:
        return _COLLECTION_LOCKS.setdefault(key, threading.Lock())


class ChromaStore:
    """ChromaDB adapter — persistent local by default, HttpClient optional."""

    def __init__(
        self,
        host: str,
        port: int,
        collection_name: str,
        local_mode: bool = False,
        local_path: str = "./opencrab_data/chroma",
        embedding_function: Any = None,
        # embedding_function: ChromaDB EmbeddingFunction 인스턴스.
        # None 이면 ChromaDB 기본 EF(all-MiniLM-L6-v2 ONNX, 384d) 사용 — 기존 동작.
        # ResilientEmbeddingFunction(KURE-v1) 을 주입하면 KURE 로 전환.
        # 변경 이유: 임베딩 모델을 외부에서 주입받아 교체 가능하게 함.
    ) -> None:
        self._host = host
        self._port = port
        self._collection_name = collection_name
        self._local_mode = local_mode
        self._local_path = local_path
        self._embedding_function = embedding_function
        self._client: Any = None
        self._collection: Any = None
        self._available = False
        # Chroma 자체는 개별 호출 단위로만 스레드 안전하다(공식 System Constraints).
        # 이 락의 용도는 두 가지다. (1) 앱 레벨 공유 상태인 self._collection 핸들 교체
        # (reset_collection)를 원자화하고 읽기/쓰기가 교체 도중의 핸들을 보지 않도록
        # 짧게 스냅샷한다. (2) upsert_texts 의 get→delete/add 는 한 슬롯에 대한
        # read-modify-write 라 호출 단위 안전성만으로는 부족하므로 그 구간 전체를
        # 직렬화한다(#175 리뷰 P2). 같은 컬렉션을 가리키는 인스턴스끼리 공유한다.
        self._lock = _collection_lock(
            f"{os.path.abspath(local_path) if local_mode else f'{host}:{port}'}"
            f"\x00{collection_name}"
        )
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            import chromadb  # type: ignore[import]

            if self._local_mode:
                import os
                os.makedirs(self._local_path, exist_ok=True)
                self._client = chromadb.PersistentClient(path=self._local_path)
                logger.info("ChromaDB local mode at %s", self._local_path)
            else:
                self._client = chromadb.HttpClient(host=self._host, port=self._port)
                self._client.heartbeat()
                logger.info("ChromaDB connected at %s:%s", self._host, self._port)

            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                # embedding_function=None 이면 Chroma 기본 EF(minilm) 적용.
                # ResilientEF(KURE) 주입 시 해당 EF 로 add/query 자동 수행.
                embedding_function=self._embedding_function,
            )
            self._available = True
        except Exception as exc:
            if self._local_mode:
                logger.warning("ChromaDB local init failed: %s", exc)
            else:
                logger.warning(
                    "ChromaDB unavailable (%s:%s): %s", self._host, self._port, exc
                )
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        """Return True if ChromaDB is reachable."""
        try:
            self._client.heartbeat()
            return True
        except Exception:
            return False

    def _collection_handle(self) -> Any:
        """Return the current collection handle, snapshotted under the lock so a
        concurrent reset_collection() swap is never observed half-applied."""
        with self._lock:
            return self._collection

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """
        Add text chunks to the vector store.

        Parameters
        ----------
        texts:
            List of text strings to embed and store.
        metadatas:
            Parallel list of metadata dicts for each text.
        ids:
            Optional stable IDs; auto-generated from content hash if omitted.

        Returns
        -------
        list[str]
            The IDs of the inserted documents.
        """
        self._require_available()

        if ids is None:
            ids = generate_add_ids(texts)

        if metadatas is None:
            metadatas = [{} for _ in texts]

        # Sanitize metadata — ChromaDB requires string/int/float/bool values
        clean_meta = [_sanitize_metadata(m) for m in metadatas]

        # Under the store lock like every other writer here, so this cannot
        # land inside another thread's replace window. Snapshot the handle
        # inside the block -- _collection_handle() takes this same
        # non-reentrant lock.
        with self._lock:
            self._collection.add(documents=texts, metadatas=clean_meta, ids=ids)
        logger.debug("ChromaDB: added %d documents", len(texts))
        return ids

    def upsert_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Upsert (add or update) text chunks — full replace semantics, with
        one exception carved out for uri-bearing records.

        [#175] Default contract: full REPLACE. chromadb's native
        ``collection.upsert()``/``update()`` MERGE metadata into any existing
        record (verified empirically against chromadb 1.5.7/1.5.9) — a stale
        key dropped by the caller's canonical metadata transform would
        survive forever under a plain ``upsert()``. sqlite-vec
        (DELETE-then-INSERT) and pgvector (``ON CONFLICT ... DO UPDATE SET
        metadata = EXCLUDED.metadata``) both give full-replace semantics, so
        this store replaces too, via delete()-then-add(). delete() on ids that
        don't exist yet is a no-op (verified), so that is safe for the
        add-or-update case as well.

        Which ids actually need it: only those that would LOSE a metadata key.
        A merge and a replace differ solely in what happens to keys the caller
        no longer passes, so when the existing metadata's keys are a subset of
        the new one's, native ``upsert()`` lands exactly the caller's metadata
        (measured: seeding ``{'a','b'}`` then upserting ``{'a','b','c'}`` reads
        back as exactly the new dict, document and embedding replaced). Those
        ids — including every brand-new id, which has no existing keys at all
        — take the single atomic call, which cannot expose a delete window to
        a concurrent reader and has no half-applied state to roll back. Every
        caller in this repository passes the complete canonical metadata dict
        on every call (see ``opencrab/stores/_vector_base.py``), so this is
        the path that normally runs; delete()+add() is reserved for a genuine
        key drop.

        EXCEPTION — uri-bearing records: this store never produces a uri of
        its own (the only ``add(uris=...)`` here is ``_rollback`` replaying
        uris it just read back), so any id that already carries one was
        written by something external. Silently
        replacing it would drop the uri, and there is no safe way to carry it
        over through delete()+add(): ``add(uris=...)`` raises ValueError
        without an accompanying embedding on a collection with no
        ``data_loader`` (verified against chromadb 1.5.9), and reusing the
        record's *old* embedding while the document text changes would leave
        the embedding out of sync with the new document. So ids that already
        carry a uri are routed through native ``upsert()`` instead — merge
        semantics (document and embedding are recomputed from the new text;
        metadata is merged, so stale keys on these records can persist).
        This is not a regression: it is the same fallback contract
        ``opencrab/pack/load.py``'s ``_vec_meta_update`` already documents for
        its own chroma/uri branch ("URI 레코드는 치환하지 않고 upsert 병합으로
        우회") — this method now honors that contract directly instead of
        clobbering it.

        Empty metadata is rejected before anything is touched: chromadb
        refuses an empty dict in ``add()`` exactly as it does in ``upsert()``,
        so the replace path would otherwise delete the old record and only
        then fail. Omitting ``metadatas`` altogether is the call shape that
        hits this — it becomes ``[{}, ...]`` here.

        Concurrency: the routing read and the writes it selects run under the
        store's lock — shared by every ChromaStore in this process that
        targets the same collection, and the same lock ``reset_collection()``
        takes — so same-id concurrent upserts inside one process cannot
        interleave into a lost update.

        Readers and the replace path: between its delete() and add() the
        record does not exist, and a reader that snapshotted the collection
        handle just before the write started can read in that gap.
        ``get_by_id`` re-checks a miss under the lock and so never reports a
        live record as absent; ``query`` does not, deliberately — closing it
        means holding this lock across every search, parking searches behind
        an in-flight ingest's embedding computation, and a search cannot even
        tell that an n+1-th hit was momentarily missing. Note this window is
        only reachable for an upsert that actually drops a metadata key; the
        ordinary full-dict upsert takes the atomic path above.

        What that lock does NOT cover, stated so callers do not over-trust it:

        - Other processes. This class enforces nothing across them: writes are
          serialised only where the *caller* itself holds
          ``opencrab/locking.py``'s ``write.lock``, and nothing here can check
          that it did. Most write paths (MCP tools, the CLI, the REST app's
          write endpoints) do take it, but treat that as caller discipline to
          verify at the call site rather than a property of this store — an
          enumeration here goes stale the moment a new writer lands, and one
          already has. ``chroma.lock`` is no substitute either: it is a SHARED
          lock and MCP-only (issue #140).
        - The collection handle. Only the lock is shared between instances;
          each keeps its own handle, so a ``reset_collection()`` on one
          instance leaves another's handle pointing at the deleted collection
          (chroma then raises NotFoundError — loud, not silent). Pre-existing
          behavior, unchanged here.

        Failure rollback: chroma embeds inside add(), so an embedding backend
        that is down makes add() raise *after* the replace path has already
        deleted the old records. The parent implementation's native upsert()
        left them intact on such a failure, so this method takes a snapshot
        (documents, metadata, embeddings, uris) in the same get() that probes
        for uris, and on any exception from the mutation it deletes the batch
        and re-adds that snapshot — pre-call state, whole batch, merge branch
        included. Replaying stored embeddings means the rollback never calls
        the embedding function, so it works precisely when the embedding
        backend is the thing that failed. Note the guarantee is about
        PRE-SUBMIT failures (embedding, validation); a failure after chroma
        has begun applying a call carries no such promise here or in the
        native upsert() this replaced, which is why the rollback deletes
        before it re-adds. COST: every upsert now also reads the existing
        records' documents, metadata and vectors.

        Non-atomicity (replace path only): if the process dies between
        delete() and add(), the row is lost until the same chunk id reappears
        in a later incremental load and gets re-added via the
        ``_live_vec_ids`` diff; if it never reappears, the row stays lost.
        The merge (upsert) path has no such window — it's a single native
        call.
        """
        self._require_available()

        if ids is None:
            ids = generate_upsert_ids(texts)
        if metadatas is None:
            metadatas = [{} for _ in texts]

        # Validate the batch shape BEFORE touching the store (review P1):
        # the replace path deletes matching records first, so a mismatched
        # batch must be rejected here -- native upsert() used to reject it
        # without erasing anything, and delete-then-crash would lose rows.
        if len(ids) != len(texts) or len(metadatas) != len(texts):
            raise ValueError(
                f"upsert_texts: mismatched batch -- texts={len(texts)}, "
                f"ids={len(ids)}, metadatas={len(metadatas)}"
            )

        clean_meta = [_sanitize_metadata(m) for m in metadatas]

        # Reject empty metadata BEFORE any mutation (review P1): chromadb
        # rejects an empty dict in add() just as it does in upsert(), so a
        # batch carrying one would fail only AFTER the replace path had
        # deleted the old record -- erasing it for good. The parent commit's
        # native upsert() rejected the same input without deleting anything;
        # this keeps that non-destructive contract. Note metadatas=None became
        # [{} ...] above, so the documented "omit metadatas" call shape lands
        # here rather than in the store. _sanitize_metadata never drops keys,
        # so checking clean_meta is equivalent to checking the caller's dicts.
        empty_pos = [i for i, meta in enumerate(clean_meta) if not meta]
        if empty_pos:
            raise ValueError(
                "upsert_texts: chromadb rejects empty metadata dicts "
                f"(positions {empty_pos}, ids {[ids[i] for i in empty_pos]}); "
                "pass at least one metadata key per text"
            )

        # Serialise the whole check-then-act (review P2): the uri probe and
        # the delete/add it selects are one read-modify-write on a shared
        # slot, while chroma only makes each individual call thread-safe and
        # the native upsert() this path replaced was a single operation.
        # Unserialised, two threads upserting the same id both pass get(),
        # both delete(), and then the second add() is SILENTLY IGNORED
        # (verified on chromadb 1.5.9: add() on an existing id neither raises
        # nor overwrites -- the first record wins), so the later writer's
        # document is lost while it reports success.
        # COST: chroma embeds inside add()/upsert(), so concurrent upserts on
        # one collection now serialise on that too. Accepted for correctness;
        # shard into per-id locks if it ever shows up as a bottleneck (the
        # invariant only needs same-id mutual exclusion).
        with self._lock:
            # Snapshot the handle directly -- _collection_handle() takes this
            # same non-reentrant lock and would deadlock here.
            handle = self._collection

            # One read serves three purposes: it finds which ids carry a uri,
            # it says which ids would lose a metadata key (the only reason to
            # replace rather than merge — see docstring), and it is the
            # rollback snapshot for the mutation below.
            existing = handle.get(
                ids=ids, include=["uris", "documents", "metadatas", "embeddings"]
            )
            uri_ids = {
                doc_id
                for doc_id, uri in zip(existing["ids"], existing.get("uris") or [])
                if uri is not None
            }
            existing_meta = dict(zip(existing["ids"], existing["metadatas"]))

            def _is_atomic(pos: int) -> bool:
                doc_id = ids[pos]
                if doc_id in uri_ids:
                    return True
                # No existing key can go stale => native upsert()'s merge lands
                # exactly the caller's metadata, so the atomic call is a full
                # replace and there is no delete window to expose.
                return set(existing_meta.get(doc_id) or {}).issubset(clean_meta[pos])

            merge_pos = [i for i in range(len(ids)) if _is_atomic(i)]
            replace_pos = [i for i in range(len(ids)) if not _is_atomic(i)]

            try:
                if merge_pos:
                    handle.upsert(
                        documents=[texts[i] for i in merge_pos],
                        metadatas=[clean_meta[i] for i in merge_pos],
                        ids=[ids[i] for i in merge_pos],
                    )
                    merged_uri = [ids[i] for i in merge_pos if ids[i] in uri_ids]
                    if merged_uri:
                        logger.warning(
                            "ChromaDB: %d uri-bearing record(s) upserted via merge "
                            "(uri preserved, stale metadata keys may persist): %s",
                            len(merged_uri), merged_uri,
                        )
                if replace_pos:
                    replace_ids = [ids[i] for i in replace_pos]
                    handle.delete(ids=replace_ids)
                    handle.add(
                        documents=[texts[i] for i in replace_pos],
                        metadatas=[clean_meta[i] for i in replace_pos],
                        ids=replace_ids,
                    )
            except Exception:
                # The mutation may be partially applied at this point -- some
                # ids deleted, some rewritten, some never touched (a uri-only
                # batch can die inside upsert() before any delete runs) --
                # which is exactly why the rollback clears the whole batch
                # before replaying the snapshot, rather than trusting which
                # step got that far (review round 4).
                try:
                    _rollback(handle, ids, existing)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.error(
                        "ChromaDB: rollback after a failed upsert also failed "
                        "(%s) -- these ids may now be missing: %s",
                        rollback_exc, ids,
                    )
                raise

        logger.debug(
            "ChromaDB: upserted %d documents (%d replaced, %d merged atomically)",
            len(ids), len(replace_pos), len(merge_pos),
        )
        return ids

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Semantic similarity search.

        Parameters
        ----------
        query_text:
            Natural language query string.
        n_results:
            Maximum number of results to return.
        where:
            Optional metadata filter (ChromaDB `where` clause).

        Returns
        -------
        list of dicts with keys: id, document, metadata, distance.
        """
        self._require_available()

        kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": n_results,
        }
        if where:
            kwargs["where"] = where

        result = self._collection_handle().query(**kwargs)

        hits: list[dict[str, Any]] = []
        if result["ids"]:
            for idx in range(len(result["ids"][0])):
                hits.append(
                    {
                        "id": result["ids"][0][idx],
                        "document": result["documents"][0][idx],
                        "metadata": result["metadatas"][0][idx] if result["metadatas"] else {},
                        "distance": result["distances"][0][idx] if result.get("distances") else None,
                    }
                )
        return hits

    def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        """Retrieve a document by its ID.

        A miss is re-checked under the store lock. A key-dropping upsert
        replaces via delete()+add(), and a reader that snapshotted the handle
        just before that write started can land between the two and see an
        existing record as absent. Callers act on that absence: the identity
        probe behind ``pack_create``/``pack_ingest``
        (``opencrab/mcp/tools/pack.py:_foreign_pack``) reads None as "no
        conflict" in an otherwise fail-closed check, so a transient miss would
        let a foreign pack's slot be overwritten. The re-check costs one lock
        acquisition on the miss path and blocks only while some thread is
        mid-write on this collection.
        """
        self._require_available()

        hit = _read_one(self._collection_handle(), doc_id)
        if hit is not None:
            return hit
        with self._lock:
            return _read_one(self._collection, doc_id)

    def delete(self, ids: list[str]) -> None:
        """Delete documents by their IDs.

        Held under the store lock so it cannot land between a concurrent
        replace's delete() and add() — where its removal would simply be
        undone by that add, or by the rollback replaying the snapshot.
        Note ``opencrab/pack/load.py`` mutates the raw chroma handle directly
        in places and so stays outside this lock; that is tracked separately.
        """
        self._require_available()
        with self._lock:
            self._collection.delete(ids=ids)

    def count(self) -> int:
        """Return the number of documents in the collection."""
        if not self._available:
            return 0
        return self._collection_handle().count()

    def reset_collection(self) -> None:
        """Delete and recreate the collection (destructive)."""
        self._require_available()
        # delete→재생성으로 self._collection 핸들을 교체하므로 락으로 직렬화한다.
        # 락이 없으면 동시 reset 시 두 스레드가 같은 컬렉션을 delete 하여 '이미 삭제됨'
        # 에러가 나거나, 읽기가 삭제된 컬렉션을 가리키는 손상 핸들을 볼 수 있다.
        with self._lock:
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=self._embedding_function,
            )
        logger.info("ChromaDB: collection '%s' reset.", self._collection_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_one(handle: Any, doc_id: str) -> dict[str, Any] | None:
    """Point lookup shaped for this store's callers, or None when absent."""
    result = handle.get(ids=[doc_id])
    if not result["ids"]:
        return None
    return {
        "id": result["ids"][0],
        "document": result["documents"][0],
        "metadata": result["metadatas"][0] if result["metadatas"] else {},
    }


def _rollback(handle: Any, batch_ids: list[str], snapshot: dict[str, Any]) -> None:
    """Put the collection back the way it was before a failed upsert batch.

    Deletes every id in the batch, then re-adds the records that existed
    beforehand — brand-new ids therefore end up absent, which IS their
    pre-call state. Deleting first is required: add() on an id that already
    exists is a silent no-op in chroma, so a partially applied add would
    otherwise survive the rollback untouched.

    Everything is replayed from the snapshot, so the re-add passes embeddings
    explicitly and chroma never calls the embedding function — the rollback
    still works when a dead embedding backend is what caused the failure
    (measured: zero EF calls, embedding restored exactly). uris ride along in
    mixed and all-None shapes alike.

    Membership is read off ``snapshot["ids"]`` only. ``snapshot["embeddings"]``
    is a NumPy array, and testing an array for truthiness raises.

    Records already in the store may carry no metadata at all — chroma hands
    those back as ``None`` (added without a ``metadatas`` argument) or as
    ``{}`` (an externally written uri record) — and replaying either verbatim
    would make this very add() fail on chroma's non-empty-dict rule, losing
    the records the rollback exists to save. Both spellings are normalised to
    ``None``, chroma's own "no metadata here" value, per record so that a
    mixed batch still goes back in one call. ``None`` itself is accepted at
    both ends of the supported range: 0.5.0's ``validate_metadata`` returns
    early for it and only rejects an empty dict, and 1.5.9 does the same.

    A record can equally carry no document (embedding-only, or an externally
    written uri record), so the replayed ``documents`` list can hold ``None``
    too. That is legal because this call always supplies ``embeddings`` from
    the snapshot: chroma validates documents with ``nullable=(embeddings is
    not None)``, and 0.5.0 does not validate document elements at all. Keep
    the embeddings argument if this is ever refactored — dropping it would
    turn every documentless record into a rollback failure.
    """
    handle.delete(ids=batch_ids)
    if not snapshot["ids"]:
        return
    handle.add(
        ids=snapshot["ids"],
        embeddings=snapshot["embeddings"],
        documents=snapshot["documents"],
        metadatas=[meta if meta else None for meta in snapshot["metadatas"]],
        uris=snapshot.get("uris"),
    )


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Convert metadata values to ChromaDB-compatible types."""
    clean: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)):
            clean[k] = v
        elif v is None:
            clean[k] = ""
        else:
            clean[k] = str(v)
    return clean
