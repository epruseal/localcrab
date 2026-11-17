"""Exact top-k pruning for the SQLite node BM25 search (#567).

The design is ``docs/issue-567-bm25-prune.md``. ``select`` returns the scored
candidates that contain the exact top ``limit`` of every group (one group, or
one per pack in per-pack mode), in the order of ``_node_bm25_sqlite.search``.
The caller runs the current-row checks on that list. Scores are computed with
``bm25_score``, so the result equals the full computation.

Term-at-a-time MaxScore: terms are taken in descending upper bound. When the
upper bound of the pending terms falls below the k-th lower bound of a group,
the group takes no new documents. Candidates get the pending terms from a
posting scan or from re-tokenising their current row.

Every function reads inside the caller's read transaction. Nothing is written.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np

from opencrab.ontology.bm25 import _B, _DELTA, _K1, bm25_score

# Entry conditions (design 6.1). FULL_SCAN_CF_MAX only chooses the cheaper path.
FULL_SCAN_CF_MAX = 50_000
PRUNE_MAX_TERMS = 4096

# Cost estimates in microseconds (design 6.3). They choose paths, not results.
SCAN_US = 0.25           # one posting occurrence
RETOK_BASE_US = 50.0     # re-tokenising one document
RETOK_TOKEN_US = 1.0     # plus one stored token
SEED_BUDGET_US = 30_000.0
SEED_MAX = 400

_SQL_CHUNK = 500


class PruneFallbackError(Exception):
    """The index disagrees with an assumption; use the full computation."""


RowToNode = Callable[[Any], dict[str, Any]]


def eligible(n_terms: int, cf_sum: int, per_pack: bool, limit: int,
             idf: dict[str, float]) -> bool:
    """True when ``select`` may run (the caller checks the switch and pending)."""
    if limit <= 0 or n_terms > PRUNE_MAX_TERMS:
        return False
    # Design 6.1: a computed idf of 0 would break the strict upper bound.
    if not idf or not all(v > 0 for v in idf.values()):
        return False
    return per_pack or cf_sum > FULL_SCAN_CF_MAX


def _ints(text: str | None) -> np.ndarray:
    if not text:
        return np.zeros(0, dtype=np.int64)
    return np.fromstring(text, dtype=np.int64, sep=" ")


def _postings(conn: sqlite3.Connection, vinst: str, term: str) -> tuple[np.ndarray, np.ndarray]:
    """(doc keys ascending, tf) of one term from the instance vocabulary."""
    row = conn.execute(
        f"SELECT count(*), group_concat(doc, ' ') FROM {vinst} WHERE term=?", (term,)
    ).fetchone()
    occ = _ints(row[1])
    if occ.size != int(row[0]):
        raise PruneFallbackError("posting text")
    if occ.size == 0:
        return occ, occ
    if np.any(np.diff(occ) < 0):
        raise PruneFallbackError("posting order")
    starts = np.concatenate(([0], np.flatnonzero(np.diff(occ)) + 1))
    tf = np.diff(np.concatenate((starts, [occ.size])))
    return occ[starts], tf


def _lookup(keys_sorted: np.ndarray, values: np.ndarray, keys: np.ndarray) -> np.ndarray:
    if keys.size == 0 or keys_sorted.size == 0:
        return np.zeros(keys.size, dtype=np.int64)
    pos = np.minimum(np.searchsorted(keys_sorted, keys), keys_sorted.size - 1)
    return np.where(keys_sorted[pos] == keys, values[pos], 0)


def _contrib(tf: np.ndarray, dl: np.ndarray, idf_t: float, mult_t: int, avgdl: float) -> np.ndarray:
    """``bm25_score``'s term contribution times the query repeat (design 6.2)."""
    tfm = tf.astype(np.float64)
    dlm = dl.astype(np.float64)
    return mult_t * idf_t * (
        tfm * (_K1 + 1) / (tfm + _K1 * (1 - _B + _B * dlm / max(avgdl, 1))) + _DELTA
    )


def _kth(values: np.ndarray, grp: np.ndarray, groups: int, k: int) -> np.ndarray:
    """k-th largest value per group; -inf for a group with fewer than k values."""
    theta = np.full(groups, -np.inf)
    if values.size == 0:
        return theta
    if groups == 1:
        if values.size >= k:
            theta[0] = np.partition(values, values.size - k)[values.size - k]
        return theta
    order = np.lexsort((-values, grp))
    sorted_grp = grp[order]
    ids = np.arange(groups)
    start = np.searchsorted(sorted_grp, ids, "left")
    count = np.searchsorted(sorted_grp, ids, "right") - start
    full = count >= k
    theta[full] = values[order[start[full] + k - 1]]
    return theta


