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
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable

from opencrab.stores.chroma_store import _sanitize_metadata

logger = logging.getLogger(__name__)

# vec0 hard-caps the KNN `k` parameter at 4096 (0.1.9). Any query MUST clamp
# fetch_k to this or it raises OperationalError. Pack constraints are pushed
# down to the partition key so the common filters stay exact at any scale; only
# residual constraints (e.g. `space`) fall back to a bounded post-filter.
_VEC0_K_MAX = 4096

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqliteVecStore:
    """sqlite-vec (vec0) adapter mirroring the ChromaStore public interface."""

    def __init__(
        self,
        db_path: str,
        embedding_function: Callable[[list[str]], list[list[float]]],
        dim: int,
        collection_name: str = "vectors_kure",
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
        """
        if embedding_function is None:
            raise ValueError("SqliteVecStore requires an embedding_function.")
        if not _IDENT_RE.match(collection_name):
            raise ValueError(f"Unsafe collection_name: {collection_name!r}")
        self._db_path = db_path
        self._ef = embedding_function
        self._dim = int(dim)
        self._table = collection_name
        self._available = False
        self._lock = threading.Lock()
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[Any] = []
        self._init_db()

    # ------------------------------------------------------------------
    # Connection / lifecycle
    # ------------------------------------------------------------------

    def _new_conn(self) -> Any:
        """Per-thread connection with WAL + sqlite-vec extension loaded.

        WAL + synchronous=NORMAL mirrors local_sql_doc_store.py (reader/writer
        isolation, fsync only at checkpoint)."""
        import sqlite3

        import sqlite_vec

        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Cross-process writers (e.g. an offline loader writing vectors.db while
        # serve also writes) wait up to 5s for the write lock instead of getting
        # SQLITE_BUSY immediately. (Python's connect(timeout=5.0) default already
        # sets this; made explicit so the WAL multi-writer contract is in-code.)
        conn.execute("PRAGMA busy_timeout=5000")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def _create_table_sql(self) -> str:
        return (
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table} USING vec0("
            "node_id TEXT PRIMARY KEY, "
            "pack_id TEXT partition key, "
            f"embedding float[{self._dim}] distance_metric=cosine, "
            "+document TEXT, "
            "+metadata TEXT"
            ")"
        )

    def _init_db(self) -> None:
        try:
            import os

            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = self._conn
            conn.execute(self._create_table_sql())
            conn.commit()
            self._available = True
            logger.info(
                "SqliteVecStore initialised at %s (table=%s, dim=%d)",
                self._db_path,
                self._table,
                self._dim,
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

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._ef(list(texts))
        for vec in vectors:
            if len(vec) != self._dim:
                raise RuntimeError(
                    f"Embedding dim {len(vec)} != table dim {self._dim}."
                )
        return vectors

    def _serialize(self, vec: list[float]) -> Any:
        import sqlite_vec

        return sqlite_vec.serialize_float32(vec)

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
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
        if not texts:
            return []
        if ids is None:
            ids = [
                hashlib.sha256(f"{t}{time.time_ns()}".encode()).hexdigest()[:16]
                for t in texts
            ]
        if metadatas is None:
            metadatas = [{} for _ in texts]
        clean_meta = [_sanitize_metadata(m) for m in metadatas]
        vectors = self._embed(texts)
        with self._lock:
            for _id, text, meta, vec in zip(ids, texts, clean_meta, vectors):
                self._conn.execute(
                    f"INSERT INTO {self._table}"
                    "(node_id, pack_id, embedding, document, metadata)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        _id,
                        str(meta.get("pack_id", "")),
                        self._serialize(vec),
                        text,
                        json.dumps(meta),
                    ),
                )
            self._conn.commit()
        return ids

    def upsert_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Upsert (content-deterministic IDs when omitted). vec0 has no UPSERT
        for virtual tables, so this is DELETE-then-INSERT per id."""
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
        if not texts:
            return []
        if ids is None:
            ids = [hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts]
        if metadatas is None:
            metadatas = [{} for _ in texts]
        clean_meta = [_sanitize_metadata(m) for m in metadatas]
        vectors = self._embed(texts)
        with self._lock:
            for _id, text, meta, vec in zip(ids, texts, clean_meta, vectors):
                self._conn.execute(
                    f"DELETE FROM {self._table} WHERE node_id = ?", (_id,)
                )
                self._conn.execute(
                    f"INSERT INTO {self._table}"
                    "(node_id, pack_id, embedding, document, metadata)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        _id,
                        str(meta.get("pack_id", "")),
                        self._serialize(vec),
                        text,
                        json.dumps(meta),
                    ),
                )
            self._conn.commit()
        return ids

    def delete(self, ids: list[str]) -> None:
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
        if not ids:
            return
        with self._lock:
            for _id in ids:
                self._conn.execute(
                    f"DELETE FROM {self._table} WHERE node_id = ?", (_id,)
                )
            self._conn.commit()

    def reset_collection(self) -> None:
        """Drop and recreate the vec0 table (destructive). Serialised under the
        write lock so concurrent resets never race."""
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
        with self._lock:
            self._conn.execute(f"DROP TABLE IF EXISTS {self._table}")
            self._conn.execute(self._create_table_sql())
            self._conn.commit()
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
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
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
        # emits pack (exact, pushed down) and space filters; space is absent from
        # vector metadata so it matches nothing in either backend — the residual
        # path is a correctness safety net, not a hot path.
        if predicate is None or pack_only:
            fetch_k = min(max(int(n_results), 1), _VEC0_K_MAX)
        else:
            fetch_k = _VEC0_K_MAX

        if pack_values:
            # pack_id $in / eq → one partition-pushed KNN per pack, merged. Each
            # is exact within its pack, so the global top-n across packs is exact.
            rows: list[dict[str, Any]] = []
            for pk in pack_values:
                rows.extend(self._knn(qvec, fetch_k, pack=pk))
            rows.sort(key=lambda r: r["distance"])
        else:
            rows = self._knn(qvec, fetch_k, pack=None)

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

    def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        if not self._available:
            raise RuntimeError("SqliteVecStore is not available.")
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
        try:
            return int(
                self._conn.execute(
                    f"SELECT count(*) FROM {self._table}"
                ).fetchone()[0]
            )
        except Exception:
            return 0


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
