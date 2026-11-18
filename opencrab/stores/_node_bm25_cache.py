"""In-process cache of term statistics and posting arrays for the node BM25 search (#667).

Design: docs/issue-667-bm25-posting-cache.md. The cache holds values that the search reads
from the ``fts5vocab`` tables. A value is valid for one database version. The version is the
``PRAGMA data_version`` of a dedicated connection that only reads (design 5.2). A session is bound
to the version of one query snapshot and reads or writes only while that version is the
current generation. ``PostingCache`` and ``Session`` never open SQLite. ``TokenSource`` opens
the one connection and never writes through it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable

import numpy as np

MAX_BYTES = 64 * 1024 * 1024
MAX_STATS = 4096
SWITCH = "OPENCRAB_BM25_CACHE"


def enabled() -> bool:
    """False when the switch is ``0``. The store reads it once, when it is made."""
    return os.getenv(SWITCH, "1") != "0"


class _Generation:
    def __init__(self, token: Hashable, stamp: int) -> None:
        self.token = token
        self.stamp = stamp
        self.stats: dict[str, tuple[int, int]] = {}
        self.postings: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self.bytes = 0


class PostingCache:
    """One cache per store. Every method holds the lock for its whole check and change."""

    def __init__(self, max_bytes: int = MAX_BYTES, max_stats: int = MAX_STATS) -> None:
        self._lock = threading.Lock()
        self._gen: _Generation | None = None
        self._max_bytes = max_bytes
        self._max_stats = max_stats
        # Totals over the life of the cache. Tests read them.
        self.counts = {"stats_hits": 0, "postings_hits": 0, "stores": 0}

    def open_session(self, token: Hashable | None, stamp: int) -> Session | None:
        """Bind a session to ``token``, the version of the caller's snapshot.

        A session whose snapshot is older than the current generation gets nothing, so a slow
        query cannot replace a newer generation (design 5.2, step 4).
        """
        if token is None:
            return None
        with self._lock:
            gen = self._gen
            if gen is None or gen.token != token:
                if gen is not None and gen.stamp > stamp:
                    return None
                self._gen = _Generation(token, stamp)
        return Session(self, token)

    def _current(self, token: Hashable) -> _Generation | None:
        gen = self._gen
        return gen if gen is not None and gen.token == token else None

    def _stats_get(self, token: Hashable, term: str) -> tuple[int, int] | None:
        with self._lock:
            gen = self._current(token)
            found = gen.stats.get(term) if gen is not None else None
            if found is not None:
                self.counts["stats_hits"] += 1
            return found

    def _stats_put(self, token: Hashable, term: str, df: int, cf: int) -> bool:
        with self._lock:
            gen = self._current(token)
            if gen is None or (term not in gen.stats and len(gen.stats) >= self._max_stats):
                return False
            gen.stats[term] = (df, cf)
            self.counts["stores"] += 1
            return True

    def _postings_has(self, token: Hashable, term: str) -> bool:
        with self._lock:
            gen = self._current(token)
            return gen is not None and term in gen.postings

    def _postings_get(self, token: Hashable, term: str) -> tuple[np.ndarray, np.ndarray] | None:
        with self._lock:
            gen = self._current(token)
            found = gen.postings.get(term) if gen is not None else None
            if found is not None:
                gen.postings.move_to_end(term)
                self.counts["postings_hits"] += 1
            return found

    def _postings_put(self, token: Hashable, term: str, keys: np.ndarray, tf: np.ndarray) -> bool:
        size = int(keys.nbytes + tf.nbytes)
        if size > self._max_bytes:
            return False
        with self._lock:
            gen = self._current(token)
            if gen is None:
                return False
            old = gen.postings.pop(term, None)
            if old is not None:
                gen.bytes -= int(old[0].nbytes + old[1].nbytes)
            while gen.postings and gen.bytes + size > self._max_bytes:
                _term, (old_keys, old_tf) = gen.postings.popitem(last=False)
                gen.bytes -= int(old_keys.nbytes + old_tf.nbytes)
            gen.postings[term] = (keys, tf)
            gen.bytes += size
            self.counts["stores"] += 1
            return True


class Session:
    """The cache as one query sees it: valid only while the generation has this token."""

    def __init__(self, cache: PostingCache, token: Hashable) -> None:
        self._cache = cache
        self._token = token

    def stats_get(self, term: str) -> tuple[int, int] | None:
        return self._cache._stats_get(self._token, term)

    def stats_put(self, term: str, df: int, cf: int) -> bool:
        return self._cache._stats_put(self._token, term, df, cf)

    def has_postings(self, term: str) -> bool:
        return self._cache._postings_has(self._token, term)

    def postings_get(self, term: str) -> tuple[np.ndarray, np.ndarray] | None:
        return self._cache._postings_get(self._token, term)

    def postings_put(self, term: str, keys: np.ndarray, tf: np.ndarray) -> bool:
        """Take the two arrays. They become read-only; the caller must not change them."""
        keys, tf = _frozen(keys), _frozen(tf)
        return self._cache._postings_put(self._token, term, keys, tf)


def _frozen(array: np.ndarray) -> np.ndarray:
    if not array.flags.owndata:
        array = array.copy()
    array.setflags(write=False)
    return array


class TokenSource:
    """The version token of one database file, read on a dedicated connection that only reads.

    ``None`` means "do not cache": a closed source, a failed read, or a file that is no longer
    the one opened. A replaced file needs a restart and the source stays off (design 5.2).
    """

    def __init__(self, path: str, connect: Callable[[], sqlite3.Connection]) -> None:
        """Open the connection now, not at the first query. A connection opened after a file
        replacement would adopt the new file and hide the replacement. A failed open turns the
        source off for good."""
        self._path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ident: tuple[int, int] | None = None
        self._off = False
        self._seq = 0
        try:
            before = os.stat(self._path)
            conn = connect()
            after = os.stat(self._path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                conn.close()
                raise OSError("database file replaced while opening")
            self._conn = conn
            self._ident = (after.st_dev, after.st_ino)
        except (sqlite3.Error, OSError):
            self._off = True

    def __call__(self) -> Hashable | None:
        return self.read()[0]

    def read(self) -> tuple[Hashable | None, int]:
        """The token and a sequence number. Both come from one locked step, so a larger number
        is a later read of the database version."""
        with self._lock:
            self._seq += 1
            return self._read_locked(), self._seq

    def _read_locked(self) -> Hashable | None:
        conn = self._conn
        if self._off or conn is None:
            return None
        try:
            st = os.stat(self._path)
            ident = (st.st_dev, st.st_ino)
            if ident != self._ident:
                self._off = True  # latch: a file that comes back does not switch the cache on
                return None
            rows = conn.execute("PRAGMA data_version").fetchall()
            return (ident, int(rows[0][0]))
        except (sqlite3.Error, OSError):
            return None

    def close(self) -> None:
        with self._lock:
            self._off = True
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


class Scope:
    """What ``node_bm25_search`` knows before ``BEGIN``: the cache and the token read then.

    ``open`` reads the token again after the snapshot is pinned. Equal tokens mean no commit
    happened in between, so the snapshot has the version ``pre`` (design 5.2).
    """

    def __init__(self, cache: PostingCache, token_fn: Callable[[], Hashable | None],
                 pre: Hashable | None, stamp: int) -> None:
        self._cache = cache
        self._token_fn = token_fn
        self._pre = pre
        self._stamp = stamp

    @classmethod
    def before_begin(cls, cache: PostingCache, source: TokenSource) -> Scope:
        pre, stamp = source.read()
        return cls(cache, source, pre, stamp)

    def open(self) -> Session | None:
        if self._pre is None or self._token_fn() != self._pre:
            return None
        return self._cache.open_session(self._pre, self._stamp)


def cached_stats(
    session: Session | None, terms: list[str]
) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Split ``terms`` into the cached ``(df, cf)`` pairs and the terms still to read."""
    found: dict[str, tuple[int, int]] = {}
    missing: list[str] = []
    for term in terms:
        hit = session.stats_get(term) if session is not None else None
        if hit is None:
            missing.append(term)
        else:
            found[term] = hit
    return found, missing