def _topk_index(values: np.ndarray, grp: np.ndarray, groups: int, k: int) -> np.ndarray:
    """Indices of the k largest values per group (ties broken arbitrarily)."""
    if groups == 1:
        if values.size <= k:
            return np.arange(values.size)
        return np.argpartition(values, values.size - k)[values.size - k:]
    order = np.lexsort((-values, grp))
    sorted_grp = grp[order]
    rank = np.arange(sorted_grp.size) - np.searchsorted(sorted_grp, sorted_grp, "left")
    return order[rank < k]


def _retok_cost(dls: np.ndarray) -> float:
    return float(RETOK_BASE_US * dls.size + RETOK_TOKEN_US * float(dls.sum()))


class _Candidates:
    """Column arrays of the candidate documents (design 6.2)."""

    def __init__(self) -> None:
        empty = np.zeros(0, dtype=np.int64)
        self.keys = empty
        self.grp = empty
        self.dls = empty
        self.part = np.zeros(0, dtype=np.float64)
        self.exact = np.zeros(0, dtype=np.float64)
        self.has_exact = np.zeros(0, dtype=bool)
        self.tfs: dict[str, np.ndarray] = {}
        self.dead = empty

    def lower(self) -> np.ndarray:
        return np.where(self.has_exact, self.exact, self.part)

    def keep(self, mask: np.ndarray) -> None:
        if mask.all():
            return
        self.dead = np.concatenate((self.dead, self.keys[~mask]))
        for name in ("keys", "grp", "dls", "part", "exact", "has_exact"):
            setattr(self, name, getattr(self, name)[mask])
        self.tfs = {term: tf[mask] for term, tf in self.tfs.items()}

    def add(self, keys: np.ndarray, grp: np.ndarray, dls: np.ndarray) -> None:
        n = keys.size
        self.keys = np.concatenate((self.keys, keys))
        self.grp = np.concatenate((self.grp, grp))
        self.dls = np.concatenate((self.dls, dls))
        self.part = np.concatenate((self.part, np.zeros(n)))
        self.exact = np.concatenate((self.exact, np.zeros(n)))
        self.has_exact = np.concatenate((self.has_exact, np.zeros(n, dtype=bool)))
        self.tfs = {
            term: np.concatenate((tf, np.zeros(n, dtype=np.int64))) for term, tf in self.tfs.items()
        }


