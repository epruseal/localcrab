"""
sqlite-vec vector store adapter (SQLite-unified backend).

Drop-in replacement for :class:`ChromaStore` that keeps the vector index in the
same SQLite WAL discipline as the graph/doc/sql stores, removing Chroma's
"single-process writer" constraint (and the custom flock layer built around it).
See ``docs/pgvector-migration-plan.md`` §3.6 / §4.1-A / §9 for the design.

WHY A SEPARATE STORE (not an embedding-function swap):
    sqlite-vec is a *vector store backend*, not an embedding backend. Chroma
    embeds text internally; sqlite-vec stores raw vectors, so the app computes
    the embedding (KURE via ResilientEmbeddingFunction) and INSERTs the vector.
    The embedding path is identical to Chroma's ``openai`` branch — only the
    storage/search backend changes. Selection is via ``VECTOR_BACKEND``.

CONTRACT PARITY (ChromaStore, chroma_store.py):
    Same public methods/signatures/returns/guards. ``query`` returns dicts with
    keys ``id/document/metadata/distance`` where ``distance`` is cosine distance
    (1 - cos), so the caller's ``score = 1 - distance`` is preserved. ID rules,
    ``_sanitize_metadata``, and the ``available``/``ping``/``count->0`` guards
    are reused verbatim.

CONCURRENCY (LocalSQLDocStore pattern, local_sql_doc_store.py):
    Each thread gets its own sqlite3 connection (threading.local); a
    threading.Lock serialises writers so only one per-thread connection writes
    the WAL at a time. Reads take no lock and run concurrently under WAL. Every
    connection loads the sqlite-vec extension.

VEC0 NOTES (verified against sqlite-vec 0.1.9):
    - TEXT PRIMARY KEY, ``pack_id partition key`` (equality pre-filter), and
      ``distance_metric=cosine`` are all supported; cosine distance == 1 - cos.
    - vec0 virtual tables do NOT support ``INSERT OR REPLACE`` / ``ON CONFLICT``
      → upsert is implemented as DELETE-then-INSERT.
    - KNN requires an explicit ``k`` constraint:
      ``WHERE embedding MATCH ? AND k = ? [AND pack_id = ?] ORDER BY distance``.
    - metadata columns are limited (16 cols, 6 operators, no IN). We therefore
      store the full metadata dict as an auxiliary JSON column and replicate
      Chroma ``where`` semantics ($in/$and/space) with a Python post-filter,
      pushing only single ``pack_id`` equality down to the partition key.

BINARY 2-STAGE ANN (VECTOR_ANN=binary, docs/pgvector-migration-plan.md §3.7):
    Global (no filter) brute-force KNN over 179k×1024d floats is
    CPU/memory-bandwidth bound (~868ms p95). With ``ann="binary"`` the store
    answers GLOBAL (no ``where``) queries in two stages over an in-process
    cache (see WHY IN-PROCESS below):
      1. coarse : sign-bit hamming (numpy XOR+bitwise_count over a 23MB RAM
         bit matrix) → C candidates (ann_coarse_k, default 512)
      2. rerank : int8-dequantized cosine over the C candidates (RAM), then the
         top ~3n are refined with EXACT float cosine (``vec_distance_cosine``
         point queries) — returned distances are exact, contract preserved.
    Pack-scoped queries stay on the exact float path (already ~8ms via the
    partition key — §3.7 keeps exact as the safe default there); queries with
    residual (non-pack) filters also fall back to exact so the post-filter
    keeps its full candidate pool.

    WHY IN-PROCESS (measured on 0.1.9, 179,784×1024d):
    vec0's own KNN/point machinery cannot meet the ≤100ms gate — its bit-KNN
    MATCH scan costs ~336ms (per-row vtab overhead, not popcount), and ANY
    per-row point access costs ~0.76ms (each read materialises a 4MB vector
    chunk), so a vec0-native coarse+rerank lands at ~730ms. The in-process
    cache (bit matrix + int8 matrix ≈ 207MB RAM at 179k×1024d) brings the
    2-stage query to tens of ms. The cache is built lazily (~3s — direct
    shadow-chunk reads; a vtab full scan would be ~97s), kept fresh via
    (max rowid, PRAGMA data_version) checks + explicit invalidation on this
    store's writes, and rebuilt on any detected change.

    Preflight facts (verified on sqlite-vec 0.1.9):
    - float + bit vector columns CAN coexist in one vec0 table; the bit column
      must be declared WITHOUT ``distance_metric`` (hamming is implied; an
      explicit ``distance_metric=hamming`` fails to parse).
    - bit BLOBs must be bound through ``vec_bit(?)`` — a raw packed-bytes bind
      whose length is divisible by 4 is misread as a float32 vector.
    - ``numpy.packbits(vec > 0, bitorder="little")`` is byte-identical to
      sqlite-vec's own ``vec_quantize_binary(float_blob)`` (cross-checked on
      signed vectors — note ``> 0`` and LSB-first, see :func:`_sign_bits`).
    - once the table has ``embedding_bit``, vec0 REQUIRES a value for it on
      every INSERT (no NULL allowed). Write-path gating is therefore driven by
      the ACTUAL schema (PRAGMA table_info), not by the config flag — a DB that
      was never migrated keeps the original 5-column INSERT byte-for-byte.
    - the stored bit column is the durable §3.7 representation (kept in
      lock-step with the floats on every write); the query path derives its RAM
      bit matrix from the floats directly, which is guaranteed identical.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from opencrab.stores._graph_common import IDENT_RE as _IDENT_RE
from opencrab.stores._sqlite_base import _SqliteConnMixin
from opencrab.stores._vector_base import (
    default_metadatas,
    embed_and_validate,
    generate_add_ids,
    generate_upsert_ids,
    validate_lengths,
)
from opencrab.stores.chroma_store import _sanitize_metadata

logger = logging.getLogger(__name__)

# vec0 hard-caps the KNN `k` parameter at 4096 (0.1.9). Any query MUST clamp
# fetch_k to this or it raises OperationalError. Pack constraints are pushed
# down to the partition key so the common filters stay exact at any scale; only
# residual constraints (e.g. `space`) fall back to a bounded post-filter.
_VEC0_K_MAX = 4096


def _sign_bits(vec: list[float]) -> bytes:
    """Pack the sign bits of a float vector into bytes (binary quantization).

    ``bit = 1 where component > 0``, packed LSB-first within each byte —
    byte-identical to sqlite-vec's own ``vec_quantize_binary(float_blob)``
    (cross-checked against SQL output on signed vectors; note it is ``> 0``,
    not ``>= 0``, and little bit-order, not numpy's default MSB-first).
    App-side derivation (store writes/queries) and SQL-side backfill
    (migration script) therefore produce the same BLOB. No re-embedding: this
    is a pure sign-bit projection of the stored float vector. dim must be
    divisible by 8 (KURE 1024 is).
    """
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32)
    return np.packbits((arr > 0).astype(np.uint8), bitorder="little").tobytes()


class _AnnCache:
    """In-process 2-stage ANN cache (§3.7): ids + sign-bit matrix (coarse) +
    int8-quantized vectors with per-row scales (rerank). ``max_rowid`` anchors
    freshness against the ``{table}_rowids`` shadow table."""

    __slots__ = ("ids", "bits", "q8", "scale", "max_rowid")

    def __init__(self, ids, bits, q8, scale, max_rowid) -> None:
        self.ids = ids
        self.bits = bits
        self.q8 = q8
        self.scale = scale
        self.max_rowid = max_rowid


class SqliteVecStore(_SqliteConnMixin):
    """sqlite-vec (vec0) adapter mirroring the ChromaStore public interface."""

    def __init__(
        self,
        db_path: str,
        embedding_function: Callable[[list[str]], list[list[float]]],
        dim: int,
        collection_name: str = "vectors_kure",
        ann: str = "",
        ann_coarse_k: int = 512,
    ) -> None:
        """
        Parameters
        ----------
        db_path:
            Path to the SQLite file holding the vec0 table (e.g.
            ``<LOCAL_DATA_DIR>/vectors.db``). Kept separate from doc_store.db so
            the vector store stays independently swappable.
        embedding_function:
            App-side embedding callable ``(list[str]) -> list[list[float]]``
            (ResilientEmbeddingFunction / KURE). REQUIRED — unlike Chroma there
            is no internal EF.
        dim:
            Vector dimension (KURE = 1024). The vec0 table is declared
            ``float[dim]``; writes with a mismatched length are rejected.
        collection_name:
            vec0 table name.
        ann:
            ``""`` (default, off — exact brute-force only, 기존 동작 불변) or
            ``"binary"`` (2-stage bit-hamming coarse + float-cosine rerank for
            GLOBAL queries; §3.7). ``"binary"`` requires the table to have the
            ``embedding_bit`` column (run scripts/migrate_add_binary_quantization.py
            on an existing DB; new/empty DBs get it at CREATE). Missing column →
            warning + silent fallback to the exact path.
        ann_coarse_k:
            Coarse candidate count C for the binary 2-stage path (recall knob;
            higher = closer to exact, slower). Clamped to vec0's k cap (4096).
        """
        if embedding_function is None:
            raise ValueError("SqliteVecStore requires an embedding_function.")
        if not _IDENT_RE.match(collection_name):
            raise ValueError(f"Unsafe collection_name: {collection_name!r}")
        if ann not in ("", "binary"):
            raise ValueError(f"Unknown ann mode: {ann!r} (valid: '', 'binary')")
        self._ef = embedding_function
        self._dim = int(dim)
        self._table = collection_name
        self._ann = ann
        self._ann_coarse_k = int(ann_coarse_k)
        # Whether the ACTUAL table schema has the embedding_bit column — set in
        # _init_db from PRAGMA table_info. Drives the write path independently
        # of `ann` (vec0 requires a value for every vector column on INSERT).
        self._has_bit_column = False
        # In-process ANN cache (§3.7). Built lazily on the first global ANN
        # query; invalidated by this store's writes and by freshness checks.
        self._ann_cache: _AnnCache | None = None
        self._ann_cache_lock = threading.Lock()
        self._conn_dv: dict[int, int] = {}
        self._available = False
        self._init_conn_state(db_path)
        self._init_db()

    # ------------------------------------------------------------------
    # Connection / lifecycle
    # ------------------------------------------------------------------

    def _configure_connection(self, conn: Any) -> None:
        """sqlite-vec extension load + cross-process writer tolerance.

        Runs after the mixin's WAL/synchronous pragmas (the original loaded
        the extension before them) — inert reorder, extension loading and
        journal-mode/sync pragmas are independent SQLite session settings."""
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        # Cross-process writers (e.g. an offline loader writing vectors.db while
        # serve also writes) wait up to 5s for the write lock instead of getting
        # SQLITE_BUSY immediately. (Python's connect(timeout=5.0) default already
        # sets this; made explicit so the WAL multi-writer contract is in-code.)
        conn.execute("PRAGMA busy_timeout=5000")

    def _create_table_sql(self, with_bit: bool = False) -> str:
        # The bit column is declared WITHOUT distance_metric — hamming is the
        # implied (and only) metric for bit vectors; an explicit
        # `distance_metric=hamming` fails vec0's column parser (0.1.9 preflight).
        bit_col = f"embedding_bit bit[{self._dim}], " if with_bit else ""
        return (
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table} USING vec0("
            "node_id TEXT PRIMARY KEY, "
            "pack_id TEXT partition key, "
            f"embedding float[{self._dim}] distance_metric=cosine, "
            f"{bit_col}"
            "+document TEXT, "
            "+metadata TEXT"
            ")"
        )

    def _detect_bit_column(self, conn: Any) -> bool:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({self._table})")]
        return "embedding_bit" in cols

    def _init_db(self) -> None:
        try:
            import os

            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = self._conn
            # New/empty DBs get the bit column at CREATE when ann=binary; an
            # EXISTING float-only table is left untouched (IF NOT EXISTS) — vec0
            # cannot ALTER TABLE ADD COLUMN, so pre-existing DBs are upgraded by
            # scripts/migrate_add_binary_quantization.py (rebuild pattern).
            conn.execute(self._create_table_sql(with_bit=(self._ann == "binary")))
            conn.commit()
            # Gate the write path on the ACTUAL schema, not the config flag:
            # once embedding_bit exists vec0 rejects INSERTs without it, and a
            # never-migrated DB must keep the original INSERT byte-for-byte.
            self._has_bit_column = self._detect_bit_column(conn)
            if self._ann == "binary" and not self._has_bit_column:
                logger.warning(
                    "SqliteVecStore: ann='binary' requested but table '%s' has "
                    "no embedding_bit column — falling back to exact search. "
                    "Run scripts/migrate_add_binary_quantization.py to backfill.",
                    self._table,
                )
            self._available = True
            logger.info(
                "SqliteVecStore initialised at %s (table=%s, dim=%d, ann=%s, "
                "bit_column=%s)",
                self._db_path,
                self._table,
                self._dim,
                self._ann or "off",
                self._has_bit_column,
            )
        except Exception as exc:  # pragma: no cover - init failure path
            logger.warning("SqliteVecStore init failed: %s", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return embed_and_validate(self._ef, self._dim, texts)

    def _serialize(self, vec: list[float]) -> Any:
        import sqlite_vec

        return sqlite_vec.serialize_float32(vec)

    def _insert_sql(self) -> str:
        """INSERT statement matching the actual table schema. When the table
        has the bit column the value MUST be provided (vec0 rejects NULL vector
        columns) and MUST be wrapped in vec_bit(?) — raw packed bytes whose
        length is divisible by 4 would be misread as a float32 vector."""
        if self._has_bit_column:
            return (
                f"INSERT INTO {self._table}"
                "(node_id, pack_id, embedding, embedding_bit, document, metadata)"
                " VALUES (?, ?, ?, vec_bit(?), ?, ?)"
            )
        return (
            f"INSERT INTO {self._table}"
            "(node_id, pack_id, embedding, document, metadata)"
            " VALUES (?, ?, ?, ?, ?)"
        )

    def _insert_params(
        self, _id: str, text: str, meta: dict[str, Any], vec: list[float]
    ) -> tuple:
        if self._has_bit_column:
            return (
                _id,
                str(meta.get("pack_id", "")),
                self._serialize(vec),
                _sign_bits(vec),
                text,
                json.dumps(meta),
            )
        return (
            _id,
            str(meta.get("pack_id", "")),
            self._serialize(vec),
            text,
            json.dumps(meta),
        )

    def add_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Add text chunks (content+time hash IDs when omitted).

        NOTE: unlike ChromaStore.add_texts (which warns and skips duplicate ids
        without raising), this is a plain vec0 INSERT and raises on a duplicate
        primary key — vec0 supports neither INSERT OR IGNORE nor UPSERT. Callers
        that may re-add ids should use upsert_texts (builder.py uses upsert)."""
        self._require_available()
        if not texts:
            return []
        if ids is None:
            ids = generate_add_ids(texts)
        metadatas = default_metadatas(texts, metadatas)
        validate_lengths(texts, metadatas, ids)
        clean_meta = [_sanitize_metadata(m) for m in metadatas]
        vectors = self._embed(texts)
        insert_sql = self._insert_sql()
        with self._tx() as conn:
            for _id, text, meta, vec in zip(ids, texts, clean_meta, vectors):
                conn.execute(insert_sql, self._insert_params(_id, text, meta, vec))
        self._ann_cache = None  # in-process write → invalidate ANN cache
        return ids

    def upsert_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Upsert (content-deterministic IDs when omitted). vec0 has no UPSERT
        for virtual tables, so this is DELETE-then-INSERT per id."""
        self._require_available()
        if not texts:
            return []
        if ids is None:
            ids = generate_upsert_ids(texts)
        metadatas = default_metadatas(texts, metadatas)
        validate_lengths(texts, metadatas, ids)
        clean_meta = [_sanitize_metadata(m) for m in metadatas]
        vectors = self._embed(texts)
        insert_sql = self._insert_sql()
        with self._tx() as conn:
            for _id, text, meta, vec in zip(ids, texts, clean_meta, vectors):
                conn.execute(f"DELETE FROM {self._table} WHERE node_id = ?", (_id,))
                conn.execute(insert_sql, self._insert_params(_id, text, meta, vec))
        self._ann_cache = None  # in-process write → invalidate ANN cache
        return ids

    def delete(self, ids: list[str]) -> None:
        self._require_available()
        if not ids:
            return
        with self._tx() as conn:
            for _id in ids:
                conn.execute(f"DELETE FROM {self._table} WHERE node_id = ?", (_id,))
        self._ann_cache = None  # in-process write → invalidate ANN cache

    def reset_collection(self) -> None:
        """Empty the collection (destructive). Uses DELETE (not DROP+CREATE) so
        the table always exists: concurrent readers never observe a "no such
        table" gap, and the write lock serialises concurrent resets. Same
        dim/schema is retained (dim is fixed at construction)."""
        self._require_available()
        with self._tx() as conn:
            # ensure the table exists (idempotent, schema-preserving) then
            # clear all rows atomically
            conn.execute(self._create_table_sql(with_bit=self._has_bit_column))
            conn.execute(f"DELETE FROM {self._table}")
        self._ann_cache = None  # in-process write → invalidate ANN cache
        logger.info("SqliteVecStore: table '%s' reset.", self._table)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic KNN search. Returns dicts with keys id/document/metadata/
        distance (cosine distance = 1 - cos), matching ChromaStore.query."""
        self._require_available()
        if n_results <= 0:
            return []
        qvec = self._embed([query_text])[0]

        predicate = _build_predicate(where)          # full post-filter (or None)
        pack_values = _extract_pack_values(where)    # pushdown targets (or None)
        # fully pushed down only if structurally pack-only AND the pack clause is
        # actually pushable (eq/$in). Otherwise a residual post-filter is needed.
        pack_only = _is_pack_only(where) and pack_values is not None

        # No filter / fully pushed-down pack filter → k = n_results is exact.
        # Residual (non-pack) post-filter → scan up to vec0's k cap for best-effort
        # recall: the residual field is filtered in Python, so matches beyond the
        # 4096 nearest cannot be recovered (a hard vec0 k limit). Localcrab only
        # emits pack (exact, pushed down) and space filters; space is only written
        # to vector metadata starting with builder.py's #51 fix, so vectors ingested
        # before that fix still have no "space" key and match nothing here (missing
        # key = _MISSING = no match, replicating Chroma's missing-key semantics) —
        # query.py surfaces a transitional warning for this until a backfill runs.
        # The residual path itself is a correctness safety net, not a hot path.
        if predicate is None or pack_only:
            fetch_k = min(max(int(n_results), 1), _VEC0_K_MAX)
        else:
            fetch_k = _VEC0_K_MAX

        rows = self._select_query_mode(
            qvec, pack_values, predicate, fetch_k, n_results
        )

        hits: list[dict[str, Any]] = []
        for row in rows:
            if predicate is not None and not predicate(row["metadata"]):
                continue
            hits.append(row)
            if len(hits) >= n_results:
                break
        return hits

    def _knn(
        self, qvec: list[float], k: int, pack: str | None
    ) -> list[dict[str, Any]]:
        """Single vec0 KNN. `k` is assumed already clamped to _VEC0_K_MAX."""
        sql = (
            f"SELECT node_id, distance, document, metadata FROM {self._table}"
            " WHERE embedding MATCH ? AND k = ?"
        )
        params: list[Any] = [self._serialize(qvec), k]
        if pack is not None:
            sql += " AND pack_id = ?"
            params.append(pack)
        sql += " ORDER BY distance"
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params).fetchall():
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            out.append(
                {
                    "id": row["node_id"],
                    "document": row["document"],
                    "metadata": meta,
                    "distance": float(row["distance"]),
                }
            )
        return out

    def _select_query_mode(
        self,
        qvec: list[float],
        pack_values: list[str] | None,
        predicate: Callable[[dict[str, Any]], bool] | None,
        fetch_k: int,
        n_results: int,
    ) -> list[dict[str, Any]]:
        """Pick which KNN strategy serves this query, and run it.

        - ``pack_values`` present → one partition-pushed KNN per pack, merged
          (exact within each pack, so the global top-n across packs is exact).
          Pack-scoped search stays EXACT even under ``ann="binary"`` — the
          partition pre-filter already makes it fast (~8ms measured), so
          §3.7 keeps exact as the safe default here.
        - no pack constraint, ``ann="binary"`` eligible (bit column present)
          and no residual filter → PURE-GLOBAL binary 2-stage ANN (§3.7):
          in-RAM bit-hamming coarse → int8 rerank → exact float refinement of
          the top ~3n. This is the 868ms hot path being accelerated. Queries
          WITH residual (non-pack) filters fall back to the exact scan so the
          Python post-filter keeps its full 4096-candidate pool (unchanged
          semantics). On any cache failure the helper returns None → exact
          fallback.
        - otherwise → plain exact brute-force KNN.
        """
        if pack_values:
            rows: list[dict[str, Any]] = []
            for pk in pack_values:
                rows.extend(self._knn(qvec, fetch_k, pack=pk))
            rows.sort(key=lambda r: r["distance"])
            return rows
        if self._ann == "binary" and self._has_bit_column and predicate is None:
            rows_ann = self._knn_bit_rerank(
                qvec, max(self._ann_coarse_k, fetch_k), n_results
            )
            if rows_ann is not None:
                return rows_ann
        return self._knn(qvec, fetch_k, pack=None)

    # ------------------------------------------------------------------
    # Binary 2-stage ANN (§3.7) — in-process cache path
    # ------------------------------------------------------------------

    def _ann_cache_fresh(self, cache: _AnnCache) -> bool:
        """Cheap (O(1)) freshness checks against out-of-band writers.

        - ``PRAGMA data_version`` on the CURRENT connection increments when
          ANOTHER connection commits — compared per-connection (values are not
          comparable across connections).
        - ``max(rowid)`` on the ``{table}_rowids`` shadow table catches
          appends/upserts even on a connection that has no recorded
          data_version yet (every insert gets a fresh monotonic rowid; upsert
          is delete+insert). In-process writes through this store invalidate
          explicitly (``self._ann_cache = None``), so the only theoretical
          miss is an external delete-only change observed by a brand-new
          connection — stale candidates until the next detected change.
        """
        conn = self._conn
        dv = conn.execute("PRAGMA data_version").fetchone()[0]
        key = id(conn)
        last = self._conn_dv.get(key)
        self._conn_dv[key] = dv
        if last is not None and dv != last:
            return False
        max_rowid = conn.execute(
            f"SELECT max(rowid) FROM {self._table}_rowids"
        ).fetchone()[0]
        return max_rowid == cache.max_rowid

    def _quantize_rows(self, sub: Any) -> tuple[Any, Any, Any]:
        """Derive sign-bit packing + symmetric int8 quantization for a batch of
        raw float vectors (rows x dim). Split out of :meth:`_build_ann_cache`'s
        per-chunk loop — pure numpy math, no I/O, no cache-object mutation."""
        import numpy as np

        bits_sub = np.packbits(sub > 0, axis=1, bitorder="little")
        scale_sub = np.abs(sub).max(axis=1) / 127.0
        scale_sub[scale_sub == 0] = 1.0
        q8_sub = np.clip(np.round(sub / scale_sub[:, None]), -127, 127).astype(
            np.int8
        )
        return bits_sub, q8_sub, scale_sub

    def _build_ann_cache(self) -> _AnnCache | None:
        """Build the in-RAM ANN cache by reading vec0's shadow tables directly.

        vec0 stores float vectors as ~4MB chunk BLOBs in
        ``{table}_vector_chunks00`` (first vector column = ``embedding``; our
        DDL fixes the column order) and maps node_id → (chunk_id, chunk_offset)
        in ``{table}_rowids``. Reading chunks directly costs ~1.5s for 179k —
        the vtab full scan costs ~97s (per-row chunk materialisation), which is
        why the public path is bypassed here. Layout verified on 0.1.9;
        any mismatch raises and the caller falls back to exact search.

        Derives per row: sign bits (== vec_quantize_binary, see _sign_bits) and
        symmetric int8 quantization (scale = max|v|/127). Peak transient memory
        ≈ one chunk (~4MB) + the cache itself (~210MB at 179k×1024d).
        """
        import numpy as np

        conn = self._conn
        maps = conn.execute(
            f"SELECT rowid, id, chunk_id, chunk_offset FROM {self._table}_rowids"
            " ORDER BY rowid"
        ).fetchall()
        n = len(maps)
        if n == 0:
            return _AnnCache(ids=[], bits=None, q8=None, scale=None,
                             max_rowid=None)
        ids: list[str] = [m[1] for m in maps]
        max_rowid = maps[-1][0]
        # group output positions by chunk
        by_chunk: dict[int, list[tuple[int, int]]] = {}
        for pos, (_rid, _nid, cid, off) in enumerate(maps):
            by_chunk.setdefault(cid, []).append((pos, off))

        bits = np.empty((n, self._dim // 8), dtype=np.uint8)
        q8 = np.empty((n, self._dim), dtype=np.int8)
        scale = np.empty(n, dtype=np.float32)
        for cid, entries in by_chunk.items():
            blob = conn.execute(
                f"SELECT vectors FROM {self._table}_vector_chunks00"
                " WHERE rowid = ?",
                (cid,),
            ).fetchone()
            if blob is None:
                raise RuntimeError(f"ANN cache: missing vector chunk {cid}")
            arr = np.frombuffer(blob[0], dtype=np.float32)
            if arr.size % self._dim != 0:
                raise RuntimeError(
                    f"ANN cache: chunk {cid} size {arr.size} not divisible by "
                    f"dim {self._dim}"
                )
            arr = arr.reshape(-1, self._dim)
            pos_idx = np.fromiter((e[0] for e in entries), dtype=np.int64)
            off_idx = np.fromiter((e[1] for e in entries), dtype=np.int64)
            sub = arr[off_idx]
            bits_sub, q8_sub, scale_sub = self._quantize_rows(sub)
            bits[pos_idx] = bits_sub
            q8[pos_idx] = q8_sub
            scale[pos_idx] = scale_sub
        # uint64 view speeds XOR+popcount ~8× when the row width allows it
        if bits.shape[1] % 8 == 0:
            bits = bits.view(np.uint64)
        return _AnnCache(ids=ids, bits=bits, q8=q8, scale=scale,
                         max_rowid=max_rowid)

    def _get_ann_cache(self) -> _AnnCache | None:
        cache = self._ann_cache
        if cache is not None and self._ann_cache_fresh(cache):
            return cache
        with self._ann_cache_lock:
            cache = self._ann_cache
            if cache is not None and self._ann_cache_fresh(cache):
                return cache
            t0 = time.perf_counter()
            cache = self._build_ann_cache()
            self._ann_cache = cache
            if cache is not None:
                logger.info(
                    "SqliteVecStore: ANN cache built — %d vectors in %.1fs",
                    len(cache.ids),
                    time.perf_counter() - t0,
                )
            return cache

    def _knn_bit_rerank(
        self, qvec: list[float], coarse_k: int, n_results: int
    ) -> list[dict[str, Any]] | None:
        """Binary 2-stage global KNN (§3.7) over the in-process cache.

        1. coarse : hamming(sign(qvec), bit matrix) via numpy XOR+bitwise_count
           → top ``coarse_k`` candidates (~16ms at 179k).
        2. rerank : int8-dequantized cosine over the C candidates (RAM, ~5ms),
           keep the top R = max(3n, n+20).
        3. refine : EXACT float cosine + document/metadata for those R rows via
           vec0 point queries (~0.76ms each → ~23ms at R=30). Returned
           distances are exact — same contract as :meth:`_knn`.

        Returns None on any cache/layout failure → caller uses the exact path.
        Recall is tuned by ``coarse_k`` (C): candidates the coarse stage misses
        cannot be recovered; C → corpus size converges to exact. int8 rerank
        displacement is absorbed by the 3n refinement pool (measured: exact
        top-10 stays within the int8 top-30 on real data).
        """
        try:
            cache = self._get_ann_cache()
        except Exception as exc:
            logger.warning(
                "SqliteVecStore: ANN cache unavailable (%s) — falling back "
                "to exact search.",
                exc,
            )
            self._ann_cache = None
            return None
        if cache is None:
            return None
        if len(cache.ids) == 0:
            return []

        cand = self._ann_coarse_candidates(cache, qvec, coarse_k)
        refine_ids = self._ann_rerank_candidates(cache, cand, qvec, n_results)
        return self._ann_refine_exact(qvec, refine_ids)

    def _ann_coarse_candidates(
        self, cache: _AnnCache, qvec: list[float], coarse_k: int
    ) -> Any:
        """Stage 1 (coarse): hamming(sign(qvec), bit matrix) via numpy
        XOR+bitwise_count → row-indices into ``cache`` of the top
        ``coarse_k`` candidates (~16ms at 179k)."""
        import numpy as np

        qbits = np.frombuffer(_sign_bits(qvec), dtype=np.uint8)
        if cache.bits.dtype == np.uint64:
            qbits = qbits.view(np.uint64)
        ham = np.bitwise_count(cache.bits ^ qbits).sum(axis=1, dtype=np.uint32)

        n = len(cache.ids)
        c = min(max(int(coarse_k), 1), n)
        if c >= n:
            return np.arange(n)
        return np.argpartition(ham, c - 1)[:c]

    def _ann_rerank_candidates(
        self, cache: _AnnCache, cand: Any, qvec: list[float], n_results: int
    ) -> list[str]:
        """Stage 2 (rerank): int8-dequantized cosine over the coarse
        candidates (RAM, ~5ms) → node ids of the top R = max(3n, n+20)
        refinement pool."""
        import numpy as np

        q = np.asarray(qvec, dtype=np.float32)
        sub = cache.q8[cand].astype(np.float32) * cache.scale[cand, None]
        denom = np.linalg.norm(sub, axis=1) * (np.linalg.norm(q) or 1.0)
        denom[denom == 0] = 1.0
        sims = (sub @ q) / denom
        r = min(len(cand), max(3 * int(n_results), int(n_results) + 20))
        top_local = (
            np.argpartition(-sims, r - 1)[:r] if r < len(cand) else np.arange(len(cand))
        )
        return [cache.ids[i] for i in cand[top_local]]

    def _ann_refine_exact(
        self, qvec: list[float], refine_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Stage 3 (refine): EXACT float cosine + document/metadata for the
        refinement pool via vec0 point queries (~0.76ms each → ~23ms at
        R=30). Returned distances are exact — same contract as :meth:`_knn`."""
        qser = self._serialize(qvec)
        scored: list[dict[str, Any]] = []
        for nid in refine_ids:
            row = self._conn.execute(
                f"SELECT node_id, document, metadata,"
                f" vec_distance_cosine(embedding, ?) AS distance"
                f" FROM {self._table} WHERE node_id = ?",
                (qser, nid),
            ).fetchone()
            if row is None:  # deleted concurrently
                continue
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            scored.append(
                {
                    "id": row["node_id"],
                    "document": row["document"],
                    "metadata": meta,
                    "distance": float(row["distance"]),
                }
            )
        scored.sort(key=lambda x: x["distance"])
        return scored

    def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        self._require_available()
        row = self._conn.execute(
            f"SELECT node_id, document, metadata FROM {self._table}"
            " WHERE node_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["node_id"],
            "document": row["document"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
        }

    def count(self) -> int:
        if not self._available:
            return 0
        return int(
            self._conn.execute(f"SELECT count(*) FROM {self._table}").fetchone()[0]
        )


# ---------------------------------------------------------------------------
# where-clause parity helpers (replicate Chroma `where` semantics in Python)
# ---------------------------------------------------------------------------


def _extract_pack_values(where: dict[str, Any] | None) -> list[str] | None:
    """Extract pack_id values to push down to the vec0 partition key — from a
    flat ``{"pack_id": scalar}``, ``{"pack_id": {"$in": [...]}}``, or the
    ``pack_id`` clause inside ``{"$and": [...]}``. Returns None when there is no
    pushable pack constraint (the post-filter then handles any pack condition)."""
    if not where:
        return None
    clause: dict[str, Any] | None = None
    if "pack_id" in where:
        clause = where
    elif "$and" in where and isinstance(where["$and"], list):
        for c in where["$and"]:
            if isinstance(c, dict) and "pack_id" in c:
                clause = c
                break
    if clause is None:
        return None
    cond = clause["pack_id"]
    if isinstance(cond, dict):
        vals = cond.get("$in")
        if isinstance(vals, list) and vals:
            # dedup (preserve order) so a repeated pack does not scan its
            # partition twice and emit duplicate result rows.
            return list(dict.fromkeys(str(v) for v in vals))
        return None  # non-$in operator on pack_id → leave to post-filter
    return [str(cond)]


def _is_pack_only(where: dict[str, Any] | None) -> bool:
    """True when the entire filter is structurally pack_id-only (so, combined
    with a non-None _extract_pack_values, the partition pushdown fully satisfies
    it and no residual post-filter is required)."""
    if not where:
        return True
    keys = set(where.keys())
    if keys == {"pack_id"}:
        return True
    if keys == {"$and"} and isinstance(where["$and"], list):
        return all(
            isinstance(c, dict) and set(c.keys()) == {"pack_id"}
            for c in where["$and"]
        )
    return False


def _build_predicate(
    where: dict[str, Any] | None,
) -> Callable[[dict[str, Any]], bool] | None:
    """Compile a Chroma ``where`` dict into a metadata predicate.

    Supports the operators localcrab actually emits (_build_chroma_where):
    flat ``{field: scalar}`` equality, ``{field: {"$in": [...]}}`` membership,
    ``{"$and": [...]}`` / ``{"$or": [...]}`` composition, plus ``$eq``/``$ne``.
    A missing metadata key never matches an equality/membership (Chroma
    semantics). Returns None when ``where`` is empty (no filtering)."""
    if not where:
        return None

    def match(meta: dict[str, Any]) -> bool:
        return _eval_where(where, meta)

    return match


def _eval_where(clause: dict[str, Any], meta: dict[str, Any]) -> bool:
    for key, cond in clause.items():
        if key == "$and":
            if not all(_eval_where(sub, meta) for sub in cond):
                return False
        elif key == "$or":
            if not any(_eval_where(sub, meta) for sub in cond):
                return False
        else:
            if not _eval_field(meta.get(key, _MISSING), cond):
                return False
    return True


_MISSING = object()


def _eval_field(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict):
        for op, operand in cond.items():
            if op == "$in":
                if value is _MISSING or value not in operand:
                    return False
            elif op == "$nin":
                if value is _MISSING or value in operand:
                    return False
            elif op == "$eq":
                if value is _MISSING or value != operand:
                    return False
            elif op == "$ne":
                if value is _MISSING or value == operand:
                    return False
            elif op == "$gt":
                if value is _MISSING or not value > operand:
                    return False
            elif op == "$gte":
                if value is _MISSING or not value >= operand:
                    return False
            elif op == "$lt":
                if value is _MISSING or not value < operand:
                    return False
            elif op == "$lte":
                if value is _MISSING or not value <= operand:
                    return False
            else:  # unknown operator → conservative no-match
                return False
        return True
    # scalar equality
    return value is not _MISSING and value == cond
