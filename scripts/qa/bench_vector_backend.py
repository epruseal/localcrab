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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/home/asdf/.openclaw/workspace/data/localcrab")
    ap.add_argument("--work-dir", default="/home/asdf/.openclaw/workspace",
                    help="disk-backed dir for temp copy+db (NOT tmpfs /tmp)")
    ap.add_argument("--collection", default=CHROMA_COLLECTION)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--sample", type=int, default=0, help="corpus cap (0 = all)")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    import chromadb
    import sqlite3

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
            rows = []
            for _id, emb, meta in zip(ids, embs, metas):
                vec = list(emb)
                rows.append(
                    (_id, str((meta or {}).get("pack_id", "")),
                     sqlite_vec.serialize_float32(vec))
                )
                # reservoir sample
                seen += 1
                if len(q_ids) < K:
                    q_ids.append(_id); q_raw.append(vec)
                    q_vecs.append(sqlite_vec.serialize_float32(vec))
                else:
                    j = rng.randint(0, seen - 1)
                    if j < K:
                        q_ids[j] = _id; q_raw[j] = vec
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