def select(
    conn: sqlite3.Connection,
    row_to_node: RowToNode,
    *,
    names: dict[str, str],
    q_terms: list[str],
    idf: dict[str, float],
    df: dict[str, int],
    cf: dict[str, int],
    avgdl: float,
    pack_set: list[str],
    spaces: list[str] | None,
    limit: int,
    per_pack: bool,
    max_text_chars: int | None,
    node_terms: Callable[[dict[str, Any], int | None], list[str]],
    node_columns: str,
    stats: dict[str, Any] | None = None,
) -> list[tuple[float, str, str, str, str]]:
    """Return ``(score, updated_at, space, node_id, pack_id)`` in search order.

    ``names`` maps "fts", "vinst" and "docs" to the index table names. The list
    holds every document of each group's exact top ``limit`` and the boundary
    ties (design 6.4). Raises ``PruneFallbackError`` when a consistency check fails.
    """
    fts, vinst, docs = names["fts"], names["vinst"], names["docs"]
    groups = len(pack_set) if per_pack else 1
    pack_index = {p: (i if per_pack else 0) for i, p in enumerate(pack_set)}
    k = limit
    mult = Counter(t for t in q_terms if t in idf)
    ub = {t: mult[t] * idf[t] * (_K1 + 1 + _DELTA) for t in idf}
    # Design 6.2 and 6.4: every compared value is a sum of non-negative terms,
    # so its rounding error is relative to the value. rho covers it many times.
    rho = 2.0 ** -30 * (len(q_terms) + 32)
    pending = sorted(idf, key=lambda t: (-ub[t], t))
    scanned: set[str] = set()
    want = set(idf)
    cand = _Candidates()
    closed = np.zeros(groups, dtype=bool)
    log = stats if stats is not None else {}
    log.update({"postings": 0, "retokenised": 0, "admitted": 0, "terms": len(pending)})

    def retokenise(idx: np.ndarray, must_hold: str | None = None, first_new: int = -1) -> None:
        keys = [int(x) for x in cand.keys[idx]]
        where: dict[int, tuple[str, str]] = {}
        for s in range(0, len(keys), _SQL_CHUNK):
            chunk = keys[s:s + _SQL_CHUNK]
            for key, space, node_id in conn.execute(
                f"SELECT doc_key, space, node_id FROM {docs} "
                f"WHERE doc_key IN ({','.join('?' * len(chunk))})", chunk
            ):
                where[int(key)] = (space, node_id)
        for j, key in zip(idx, keys):
            if key not in where:
                raise PruneFallbackError("docs row")
            row = conn.execute(
                f"SELECT {node_columns} FROM doc_nodes WHERE space=? AND node_id=?", where[key]
            ).fetchone()
            if row is None:
                raise PruneFallbackError("node row")
            terms = node_terms(row_to_node(row), max_text_chars)
            if len(terms) != int(cand.dls[j]):
                raise PruneFallbackError("dl")
            tf_map = Counter(t for t in terms if t in want)
            for term in scanned:
                if tf_map.get(term, 0) != int(cand.tfs[term][j]):
                    raise PruneFallbackError("tf")
            if must_hold is not None and j >= first_new and tf_map.get(must_hold, 0) <= 0:
                raise PruneFallbackError("match without term")
            cand.exact[j] = bm25_score(q_terms, tf_map, idf, int(cand.dls[j]), avgdl)
            cand.has_exact[j] = True
        log["retokenised"] += len(keys)

    def add_tf(term: str, tf: np.ndarray) -> None:
        cand.tfs[term] = tf
        open_rows = (tf > 0) & ~cand.has_exact
        cand.part[open_rows] += _contrib(tf[open_rows], cand.dls[open_rows], idf[term],
                                         mult[term], avgdl)

    while pending:
        rest = math.fsum(ub[t] for t in pending)
        if cand.keys.size:
            top = _topk_index(cand.lower(), cand.grp, groups, k)
            top = top[~cand.has_exact[top]]
            if 0 < top.size <= SEED_MAX and _retok_cost(cand.dls[top]) <= SEED_BUDGET_US:
                retokenise(top)
            theta = _kth(cand.lower(), cand.grp, groups, k)
            upper = np.where(cand.has_exact, cand.exact, cand.part + rest)
            cand.keep(upper * (1 + rho) >= theta[cand.grp] * (1 - rho))
            closed |= theta * (1 - rho) > rest * (1 + rho)
        nonexact = np.flatnonzero(~cand.has_exact)
        if closed.all():
            scan_cost = SCAN_US * sum(cf[t] for t in pending)
            if _retok_cost(cand.dls[nonexact]) <= scan_cost:
                if nonexact.size:
                    retokenise(nonexact)
                break
            term = max(pending, key=lambda t: (ub[t] / max(cf[t], 1), t))
            pending.remove(term)
            p_keys, p_tf = _postings(conn, vinst, term)
            log["postings"] += 1
            scanned.add(term)
            add_tf(term, _lookup(p_keys, p_tf, cand.keys))
            continue
        term = pending.pop(0)
        open_packs = [p for p in pack_set if not closed[pack_index[p]]]
        a_keys, a_dl, a_grp = _match_docs(conn, fts, docs, term, open_packs, pack_index,
                                          spaces, per_pack, df[term])
        new = np.ones(a_keys.size, dtype=bool)
        if cand.keys.size:
            new &= ~np.isin(a_keys, cand.keys)
        if cand.dead.size:
            new &= ~np.isin(a_keys, cand.dead)
        log["admitted"] += int(new.sum())
        if _retok_cost(a_dl[new]) + _retok_cost(cand.dls[nonexact]) < SCAN_US * cf[term]:
            start = cand.keys.size
            cand.add(a_keys[new], a_grp[new], a_dl[new])
            idx = np.concatenate((nonexact, np.arange(start, cand.keys.size)))
            if idx.size:
                retokenise(idx, must_hold=term, first_new=start)
            # Documents taken later cannot hold this term (design 6.4, 1).
            cand.tfs[term] = np.zeros(cand.keys.size, dtype=np.int64)
            continue
        p_keys, p_tf = _postings(conn, vinst, term)
        log["postings"] += 1
        tf_old = _lookup(p_keys, p_tf, cand.keys)
        new_keys = a_keys[new]
        tf_new = _lookup(p_keys, p_tf, new_keys)
        if new_keys.size and not np.all(tf_new > 0):
            raise PruneFallbackError("match outside postings")
        cand.add(new_keys, a_grp[new], a_dl[new])
        scanned.add(term)
        add_tf(term, np.concatenate((tf_old, tf_new)))

    if cand.keys.size == 0:
        return []
    low = cand.lower()
    theta = _kth(low, cand.grp, groups, k)
    chosen = np.flatnonzero(low >= theta[cand.grp] * (1 - 2 * rho))
    log["chosen"] = int(chosen.size)
    scored_idx: list[tuple[float, int]] = []
    for j in chosen:
        if cand.has_exact[j]:
            score = float(cand.exact[j])
        else:
            tf_map = {t: int(tf[j]) for t, tf in cand.tfs.items() if tf[j] > 0}
            score = bm25_score(q_terms, tf_map, idf, int(cand.dls[j]), avgdl)
        if score > 0:
            scored_idx.append((score, int(cand.keys[j])))
    info: dict[int, tuple[str, str, str, str]] = {}
    keys = [key for _score, key in scored_idx]
    for s in range(0, len(keys), _SQL_CHUNK):
        chunk = keys[s:s + _SQL_CHUNK]
        for key, upd, space, node_id, pid in conn.execute(
            f"SELECT doc_key, updated_at, space, node_id, pack_id FROM {docs} "
            f"WHERE doc_key IN ({','.join('?' * len(chunk))})", chunk
        ):
            info[int(key)] = (upd, space, node_id, pid)
    if len(info) != len(keys):
        raise PruneFallbackError("docs info")
    scored = [(score, *info[key]) for score, key in scored_idx]
    # (-score, updated_at DESC, space, node_id): stable sorts, last key first.
    scored.sort(key=lambda c: (c[2], c[3]))
    scored.sort(key=lambda c: c[1], reverse=True)
    scored.sort(key=lambda c: -c[0])
    return scored


