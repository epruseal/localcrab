#!/usr/bin/env python3
"""Benchmark sqlite-vec (vec0) vs the live Chroma KURE collection.

Phase-3 gate for docs/pgvector-migration-plan.md (A) path. Copies the live
Chroma dir to a temp location on disk (read-only; never touches live data),
streams the real KURE 1024d vectors into a temp vec0 table (same vectors, raw),
then measures — isolating the *store index/search* behaviour from embedding:

  - recall@10   : sqlite-vec vs Chroma top-10 agreement (chroma = reference)
  - latency p95 : vec0 KNN, unfiltered and pack-partition-filtered
  - pack leak   : partition-filtered results must all belong to the pack
  - disk/build  : vectors.db size and build time

Gate targets (§11.1): recall@10 >= 0.95, single-pack p95 <= 100ms,
metadata-filtered p95 <= 200ms, pack leak = 0.

Notes:
  - The live Chroma dir (~2GB) is copied to --work-dir (default on nvme disk,
    NOT /tmp which is tmpfs) so the running gateway is never touched.
  - Vectors are streamed in batches; only a small query reservoir is held in RAM.

BINARY MODE (--mode binary, docs/pgvector-migration-plan.md §3.7 gate):
  Measures the binary 2-stage ANN path against exact float brute-force on an
  ALREADY-MIGRATED vec0 DB (run scripts/migrate_add_binary_quantization.py on a
  COPY first — this bench is read-only and never migrates/mutates the target;
  point --src-db at the dev copy, NEVER at the live vectors.db):

  - global exact float p50/p95 (the ~868ms baseline being replaced)
  - global 2-stage (bit-hamming coarse C + cosine rerank) p50/p95 per C
  - recall@10 : 2-stage top-10 vs exact float top-10 overlap, per C
    → the smallest C with recall >= 0.95 is reported as the adopted C
  - pack isolation leak on the exact partition-filtered path (must stay 0;
    pack-scoped search keeps the exact path under VECTOR_ANN=binary)

  Usage:
    python scripts/qa/bench_vector_backend.py --mode binary \
        --src-db /path/to/vectors-dev.db [--coarse-ks 256,512,1024]
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import statistics
import tempfile
import time

CHROMA_COLLECTION = "opencrab_vectors_kure"
DIM = 1024


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def pctl(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def bench_binary(args: argparse.Namespace) -> int:
    """§3.7 gate: binary 2-stage (real SqliteVecStore path) vs exact float.

    Protocol (kept cheap — the exact baseline is the expensive part):
      - exact global top-10 per query is computed ONCE and cached to a JSON
        file next to --src-db (keyed by db/table/seed/queries); the C sweep
        reuses it instead of re-running ~1s exact scans per C.
      - the 2-stage numbers come from the REAL store (SqliteVecStore with
        ann="binary"), driven via an injected EF that returns the sampled
        stored vector — i.e. the exact code path serve uses.
    """
    import json as _json
    import sqlite3

    import sqlite_vec

    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    rng = random.Random(args.seed)
    if not args.src_db or not os.path.exists(args.src_db):
        print(f"! --src-db not found: {args.src_db!r} (use a migrated DEV COPY)")
        return 3
    coarse_ks = [int(x) for x in args.coarse_ks.split(",") if x.strip()]

    # raw read-only connection for sampling + exact baseline
    conn = sqlite3.connect(f"file:{args.src_db}?mode=ro", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    table = args.table

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if "embedding_bit" not in cols:
        print(f"! table '{table}' has no embedding_bit column — run "
              "scripts/migrate_add_binary_quantization.py on the copy first.")
        return 3
    n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"# src db  : {args.src_db} (read-only)")
    print(f"# table   : {table}, rows {n}")

    # ---- query sample: random stored vectors (real-data query proxies) ----
    ids = [r[0] for r in conn.execute(f"SELECT node_id FROM {table}")]
    q_ids = rng.sample(ids, min(args.queries, len(ids)))
    q_blobs = [
        bytes(conn.execute(
            f"SELECT embedding FROM {table} WHERE node_id = ?", (qid,)
        ).fetchone()[0])
        for qid in q_ids
    ]
    print(f"# queries : {len(q_blobs)}")

    # ---- exact float global top-10: computed ONCE, cached to JSON ----
    cache_path = (f"{args.src_db}.bench_exact_"
                  f"{table}_{args.seed}_{len(q_ids)}.json")
    lat_exact: list[float] = []
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            payload = _json.load(fh)
        exact_top = payload["exact_top"]
        lat_exact = payload["lat_ms"]
        print(f"# exact baseline: reused cache {cache_path}")
    else:
        exact_top = []
        for qid, qblob in zip(q_ids, q_blobs):
            t = time.perf_counter()
            rows = conn.execute(
                f"SELECT node_id FROM {table} WHERE embedding MATCH ?"
                " AND k = 11 ORDER BY distance",
                (qblob,),
            ).fetchall()
            lat_exact.append((time.perf_counter() - t) * 1000.0)
            exact_top.append([r[0] for r in rows if r[0] != qid][:10])
        with open(cache_path, "w") as fh:
            _json.dump({"exact_top": exact_top, "lat_ms": lat_exact}, fh)
        print(f"# exact baseline: computed once, cached -> {cache_path}")

    # ---- REAL store, ann=binary, injected EF returns the current vector ----
    import struct

    class _HolderEF:
        def __init__(self) -> None:
            self.vec: list[float] = []

        def __call__(self, texts):  # noqa: ANN001
            return [self.vec for _ in texts]

    holder = _HolderEF()
    dim = len(q_blobs[0]) // 4
    store = SqliteVecStore(
        db_path=args.src_db,
        embedding_function=holder,
        dim=dim,
        collection_name=table,
        ann="binary",
        ann_coarse_k=coarse_ks[0],
    )
    if not store.available:
        print("! store init failed")
        return 3

    # warm the ANN cache once (build time reported; excluded from latencies)
    holder.vec = list(struct.unpack(f"{dim}f", q_blobs[0]))
    t = time.perf_counter()
    store.query("warm", n_results=1)
    print(f"# ANN cache build+first query: {time.perf_counter()-t:.1f}s")

    results = []  # (C, recall, p50, p95)
    for C in coarse_ks:
        store._ann_coarse_k = C  # QA-only knob; same store/cache reused
        recalls = []
        lat = []
        for qid, qblob, ref in zip(q_ids, q_blobs, exact_top):
            holder.vec = list(struct.unpack(f"{dim}f", qblob))
            t = time.perf_counter()
            hits = store.query("q", n_results=11)
            lat.append((time.perf_counter() - t) * 1000.0)
            got = [h["id"] for h in hits if h["id"] != qid][:10]
            if ref:
                recalls.append(len(set(ref) & set(got)) / len(ref))
        results.append((C, statistics.mean(recalls) if recalls else 0.0,
                        pctl(lat, 50), pctl(lat, 95)))
        print(f"#   C={C:5d}: recall@10={results[-1][1]:.4f} "
              f"p50={results[-1][2]:.1f}ms p95={results[-1][3]:.1f}ms")

    # ---- pack isolation leak (exact partition path — unchanged under ANN) ----
    packs = [r[0] for r in conn.execute(
        f"SELECT DISTINCT pack_id FROM {table} WHERE pack_id != '' LIMIT 60"
    )]
    lat_pack = []
    leak = 0
    for i, pk in enumerate(packs):
        qblob = q_blobs[i % len(q_blobs)]
        holder.vec = list(struct.unpack(f"{dim}f", qblob))
        t = time.perf_counter()
        hits = store.query("q", n_results=10, where={"pack_id": pk})
        lat_pack.append((time.perf_counter() - t) * 1000.0)
        leak += sum(1 for h in hits if h["metadata"].get("pack_id") != pk)

    adopted = next((r for r in results if r[1] >= 0.95), None)

    print("\n===== BINARY 2-STAGE BENCH RESULT =====")
    print(f"corpus vectors          : {n}")
    print(f"query sample            : {len(q_blobs)}")
    print(f"global exact p50 / p95  : {pctl(lat_exact,50):.1f} / {pctl(lat_exact,95):.1f} ms  (baseline)")
    for C, rec, p50, p95 in results:
        print(f"global 2-stage C={C:5d}  : recall@10={rec:.4f}  p50/p95={p50:.1f}/{p95:.1f} ms")
    print(f"pack-filter p50 / p95   : {pctl(lat_pack,50):.2f} / {pctl(lat_pack,95):.2f} ms")
    print(f"pack isolation leak     : {leak}   [gate == 0]")
    if adopted:
        print(f"adopted C               : {adopted[0]} "
              f"(smallest with recall>=0.95; recall={adopted[1]:.4f}, p95={adopted[3]:.1f}ms)")
    else:
        print("adopted C               : NONE (no tested C reached recall 0.95 — raise --coarse-ks)")

    gate_recall = adopted is not None
    gate_p95 = adopted is not None and adopted[3] <= 100.0
    gate_leak = leak == 0
    ok = gate_recall and gate_p95 and gate_leak
    print("\nGATES:",
          f"recall={'PASS' if gate_recall else 'FAIL'}",
          f"2stage_p95={'PASS' if gate_p95 else 'FAIL'} [<=100ms]",
          f"leak={'PASS' if gate_leak else 'FAIL'}")
    print("OVERALL:", "PASS" if ok else "FAIL")
    store.close()
    conn.close()
    return 0 if ok else 1


def bench_pg(args: argparse.Namespace) -> int:
    """(B) PgVectorStore 게이트 측정 — sqlite vec0 DB 사본(--src-db, 예:
    라이브 vectors.db의 오프라인 사본)의 벡터를 --pg-url 임시 테이블에 스트리밍
    적재(``execute_values``, batch=1000) 후 측정한다:

      - global top-10 p50/p95 (HNSW, ``hnsw.ef_search`` = --ef-search)
      - pack-scoped(실 pack_id) p50/p95
      - recall@10 : HNSW vs exact(동일 데이터, 인덱스 강제 비활성 스캔) top-10 overlap
      - pack leak : pack-scoped 결과가 항상 지정 pack만 포함하는지(=0 이어야 함)

    게이트 상수는 §11.1(recall>=0.95, pack p95<=200ms)을 재사용한다. 전역 p95는
    HNSW가 서브선형이라 별도 게이트를 프리플라이트에서 신설하지 않았으므로(§3.7
    binary 모드의 100ms 게이트와 달리 pgvector는 HNSW 자체가 답이라 참고치만 출력).

    NOTE: 이 함수는 --mode pg 를 동작 가능하게 구현한 것이며, 179k 라이브 데이터
    규모의 실통합 벤치는 이 PR 범위가 아니다(리드가 Phase 2에서 별도 실행) — 여기서는
    작은 --sample(기본 1000)로 모드 동작만 스모크 확인했다.
    """
    import sqlite3
    import struct

    import sqlite_vec
    from psycopg2.extras import execute_values
    from sqlalchemy import create_engine, text

    if not args.pg_url:
        print("! --pg-url is required for --mode pg")
        return 3
    if not args.src_db or not os.path.exists(args.src_db):
        print(f"! --src-db not found: {args.src_db!r} (use an OFFLINE COPY)")
        return 3

    rng = random.Random(args.seed)
    table = args.table
    pg_table = f"bench_pg_{int(time.time())}"

    conn = sqlite3.connect(f"file:{args.src_db}?mode=ro", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if "node_id" not in cols:
        print(f"! table '{table}' missing expected columns {cols}")
        return 3
    n_total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    cap = min(args.sample, n_total) if args.sample else n_total
    print(f"# src sqlite: {args.src_db} table={table} rows={n_total} using={cap}")

    rows = conn.execute(
        f"SELECT node_id, pack_id, embedding, document, metadata FROM {table} LIMIT ?",
        (cap,),
    ).fetchall()
    conn.close()
    if not rows:
        print("! no rows sampled")
        return 3
    dim = len(bytes(rows[0][2])) // 4
    print(f"# dim inferred: {dim}")

    engine = create_engine(args.pg_url)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(text(f"DROP TABLE IF EXISTS {pg_table}"))
            c.execute(text(
                f"CREATE TABLE {pg_table} (node_id TEXT PRIMARY KEY, pack_id TEXT, "
                f"embedding vector({dim}) NOT NULL, document TEXT, metadata JSONB)"
            ))

        t0 = time.perf_counter()
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            payload = []
            for node_id, pack_id, emb_blob, document, metadata in rows:
                vec = struct.unpack(f"{dim}f", bytes(emb_blob))
                vec_lit = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                payload.append(
                    (node_id, pack_id, vec_lit, document, metadata or "{}")
                )
            execute_values(
                cur,
                f"INSERT INTO {pg_table} (node_id, pack_id, embedding, document, metadata) "
                "VALUES %s",
                payload,
                template="(%s, %s, %s::vector, %s, %s::jsonb)",
                page_size=1000,
            )
            raw.commit()
        finally:
            raw.close()
        build_s = time.perf_counter() - t0
        print(f"# pg load: {len(rows)} rows in {build_s:.1f}s (table={pg_table})")

        with engine.begin() as c:
            c.execute(text("SET maintenance_work_mem = '512MB'"))
            c.execute(text("SET max_parallel_maintenance_workers = 0"))
            c.execute(text(
                f"CREATE INDEX ON {pg_table} USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            ))
            c.execute(text(f"CREATE INDEX ON {pg_table} (pack_id)"))

        n = len(rows)
        q_idx = rng.sample(range(n), min(args.queries, n))

        def qlit(i: int) -> str:
            vec = struct.unpack(f"{dim}f", bytes(rows[i][2]))
            return "[" + ",".join(repr(float(x)) for x in vec) + "]"

        # ---- global: HNSW vs exact (index scan disabled) top-10 ----
        lat_hnsw: list[float] = []
        lat_exact: list[float] = []
        recalls: list[float] = []
        with engine.connect() as c:
            c.execute(text(f"SET hnsw.ef_search = {args.ef_search}"))
            for i in q_idx:
                node_id = rows[i][0]
                v = qlit(i)
                t = time.perf_counter()
                hnsw_rows = c.execute(text(
                    f"SELECT node_id FROM {pg_table} "
                    "ORDER BY embedding <=> (:q)::vector LIMIT 11"
                ), {"q": v}).fetchall()
                lat_hnsw.append((time.perf_counter() - t) * 1000.0)
                hnsw_top = [r[0] for r in hnsw_rows if r[0] != node_id][:10]

                c.execute(text("SET enable_indexscan = off"))
                c.execute(text("SET enable_bitmapscan = off"))
                t = time.perf_counter()
                exact_rows = c.execute(text(
                    f"SELECT node_id FROM {pg_table} "
                    "ORDER BY embedding <=> (:q)::vector LIMIT 11"
                ), {"q": v}).fetchall()
                lat_exact.append((time.perf_counter() - t) * 1000.0)
                c.execute(text("SET enable_indexscan = on"))
                c.execute(text("SET enable_bitmapscan = on"))
                exact_top = [r[0] for r in exact_rows if r[0] != node_id][:10]
                if exact_top:
                    recalls.append(
                        len(set(exact_top) & set(hnsw_top)) / len(exact_top)
                    )

        # ---- pack-scoped p95 + leak ----
        packs = list({r[1] for r in rows if r[1]})[:60]
        lat_pack: list[float] = []
        leak = 0
        with engine.connect() as c:
            c.execute(text(f"SET hnsw.ef_search = {args.ef_search}"))
            for i, pk in enumerate(packs):
                v = qlit(q_idx[i % len(q_idx)])
                t = time.perf_counter()
                hits = c.execute(text(
                    f"SELECT node_id, pack_id FROM {pg_table} WHERE pack_id = :pk "
                    "ORDER BY embedding <=> (:q)::vector LIMIT 10"
                ), {"pk": pk, "q": v}).fetchall()
                lat_pack.append((time.perf_counter() - t) * 1000.0)
                leak += sum(1 for r in hits if r[1] != pk)

        recall_at_10 = statistics.mean(recalls) if recalls else 0.0
        print("\n===== PG (pgvector/HNSW) BENCH RESULT =====")
        print(f"corpus vectors          : {n}")
        print(f"query sample            : {len(q_idx)}")
        print(f"pg load time            : {build_s:.1f}s")
        print(f"recall@10 (HNSW vs exact): {recall_at_10:.4f}   [gate >= 0.95]")
        print(f"global HNSW  p50 / p95  : {pctl(lat_hnsw,50):.2f} / {pctl(lat_hnsw,95):.2f} ms")
        print(f"global exact p50 / p95  : {pctl(lat_exact,50):.2f} / {pctl(lat_exact,95):.2f} ms  (enable_indexscan=off)")
        print(f"pack-scoped  p50 / p95  : {pctl(lat_pack,50):.2f} / {pctl(lat_pack,95):.2f} ms   [gate p95 <= 200]")
        print(f"pack isolation leak     : {leak}   [gate == 0]")

        gate_recall = recall_at_10 >= 0.95
        gate_pack = pctl(lat_pack, 95) <= 200.0 if lat_pack else True
        gate_leak = leak == 0
        ok = gate_recall and gate_pack and gate_leak
        print("\nGATES:",
              f"recall={'PASS' if gate_recall else 'FAIL'}",
              f"pack_p95={'PASS' if gate_pack else 'FAIL'}",
              f"leak={'PASS' if gate_leak else 'FAIL'}")
        print("OVERALL:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            with engine.begin() as c:
                c.execute(text(f"DROP TABLE IF EXISTS {pg_table}"))
        except Exception:
            pass
        engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["chroma-parity", "binary", "pg"],
                    default="chroma-parity",
                    help="chroma-parity: original chroma-vs-vec0 gate; "
                         "binary: §3.7 2-stage ANN gate on a migrated DB copy; "
                         "pg: PgVectorStore(HNSW) gate from an offline vec0 DB copy")
    ap.add_argument("--data-dir", default="/home/asdf/.openclaw/workspace/data/localcrab")
    ap.add_argument("--work-dir", default="/home/asdf/.openclaw/workspace",
                    help="disk-backed dir for temp copy+db (NOT tmpfs /tmp)")
    ap.add_argument("--collection", default=CHROMA_COLLECTION)
    ap.add_argument("--queries", type=int, default=200,
                    help="(binary/pg modes use --queries too; 100 is enough)")
    ap.add_argument("--sample", type=int, default=0, help="corpus cap (0 = all)")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1234)
    # binary-mode options
    ap.add_argument("--src-db", default=None,
                    help="[binary/pg] source vec0 DB COPY (read-only; never the live db)")
    ap.add_argument("--table", default="vectors_kure",
                    help="[binary/pg] vec0 table name")
    ap.add_argument("--coarse-ks", default="256,512,1024",
                    help="[binary] comma-separated coarse candidate counts C to sweep")
    # pg-mode options
    ap.add_argument("--pg-url", default=None,
                    help="[pg] PostgreSQL DSN for a disposable bench DB (never prod)")
    ap.add_argument("--ef-search", type=int, default=150,
                    help="[pg] hnsw.ef_search session parameter")
    args = ap.parse_args()

    if args.mode == "binary":
        return bench_binary(args)
    if args.mode == "pg":
        return bench_pg(args)

    rng = random.Random(args.seed)

    import sqlite3

    import chromadb
    import sqlite_vec

    live_chroma = os.path.join(args.data_dir, "chroma")
    tmpdir = tempfile.mkdtemp(prefix="bench_vec_", dir=args.work_dir)
    bench_chroma = os.path.join(tmpdir, "chroma")
    bench_db = os.path.join(tmpdir, "vectors.db")
    try:
        print(f"# copying live chroma -> {bench_chroma} (read-only isolation, ~2GB)")
        t = time.perf_counter()
        shutil.copytree(live_chroma, bench_chroma)
        print(f"#   copied in {time.perf_counter()-t:.1f}s")

        client = chromadb.PersistentClient(path=bench_chroma)
        col = client.get_collection(args.collection)
        total = col.count()
        cap = args.sample if (args.sample and args.sample < total) else total
        print(f"# collection '{args.collection}': {total} vectors; using {cap}")

        conn = sqlite3.connect(bench_db)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            f"CREATE VIRTUAL TABLE v USING vec0(node_id TEXT PRIMARY KEY, "
            f"pack_id TEXT partition key, embedding float[{DIM}] distance_metric=cosine)"
        )

        # stream + insert + reservoir-sample query vectors
        q_ids: list[str] = []
        q_vecs: list[bytes] = []
        q_raw: list[list[float]] = []
        K = args.queries
        seen = 0
        t0 = time.perf_counter()
        off = 0
        while off < cap:
            lim = min(args.batch, cap - off)
            got = col.get(limit=lim, offset=off, include=["embeddings", "metadatas"])
            ids = got["ids"]
            embs = got["embeddings"]
            metas = got["metadatas"] or [{} for _ in ids]
            if not ids:
                break
            from opencrab.stores._vector_base import slot_owner

            rows = []
            for _id, emb, meta in zip(ids, embs, metas):
                vec = list(emb)
                rows.append(
                    # `slot_owner` 로 정규화한다. `str(x.get(k, ""))` 는 키가 있고
                    # 값이 None 일 때 리터럴 "None" 을 만들고, 그 값은 소유 게이트에서
                    # 미소유로도 소유로도 읽히지 않는다(#197).
                    (_id, slot_owner(meta),
                     sqlite_vec.serialize_float32(vec))
                )
                # reservoir sample
                seen += 1
                if len(q_ids) < K:
                    q_ids.append(_id)
                    q_raw.append(vec)
                    q_vecs.append(sqlite_vec.serialize_float32(vec))
                else:
                    j = rng.randint(0, seen - 1)
                    if j < K:
                        q_ids[j] = _id
                        q_raw[j] = vec
                        q_vecs[j] = sqlite_vec.serialize_float32(vec)
            conn.executemany(
                "INSERT INTO v(node_id, pack_id, embedding) VALUES (?,?,?)", rows
            )
            conn.commit()
            off += lim
            if off % 20000 == 0:
                print(f"#   inserted {off}/{cap} ({time.perf_counter()-t0:.0f}s)")
        build_s = time.perf_counter() - t0
        n = conn.execute("SELECT count(*) FROM v").fetchone()[0]
        print(f"# vec0 build: {n} rows in {build_s:.1f}s")

        # ---- recall@10 (chroma reference vs vec0, same query vector) ----
        recalls = []
        for qid, qvec, qser in zip(q_ids, q_raw, q_vecs):
            cres = col.query(query_embeddings=[qvec], n_results=11)
            cids = [i for i in cres["ids"][0] if i != qid][:10]
            r = conn.execute(
                "SELECT node_id FROM v WHERE embedding MATCH ? AND k = 11 ORDER BY distance",
                (qser,),
            ).fetchall()
            sids = [row[0] for row in r if row[0] != qid][:10]
            if cids:
                recalls.append(len(set(cids) & set(sids)) / len(cids))
        recall_at_10 = statistics.mean(recalls) if recalls else 0.0

        # ---- latency: unfiltered KNN ----
        lat_unf = []
        for qser in q_vecs:
            t = time.perf_counter()
            conn.execute(
                "SELECT node_id FROM v WHERE embedding MATCH ? AND k = 10 ORDER BY distance",
                (qser,),
            ).fetchall()
            lat_unf.append((time.perf_counter() - t) * 1000.0)

        # ---- latency + isolation: pack-partition-filtered ----
        pack_rows = conn.execute(
            "SELECT DISTINCT pack_id FROM v WHERE pack_id != '' LIMIT 60"
        ).fetchall()
        packs = [r[0] for r in pack_rows]
        lat_pack = []
        leak = 0
        for i, pk in enumerate(packs):
            qser = q_vecs[i % len(q_vecs)]
            t = time.perf_counter()
            r = conn.execute(
                "SELECT node_id, pack_id FROM v WHERE embedding MATCH ? AND k = 10 "
                "AND pack_id = ? ORDER BY distance",
                (qser, pk),
            ).fetchall()
            lat_pack.append((time.perf_counter() - t) * 1000.0)
            leak += sum(1 for row in r if row[1] != pk)

        db_size = os.path.getsize(bench_db)

        print("\n===== BENCH RESULT =====")
        print(f"corpus vectors        : {n}")
        print(f"query sample          : {len(q_vecs)}")
        print(f"distinct packs tested : {len(packs)}")
        print(f"recall@10 (vs chroma) : {recall_at_10:.4f}   [gate >= 0.95]")
        print(f"unfiltered  p50 / p95 : {pctl(lat_unf,50):.2f} / {pctl(lat_unf,95):.2f} ms   [gate p95 <= 100]")
        print(f"pack-filter p50 / p95 : {pctl(lat_pack,50):.2f} / {pctl(lat_pack,95):.2f} ms   [gate p95 <= 200]")
        print(f"pack isolation leak   : {leak}   [gate == 0]")
        print(f"vectors.db size       : {db_size/1e6:.1f} MB")
        print(f"vec0 build time       : {build_s:.1f}s for {n} rows")

        gate_recall = recall_at_10 >= 0.95
        gate_unf = pctl(lat_unf, 95) <= 100.0
        gate_pack = pctl(lat_pack, 95) <= 200.0
        gate_leak = leak == 0
        ok = gate_recall and gate_unf and gate_pack and gate_leak
        print("\nGATES:",
              f"recall={'PASS' if gate_recall else 'FAIL'}",
              f"unf_p95={'PASS' if gate_unf else 'FAIL'}",
              f"pack_p95={'PASS' if gate_pack else 'FAIL'}",
              f"leak={'PASS' if gate_leak else 'FAIL'}")
        print("OVERALL:", "PASS" if ok else "FAIL")
        conn.close()
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
