#!/usr/bin/env python3
"""Migrate the live Chroma KURE collection into a sqlite-vec (vec0) store.

Phase-5 cutover for docs/pgvector-migration-plan.md (A) path. Instead of
re-embedding (slow, and risks Q8 drift), this copies the EXACT KURE 1024d
vectors already stored in Chroma into a vec0 table with byte-identical
document/metadata — a perfect 1:1 migration in minutes.

The target table matches SqliteVecStore's schema exactly (the store instance
creates it), so after migration `VECTOR_BACKEND=sqlite-vec` serves it directly.

SAFETY:
  - Only READS Chroma; WRITES only the new vectors.db (LOCAL_DATA_DIR).
  - Run AFTER stopping serves and backing up (Phase 5 CRITICAL procedure).
  - Refuses to clobber an existing non-empty vectors.db unless --force.

Usage:
    python scripts/migrate_chroma_to_sqlite_vec.py [--force] [--batch 2000] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import time


def _copy_chroma_to_vec0(args, settings, src_collection, db_path, chroma_path) -> int:
    """Copy one local Chroma collection into a vec0 table. Caller holds chroma.lock.

    Split out of main() so the caller can hold ``chroma.lock`` across the whole
    lifetime of the PersistentClient this opens (issue #140). The several early
    returns below are exactly why: a lock acquired inline in main() would be
    released on some paths and not others.

    Every path tears the client down BEFORE returning, so the caller's lock is
    never released while a client is still open. Reference counting alone covers
    neither half of that:

    * chromadb's client has no ``__del__``, so dropping the last reference does
      not decrement the shared ``System`` refcount. Even the plain early returns
      leaked without an explicit ``close()``.
    * on the exception path the traceback keeps this frame alive, so the local
      names must be cleared too -- ``col`` included, since a Collection holds the
      client on ``self._client``.

    LIMIT: the invariant is "an explicit close is ATTEMPTED before the lock is
    released", not "the client is definitely gone". If ``close()`` raises we
    still release. Holding the lock instead would need process-lifetime handle
    ownership, which issue #140 measured and rejected: it turns a bounded stall
    into a lock nothing ever reclaims. Chroma resources can then outlive the
    lock. ChromaStore.close() and _migrate_vectors_locked follow the same rule.
    """
    import chromadb
    import sqlite_vec

    from opencrab.locking import close_quietly
    from opencrab.stores.chroma_store import _sanitize_metadata
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    # Bound before the try so the finally never meets an unset name: any of the
    # three creations below can raise.
    client = None
    col = None
    store = None
    try:
        client = chromadb.PersistentClient(path=chroma_path)
        col = client.get_collection(src_collection)
        total = col.count()
        print(f"# chroma vectors: {total}")

        if args.dry_run:
            print("# dry-run: no writes. (would create/populate vec0 above)")
            return 0

        if os.path.exists(db_path) and not args.force:
            # allow empty file; refuse if it already holds rows
            probe = None
            try:
                probe = SqliteVecStore(
                    db_path=db_path,
                    embedding_function=lambda x: [],  # unused (no writes here)
                    dim=settings.embed_dim,
                    collection_name=settings.vector_collection,
                )
                try:
                    if probe.count() > 0:
                        print(f"! {db_path} already has {probe.count()} rows. Use --force to overwrite.")
                        return 2
                finally:
                    # count() can raise; without this the probe stayed open.
                    close_quietly(probe, "probe store")
                    probe = None
            except Exception:
                pass

        # Real store creates the exact vec0 schema. EF is required by ctor but unused
        # here (we raw-insert vectors copied from Chroma, never re-embed).
        store = SqliteVecStore(
            db_path=db_path,
            embedding_function=lambda x: [],
            dim=settings.embed_dim,
            collection_name=settings.vector_collection,
        )
        if not store.available:
            print("! SqliteVecStore init failed")
            return 3
        store.reset_collection()  # start clean
        tbl = store._table

        t0 = time.perf_counter()
        off = 0
        inserted = 0
        while off < total:
            lim = min(args.batch, total - off)
            got = col.get(limit=lim, offset=off, include=["embeddings", "documents", "metadatas"])
            ids = got["ids"]
            embs = got["embeddings"]
            docs = got["documents"] or ["" for _ in ids]
            metas = got["metadatas"] or [{} for _ in ids]
            if not ids:
                break
            rows = []
            for _id, emb, doc, meta in zip(ids, embs, docs, metas):
                clean = _sanitize_metadata(meta or {})
                rows.append(
                    (
                        _id,
                        str(clean.get("pack_id", "")),
                        sqlite_vec.serialize_float32(list(emb)),
                        doc or "",
                        json.dumps(clean),
                    )
                )
            with store._lock:
                store._conn.executemany(
                    f"INSERT INTO {tbl}(node_id, pack_id, embedding, document, metadata)"
                    " VALUES (?,?,?,?,?)",
                    rows,
                )
                store._conn.commit()
            inserted += len(rows)
            off += lim
            if off % 20000 == 0 or off >= total:
                print(f"#   migrated {off}/{total} ({time.perf_counter()-t0:.0f}s)")

        final = store.count()
        print(f"# done: inserted {inserted}, vec0 count {final} (chroma {total}) in {time.perf_counter()-t0:.1f}s")
        ok = final == total
        if ok:
            # spot check
            sample = col.get(limit=1, include=["metadatas"])
            if sample["ids"]:
                sid = sample["ids"][0]
                hit = store.get_by_id(sid)
                print(f"# spot get_by_id({sid}): {'OK' if hit else 'MISSING'}")
        print("RESULT:", "PASS" if ok else "FAIL (count mismatch)")
        return 0 if ok else 4
    finally:
        # Independent cleanup per resource (inside close_quietly): one failing
        # close must not skip the rest, or the invariant breaks again.
        close_quietly(store, "sqlite-vec store")
        close_quietly(client, "chroma client")
        # Drop this frame's own references. `col` matters as much as `client`:
        # chromadb's Collection stores the client on `self._client`, so leaving
        # `col` bound keeps the client alive in a traceback-held frame.
        store = col = client = None



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--force", action="store_true", help="overwrite existing vectors.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import sqlite_vec  # noqa: F401 - ensures extension is importable

    from opencrab.config import get_settings

    settings = get_settings()
    src_collection = settings.embed_collection  # opencrab_vectors_kure
    db_path = os.path.join(settings.local_data_dir, settings.vector_db_file)
    chroma_path = os.path.join(settings.local_data_dir, "chroma")

    print(f"# source chroma : {chroma_path} / collection '{src_collection}'")
    print(f"# target vec0   : {db_path} (table '{settings.vector_collection}', dim {settings.embed_dim})")

    # #140: hold chroma.lock for as long as the PersistentClient below lives.
    # SHARED, not exclusive: this reads Chroma and writes vec0, so it may run
    # alongside other readers but must be excluded by a bulk loader or a
    # migration that takes the lock exclusively.
    from opencrab.locking import chroma_lock_dir, chroma_lock_held

    # EXCLUSIVE, not shared (#140). A shared claim looks right for a reader,
    # but a live chroma-backed server also holds this lock shared for its whole
    # lifetime while it keeps serving WRITES under write.lock. Joining it would
    # let this copy run against a moving source: _copy_chroma_to_vec0 snapshots
    # count() and then pages by offset without write.lock, so an ingest or a
    # delete in flight silently drops records or mixes two points in time. The
    # script's stated precondition is an offline run; an exclusive claim makes
    # the lock enforce it instead of leaving it to the operator to remember.
    #
    # Bounded, and the message says what to stop.
    # COMPATIBILITY: this now refuses while a server is up, where it used to
    # proceed. That proceeding could produce a quietly wrong target.
    with chroma_lock_held(chroma_lock_dir(chroma_path), shared=False):
        return _copy_chroma_to_vec0(args, settings, src_collection, db_path, chroma_path)


if __name__ == "__main__":
    raise SystemExit(main())