def _match_docs(
    conn: sqlite3.Connection,
    fts: str,
    docs: str,
    term: str,
    open_packs: list[str],
    pack_index: dict[str, int],
    spaces: list[str] | None,
    per_pack: bool,
    df_term: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(doc keys, dl, group) of the term's documents in the open packs and spaces."""
    match = '"' + term.replace('"', '""') + '"'
    cond = f"d.pack_id IN ({','.join('?' * len(open_packs))})"
    params: list[Any] = [match, match, *open_packs]
    if spaces:
        cond += f" AND d.space IN ({','.join('?' * len(spaces))})"
        params += list(spaces)
    pack_col = ", group_concat(d.pack_id, char(31))" if per_pack else ""
    row = conn.execute(
        f"SELECT (SELECT count(*) FROM {fts} WHERE {fts} MATCH ?), count(*), "
        f"group_concat(d.doc_key, ' '), group_concat(d.dl, ' '){pack_col} "
        f"FROM {fts} f JOIN {docs} d ON d.doc_key = f.rowid "
        f"WHERE {fts} MATCH ? AND {cond}",
        params,
    ).fetchone()
    # Design 6.5: the MATCH set must be the posting set of the term.
    if int(row[0]) != df_term:
        raise PruneFallbackError("match count")
    n = int(row[1])
    keys = _ints(row[2])
    dl = _ints(row[3])
    if per_pack and n:
        packs = row[4].split("\x1f")
        if len(packs) != n:
            raise PruneFallbackError("pack text")
        grp = np.fromiter((pack_index[p] for p in packs), dtype=np.int64, count=n)
    else:
        grp = np.zeros(n, dtype=np.int64)
    if keys.size != n or dl.size != n:
        raise PruneFallbackError("match text")
    if n > 1 and np.any(np.diff(keys) <= 0):
        order = np.argsort(keys, kind="stable")
        keys, dl, grp = keys[order], dl[order], grp[order]
        if np.any(np.diff(keys) == 0):
            raise PruneFallbackError("match duplicate")
    return keys, dl, grp
