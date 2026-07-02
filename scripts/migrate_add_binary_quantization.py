#!/usr/bin/env python3
"""Backfill binary-quantized (sign-bit) vectors into an existing vec0 table.

docs/pgvector-migration-plan.md §3.7 non-destructive extension: adds an
``embedding_bit bit[dim]`` column next to the float embeddings so that
VECTOR_ANN=binary can serve global searches via the 2-stage (hamming coarse +
cosine rerank) path. NO re-embedding — bit vectors are derived from the sign
bits of the stored floats via SQL ``vec_quantize_binary(embedding)``
(byte-identical to the store's app-side ``_sign_bits``: bit = 1 where v > 0,
packed LSB-first — verified in tests/test_sqlite_vec_binary_ann.py).

WHY A REBUILD (not ALTER TABLE): vec0 virtual tables support neither
``ALTER TABLE ADD COLUMN`` nor a safe ``RENAME TO`` (rename does not move the
``_info``/``_chunks``/``_rowids``/``_vector_chunksNN`` shadow tables, breaking
all subsequent queries — verified on 0.1.9). The only safe upgrade is:

    1. create ``{table}__bitmig_tmp`` with the new (float+bit) schema
    2. batch-copy {table} → tmp, backfilling bit = vec_quantize_binary(embedding)
    3. verify counts, DROP {table}
    4. recreate {table} with the new schema, batch-copy tmp → {table}
    5. verify counts, DROP tmp

Data (float vectors, documents, metadata, pack partitions) is preserved
bit-for-bit; only the table's DDL changes. Idempotent: if ``embedding_bit``
already exists the script exits 0 without touching anything. Re-runnable after
a crash (the tmp table is dropped and rebuilt from the still-intact source; if
the crash happened after step 3, restore from the --backup-to file).

SAFETY:
  - REQUIRES --backup-to <path> (online .backup() copy taken before any write)
    unless --skip-backup is passed explicitly. 운영 원칙: DB 작업 전 백업 필수.
  - Never run against a DB being served concurrently (DROP/rebuild swaps the
    table under readers). Stop serves first.

Usage:
    python scripts/migrate_add_binary_quantization.py \
        --db-path /path/to/vectors.db --backup-to /path/to/backup.db \
        [--table vectors_kure] [--dim 1024] [--batch 5000] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time


def _connect(db_path: str) -> sqlite3.Connection:
    import sqlite_vec

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _create_sql(table: str, dim: int, with_bit: bool) -> str:
    # Must match SqliteVecStore._create_table_sql exactly (same column order /
    # partition key / auxiliary columns) so the store serves the result as-is.
    bit_col = f"embedding_bit bit[{dim}], " if with_bit else ""
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
        "node_id TEXT PRIMARY KEY, "
        "pack_id TEXT partition key, "
        f"embedding float[{dim}] distance_metric=cosine, "
        f"{bit_col}"
        "+document TEXT, "
        "+metadata TEXT"
        ")"
    )


def _copy_rows(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    batch: int,
    total: int,
    label: str,
) -> int:
    """Keyset-paginated batch copy src → dst (dst has the bit column).

    embedding_bit is derived in SQL via vec_quantize_binary(embedding) — a pure
    deterministic sign-bit projection of the float vector, so it is safe (and
    simplest) to re-derive on every copy instead of round-tripping the bit BLOB
    (whose raw bytes would be misread as float32 when re-bound: len % 4 == 0).
    """
    t0 = time.perf_counter()
    copied = 0
    last_id = ""
    while True:
        rows = conn.execute(
            f"SELECT node_id, pack_id, embedding, document, metadata"
            f" FROM {src} WHERE node_id > ? ORDER BY node_id LIMIT ?",
            (last_id, batch),
        ).fetchall()
        if not rows:
            break
        conn.executemany(
            f"INSERT INTO {dst}"
            "(node_id, pack_id, embedding, embedding_bit, document, metadata)"
            " VALUES (?, ?, ?, vec_quantize_binary(?), ?, ?)",
            [(r[0], r[1], r[2], r[2], r[3], r[4]) for r in rows],
        )
        conn.commit()
        copied += len(rows)
        last_id = rows[-1][0]
        if copied % 20000 < batch or copied >= total:
            print(f"#   {label}: {copied}/{total} ({time.perf_counter()-t0:.0f}s)")
    return copied


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db-path", default=None,
                    help="vectors.db path (default: <LOCAL_DATA_DIR>/<VECTOR_DB_FILE>)")
    ap.add_argument("--table", default=None,
                    help="vec0 table name (default: VECTOR_COLLECTION)")
    ap.add_argument("--dim", type=int, default=None,
                    help="vector dimension (default: EMBED_DIM)")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--backup-to", default=None,
                    help="write an online .backup() copy here before migrating (REQUIRED)")
    ap.add_argument("--skip-backup", action="store_true",
                    help="explicitly skip the mandatory pre-migration backup")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.db_path is None or args.table is None or args.dim is None:
        from opencrab.config import get_settings

        settings = get_settings()
        if args.db_path is None:
            args.db_path = os.path.join(
                settings.local_data_dir, settings.vector_db_file
            )
        if args.table is None:
            args.table = settings.vector_collection
        if args.dim is None:
            args.dim = settings.embed_dim

    table = args.table
    tmp = f"{table}__bitmig_tmp"
    print(f"# target db    : {args.db_path}")
    print(f"# table / dim  : {table} / {args.dim}")

    if not os.path.exists(args.db_path):
        print(f"! db not found: {args.db_path}")
        return 3

    conn = _connect(args.db_path)

    # ---- idempotency: already migrated? ----
    if _has_column(conn, table, "embedding_bit"):
        print(f"# table '{table}' already has embedding_bit — nothing to do.")
        print("RESULT: PASS (already migrated)")
        return 0

    total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    # sanity: stored vector dim must match --dim (bit[N] must mirror float[N])
    row = conn.execute(f"SELECT vec_length(embedding) FROM {table} LIMIT 1").fetchone()
    if row and row[0] != args.dim:
        print(f"! stored vector dim {row[0]} != --dim {args.dim}; aborting.")
        return 3
    if args.dim % 8 != 0:
        print(f"! dim {args.dim} not divisible by 8 (bit packing); aborting.")
        return 3
    print(f"# rows to migrate: {total}")

    if args.dry_run:
        print("# dry-run: no writes. Plan:")
        print(f"#   1. backup -> {args.backup_to or '(none — would be REQUIRED)'}")
        print(f"#   2. create {tmp} (float[{args.dim}] + bit[{args.dim}])")
        print(f"#   3. copy {table} -> {tmp} with vec_quantize_binary backfill")
        print(f"#   4. DROP {table}, recreate with bit column, copy back, DROP {tmp}")
        return 0

    # ---- mandatory backup (운영 원칙: DB 작업 전 백업 필수) ----
    if not args.backup_to and not args.skip_backup:
        print("! --backup-to <path> is required (or pass --skip-backup explicitly).")
        return 2
    if args.backup_to:
        if os.path.exists(args.backup_to):
            print(f"! backup target already exists: {args.backup_to} (refusing to overwrite)")
            return 2
        print(f"# backing up -> {args.backup_to}")
        t = time.perf_counter()
        dst = sqlite3.connect(args.backup_to)
        with dst:
            conn.backup(dst)
        dst.close()
        print(f"#   backup done in {time.perf_counter()-t:.1f}s")

    t0 = time.perf_counter()

    # ---- stage 1: build tmp with the new schema, backfilled ----
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")  # crash leftover from a prior run
    conn.execute(_create_sql(tmp, args.dim, with_bit=True))
    conn.commit()
    copied = _copy_rows(conn, table, tmp, args.batch, total=total, label="stage")
    tmp_count = conn.execute(f"SELECT count(*) FROM {tmp}").fetchone()[0]
    if tmp_count != total:
        print(f"! stage count mismatch: tmp {tmp_count} != source {total}. "
              f"Source untouched; tmp kept for inspection.")
        return 5

    # ---- stage 2: swap — drop original, recreate with bit column, copy back ----
    # (vec0 RENAME is unsafe: shadow tables keep the old prefix. Rebuild instead.)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(_create_sql(table, args.dim, with_bit=True))
    conn.commit()
    _copy_rows(conn, tmp, table, args.batch, total=total, label="final")
    final_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    if final_count != total:
        print(f"! final count mismatch: {final_count} != {total}. "
              f"tmp table {tmp} kept; restore from --backup-to if needed.")
        return 5

    conn.execute(f"DROP TABLE {tmp}")
    conn.commit()

    # ---- verification: schema + spot bit/float consistency ----
    assert _has_column(conn, table, "embedding_bit")
    mismatch = conn.execute(
        f"SELECT count(*) FROM (SELECT node_id FROM {table} LIMIT 1000) s"
        f" JOIN {table} t ON t.node_id = s.node_id"
        " WHERE vec_to_json(t.embedding_bit) != vec_to_json(vec_quantize_binary(t.embedding))"
    ).fetchone()[0]
    conn.close()

    print(f"# done: {copied} rows in {time.perf_counter()-t0:.1f}s; "
          f"spot-check bit==sign(float) mismatches: {mismatch}/1000")
    print("# note: the rebuild leaves free pages in the file (size roughly "
          "doubles). Run `sqlite3 <db> 'VACUUM;'` afterwards to reclaim.")
    ok = mismatch == 0
    print("RESULT:", "PASS" if ok else "FAIL (bit/float mismatch)")
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
