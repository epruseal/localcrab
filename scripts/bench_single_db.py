#!/usr/bin/env python3
"""다섯 파일 구조와 단일 파일의 지연 시간을 같은 조건으로 잰다 (#671 시제품).

모든 하위 명령은 ``--layout five|single``과 ``--dir`` 을 받는다. ``--dir`` 은 **시험 사본**이다.
라이브 데이터와 백업 경로를 가리키지 않는다. 이 도구는 ``--dir`` 아래 파일을 읽고 쓴다.
``--dir`` 과 임시 디렉터리가 tmpfs나 ramfs이면 시작하지 않는다.

- ``samples``: 질의 표본(그래프 허브와 임의 노드, 벡터 질의 노드, BM25 용어와 팩)을 ``--out`` JSON에 쓴다.
  다섯 파일 구조에서 질의마다 한 번 돌려 빈 결과를 낸 표본은 버린다. 표본은 한 번 만들어 두 구조에 똑같이 쓴다.
- ``latency``: 그래프 3-hop(현재 경로와 재귀 CTE), 벡터(전역과 팩 한정), BM25(node와 keyword)를 잰다.
  질의마다 결과 지문(해시)과 크기를 남기고, 빈 결과나 ``None``이 나오면 멈춘다.
- ``judge``: ``latency`` 출력을 모아 사전 등록 규칙으로 판정한다(규칙 본문은 ``docs/records/issue-671-single-db-2026-11-18.md`` 2.3절).
- ``concurrent``: 재적재 모양의 긴 쓰기 스레드를 돌리면서 읽기 지연과 billing 쓰기 지연을 잰다.
  쓰기 스레드가 실제로 돌았는지(유효 조건)를 출력에 판정해 남긴다.
- ``maintenance``: ``--op backup|vacuum|check|restore`` 하나를 잰다(op마다 따로 실행해 최대 RSS를 분리한다).

출력은 JSON 한 줄이다. 시간은 ms, 크기는 바이트다.
임베딩은 쓰지 않는다. 벡터 질의는 저장된 벡터를 그대로 질의 벡터로 쓰는 대역 함수로 돈다.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import resource
import shutil
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "merge_sqlite_stores", Path(__file__).resolve().parent / "merge_sqlite_stores.py")
merge_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_tool)

GRAPH_LIMIT = 50
MIN_ROWS_FRACTION = 0.8
MAX_LOAD1 = 2.0
RECURSIVE_CTE = """
WITH RECURSIVE walk(node_id, depth) AS (
  SELECT :anchor, 0
  UNION
  SELECT e.to_id, w.depth + 1 FROM walk w JOIN graph_edges e ON e.from_id = w.node_id WHERE w.depth < :depth
  UNION
  SELECT e.from_id, w.depth + 1 FROM walk w JOIN graph_edges e ON e.to_id = w.node_id WHERE w.depth < :depth
)
SELECT node_id FROM walk WHERE node_id != :anchor LIMIT :limit
"""
KINDS = ("graph_bfs", "graph_cte", "vector_global", "vector_pack",
         "bm25_node_common", "bm25_node_mid", "keyword_mid")


class BenchError(RuntimeError):
    """측정을 무효로 하는 조건. 메시지에 원인을 담고 실행을 멈춘다."""


def layout_paths(layout: str, d: Path, single_name: str) -> dict[str, Path]:
    if layout == "single":
        p = d / single_name
        return {"graph": p, "doc": p, "vec": p}
    return {"graph": d / "graph.db", "doc": d / "doc_store.db", "vec": d / "vectors.db"}


def check_dirs(*paths: Path) -> None:
    for p in paths:
        merge_tool.refuse_ram_fs(str(p), p)
    for var in ("TMPDIR", "SQLITE_TMPDIR"):
        if os.environ.get(var):
            merge_tool.refuse_ram_fs(var, Path(os.environ[var]))


def machine_state() -> dict:
    load1 = float(Path("/proc/loadavg").read_text().split()[0])
    avail = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            avail = int(line.split()[1]) // 1024
    return {"load1": load1, "mem_available_mib": avail}


def ro_conn(path: Path) -> sqlite3.Connection:
    import sqlite_vec

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def vec_dim(path: Path) -> int:
    conn = ro_conn(path)
    try:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='vectors_kure'").fetchone()[0]
    finally:
        conn.close()
    return int(re.search(r"float\[(\d+)\]", ddl).group(1))


class StoredVectorEF:
    """질의 텍스트를 키로 저장된 벡터를 돌려주는 대역 임베딩 함수.

    모르는 텍스트는 결정적 벡터를 낸다. ``delay``는 호출 한 번마다 걸리는 임베딩 시간 흉내다.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.delay = 0.0
        self.vectors: dict[str, list[float]] = {}

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if self.delay:
            time.sleep(self.delay)
        out = []
        for t in texts:
            if t in self.vectors:
                out.append(self.vectors[t])
            else:
                rnd = random.Random(t)
                out.append([rnd.random() - 0.5 for _ in range(self.dim)])
        return out


class Stores:
    def __init__(self, layout: str, d: Path, single_name: str, ann: str = "") -> None:
        os.environ["LOCAL_DATA_DIR"] = str(d)
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        self.paths = layout_paths(layout, d, single_name)
        self.layout = layout
        self.dim = vec_dim(self.paths["vec"])
        self.ef = StoredVectorEF(self.dim)
        kw = {}
        if layout == "single":
            from opencrab.stores._single_file import SharedSqliteFile

            kw = {"shared": SharedSqliteFile(str(self.paths["graph"]))}
        self.graph = LocalGraphStore(str(self.paths["graph"]), **kw)
        self.docs = LocalSQLDocStore(str(self.paths["doc"]), **kw)
        self.vec = SqliteVecStore(str(self.paths["vec"]), embedding_function=self.ef, dim=self.dim,
                                  ann=ann, **kw)

    def unit(self):
        from opencrab.stores._single_file import unit_of_work_for

        return unit_of_work_for(self.vec, self.docs, self.graph)

    def close(self) -> None:
        for s in (self.graph, self.docs, self.vec):
            s.close()


def pct(sorted_ms: list[float], q: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def summarize(ms: list[float]) -> dict:
    if not ms:
        return {"n": 0}
    s = sorted(ms)
    return {"n": len(s), "p50": round(statistics.median(s), 3), "p95": round(pct(s, 0.95), 3),
            "mean": round(statistics.fmean(s), 3), "max": round(s[-1], 3)}


def timed(fn):
    t0 = time.perf_counter()
    res = fn()
    return (time.perf_counter() - t0) * 1000.0, res


def fingerprint(result, *, as_set: bool) -> tuple[str, int]:
    items = [json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) for r in result]
    if as_set:
        items.sort()
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16], len(items)


# ---------------------------------------------------------------- queries

def load_query_vectors(stores: Stores, samples: dict) -> None:
    import array

    conn = ro_conn(stores.paths["vec"])
    try:
        for node_id, _pack in samples["vector_queries"]:
            blob = conn.execute("SELECT embedding FROM vectors_kure WHERE node_id=?", (node_id,)).fetchone()[0]
            a = array.array("f")
            a.frombytes(blob)
            stores.ef.vectors[node_id] = list(a)
    finally:
        conn.close()


def query_calls(stores: Stores, samples: dict):
    """(kind, key, callable, as_set) 목록. 한 번 도는 질의 전부다."""
    graph, docs, vec = stores.graph, stores.docs, stores.vec

    def cte(anchor: str) -> list[dict]:
        conn = graph._conn
        ids = [r[0] for r in conn.execute(RECURSIVE_CTE, {"anchor": anchor, "depth": 3, "limit": GRAPH_LIMIT})]
        rows = []
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = [{"node_id": r[0], "node_type": r[1], "properties": r[2]} for r in conn.execute(
                f"SELECT node_id, node_type, properties FROM graph_nodes WHERE node_id IN ({marks})", ids)]
        return rows

    calls = []
    for nid in samples["graph_hubs"] + samples["graph_random"]:
        calls.append(("graph_bfs", nid, lambda nid=nid: graph.find_neighbors(nid, "both", 3, GRAPH_LIMIT), True))
        calls.append(("graph_cte", nid, lambda nid=nid: cte(nid), True))
    for node_id, pack in samples["vector_queries"]:
        calls.append(("vector_global", node_id, lambda n=node_id: vec.query(n, n_results=10), False))
        if pack:
            calls.append(("vector_pack", node_id,
                          lambda n=node_id, p=pack: vec.query(n, n_results=10, where={"pack_id": p}), False))
    if samples.get("bm25_index_ready"):
        packs = samples["bm25_packs"]  # 큰 팩 다섯 개를 한 질의의 대상으로 쓴다(한 팩이면 빈 결과가 많다)

        def node_search(term):
            res = docs.node_bm25_search(term, pack_ids=packs, limit=20)
            return None if res is None else res[0]

        for term in samples["bm25_terms_common"]:
            calls.append(("bm25_node_common", term, lambda t=term: node_search(t), False))
        for term in samples["bm25_terms_mid"]:
            calls.append(("bm25_node_mid", term, lambda t=term: node_search(t), False))
            calls.append(("keyword_mid", term,
                          lambda t=term: docs.keyword_search(t, pack_ids=packs, limit=20), False))
    return calls


def run_pass(stores: Stores, samples: dict, kinds: set[str] | None = None) -> list[tuple[str, str, float, str, int, bool]]:
    """질의 한 패스. (kind, key, ms, 지문, 크기, 한도에 걸림)을 돌려준다. 빈 결과는 BenchError."""
    out = []
    for kind, key, fn, as_set in query_calls(stores, samples):
        if kinds is not None and kind not in kinds:
            continue
        ms, res = timed(fn)
        if res is None:
            raise BenchError(f"{kind}:{key} returned None (index not ready or query invalid)")
        fp, size = fingerprint(res, as_set=as_set)
        if size == 0:
            raise BenchError(f"{kind}:{key} returned an empty result")
        out.append((kind, key, ms, fp, size, kind in ("graph_bfs",) and size >= GRAPH_LIMIT))
    return out


# ---------------------------------------------------------------- samples

def cmd_samples(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    check_dirs(d)
    paths = layout_paths(args.layout, d, args.single_name)
    rnd = random.Random(args.seed)
    out: dict = {"seed": args.seed}
    g = ro_conn(paths["graph"])
    hubs = [r[0] for r in g.execute(
        "SELECT from_id FROM graph_edges GROUP BY from_id ORDER BY count(*) DESC, from_id LIMIT ?",
        (args.hubs,))]
    max_rowid = g.execute("SELECT max(rowid) FROM graph_nodes").fetchone()[0]
    randoms: list[str] = []
    while len(randoms) < args.randoms:
        r = g.execute("SELECT node_id FROM graph_nodes WHERE rowid >= ? ORDER BY rowid LIMIT 1",
                      (rnd.randint(1, max_rowid),)).fetchone()
        if r and r[0] not in randoms and r[0] not in hubs:
            randoms.append(r[0])
    out["graph_hubs"], out["graph_random"] = hubs, randoms
    g.close()

    v = ro_conn(paths["vec"])
    vmax = v.execute("SELECT max(rowid) FROM vectors_kure_rowids").fetchone()[0]
    vec_nodes: list[list] = []
    while len(vec_nodes) < args.vector_queries:
        r = v.execute("SELECT id FROM vectors_kure_rowids WHERE rowid >= ? ORDER BY rowid LIMIT 1",
                      (rnd.randint(1, vmax),)).fetchone()
        if r is None:
            continue
        row = v.execute("SELECT node_id, pack_id FROM vectors_kure WHERE node_id = ?", (r[0],)).fetchone()
        if row and list(row) not in vec_nodes:
            vec_nodes.append(list(row))
    out["vector_queries"] = vec_nodes
    v.close()

    d_conn = ro_conn(paths["doc"])
    try:
        from opencrab.stores import _node_bm25_sqlite as nb

        total = d_conn.execute(f'SELECT count(*) FROM "{nb.DOCS}"').fetchone()[0]
        out["bm25_index_ready"] = total > 0
        if total > 0:
            out["bm25_packs"] = [r[0] for r in d_conn.execute(
                f'SELECT pack_id FROM "{nb.DOCS}" GROUP BY pack_id ORDER BY count(*) DESC, pack_id LIMIT 5')]
            terms = d_conn.execute(
                f'SELECT term, doc FROM "{nb.VROW}" WHERE length(term) >= 3 ORDER BY doc DESC LIMIT 5000'
            ).fetchall()
            common = [t for t, c in terms if c > total * 0.5][:3]
            mid = [t for t, c in terms if total * 0.001 < c <= total * 0.05]
            rnd.shuffle(mid)
            # 후보 풀을 넉넉히 잡는다. 대상 팩에 없는 용어는 빈 결과를 내므로 아래 선별에서 버리고 앞에서 terms개만 남긴다.
            out["bm25_terms_common"], out["bm25_terms_mid"] = common, mid[: args.terms * 10]
            # 절대 건수는 기록 문서에 넣지 않는다. 비율만 남긴다.
            out["bm25_common_doc_ratio"] = [round(c / total, 3) for t, c in terms if t in common]
    except sqlite3.OperationalError as exc:
        out["bm25_index_ready"] = False
        out["bm25_error"] = str(exc)
    d_conn.close()

    planned = {"graph": len(hubs) + len(randoms), "vector": len(vec_nodes),
               "bm25_common": len(out.get("bm25_terms_common", [])),
               "bm25_mid": args.terms if out.get("bm25_terms_mid") else 0}
    out["planned"] = planned
    # 다섯 파일 구조에서 질의마다 한 번 돌려 빈 결과를 낸 표본을 버린다.
    os.environ["OPENCRAB_BM25_CACHE"] = "0"
    stores = Stores(args.layout, d, args.single_name)
    try:
        load_query_vectors(stores, out)
        bad: set[tuple[str, str]] = set()
        for kind, key, fn, as_set in query_calls(stores, out):
            try:
                res = fn()
            except Exception:  # noqa: BLE001 -- 질의 실패도 표본에서 뺀다
                res = None
            if not res:
                bad.add((kind, key))
    finally:
        stores.close()
    out["dropped"] = sorted(f"{k}:{v}" for k, v in bad)
    out["graph_hubs"] = [n for n in hubs if ("graph_bfs", n) not in bad and ("graph_cte", n) not in bad]
    out["graph_random"] = [n for n in randoms if ("graph_bfs", n) not in bad and ("graph_cte", n) not in bad]
    out["vector_queries"] = [q for q in vec_nodes if ("vector_global", q[0]) not in bad]
    for name in ("bm25_terms_common", "bm25_terms_mid"):
        kind = "bm25_node_common" if name.endswith("common") else "bm25_node_mid"
        if name in out:
            out[name] = [t for t in out[name] if (kind, t) not in bad and ("keyword_mid", t) not in bad]
    out["bm25_terms_mid"] = out.get("bm25_terms_mid", [])[: args.terms]
    kept = {"graph": len(out["graph_hubs"]) + len(out["graph_random"]), "vector": len(out["vector_queries"]),
            "bm25_common": len(out.get("bm25_terms_common", [])), "bm25_mid": len(out.get("bm25_terms_mid", []))}
    out["kept"] = kept
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    short = [k for k in kept if planned[k] and kept[k] < planned[k] * MIN_ROWS_FRACTION]
    print(json.dumps({"samples_written": True, "planned": planned, "kept": kept, "short": short,
                      "bm25_ready": out.get("bm25_index_ready")}))
    return 1 if short else 0


# ---------------------------------------------------------------- latency

def cmd_latency(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    check_dirs(d)
    state0 = machine_state()
    if state0["load1"] > args.max_load1:
        print(json.dumps({"noisy": True, "reason": "load1 above limit at start", "state": state0}))
        return 4
    os.environ["OPENCRAB_BM25_CACHE"] = "1" if args.bm25_cache == "on" else "0"
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    stores = Stores(args.layout, d, args.single_name, ann=args.ann)
    try:
        load_query_vectors(stores, samples)
        kinds = set(args.kinds.split(",")) if args.kinds else None
        warm = run_pass(stores, samples, kinds)  # 웜업 패스: 판정에 쓰지 않고 기록만 한다
        raw: dict[str, list[float]] = {k: [] for k in KINDS}
        fps: dict[str, dict[str, list]] = {}
        for i in range(args.rounds):
            for kind, key, ms, fp, size, limit_hit in run_pass(stores, samples, kinds):
                raw[kind].append(round(ms, 4))
                prev = fps.setdefault(kind, {}).get(key)
                entry = [fp, size, limit_hit]
                if prev is not None and prev != entry:
                    raise BenchError(f"{kind}:{key} changed between passes of one process")
                fps[kind][key] = entry
        conn = stores.graph._conn
        cache_info = {"cache_size": conn.execute("PRAGMA cache_size").fetchone()[0],
                      "mmap_size": conn.execute("PRAGMA mmap_size").fetchone()[0]}
    except BenchError as exc:
        print(json.dumps({"error": str(exc), "layout": args.layout}))
        return 1
    finally:
        stores.close()
    first_pass: dict[str, list[float]] = {k: [] for k in KINDS}
    for kind, _key, ms, *_rest in warm:
        first_pass[kind].append(round(ms, 4))
    print(json.dumps({
        "mode": "latency", "layout": args.layout, "label": args.label, "bm25_cache": args.bm25_cache,
        "ann": args.ann or "off", "rounds": args.rounds, "raw": raw, "fingerprints": fps,
        "first_pass_summary": {k: summarize(v) for k, v in first_pass.items()},
        "summary": {k: summarize(v) for k, v in raw.items()},
        "connection_settings": cache_info, "state_start": state0, "state_end": machine_state(),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }))
    return 0


def load_runs(paths: list[str]) -> list[dict]:
    return [json.loads(Path(p).read_text(encoding="utf-8").strip().splitlines()[-1]) for p in paths]


def pooled(runs: list[dict], kind: str) -> list[float]:
    return [x for r in runs if not r.get("noisy") for x in r["raw"][kind]]


def cmd_judge(args: argparse.Namespace) -> int:
    for paths in (args.s, args.f1, args.f2 or []):
        resolved = [Path(path).resolve() for path in paths]
        if len(resolved) != len(set(resolved)):
            raise ValueError("각 회차는 서로 다른 파일이어야 한다")
    s_runs = [r for r in load_runs(args.s) if not r.get("noisy")]
    f1_runs = [r for r in load_runs(args.f1) if not r.get("noisy")]
    f2_requested = bool(args.f2)
    f2_runs = [r for r in load_runs(args.f2) if not r.get("noisy")] if f2_requested else []
    verdict: dict = {"mode": "judge", "kinds": {}, "a_a_independent_copy": f2_requested}
    for kind in KINDS:
        s_valid = [r for r in s_runs if r["raw"].get(kind)]
        f1_valid = [r for r in f1_runs if r["raw"].get(kind)]
        f2_valid = [r for r in f2_runs if r["raw"].get(kind)]
        if not s_valid or not f1_valid:
            verdict["kinds"][kind] = {"status": "미측정"}
            continue
        if len(s_valid) < 2 or len(f1_valid) < 2 or (f2_requested and len(f2_valid) < 2):
            verdict["kinds"][kind] = {"status": "미결(유효 회차 부족)"}
            continue
        s_ms, f1_ms = sorted(pooled(s_valid, kind)), sorted(pooled(f1_valid, kind))
        # 한도에 걸린 그래프 질의는 모든 회차에서 결과 크기를 비교한다.
        all_runs = s_valid + f1_valid + (f2_valid if f2_requested else [])
        fingerprints = [r["fingerprints"].get(kind, {}) for r in all_runs]
        keys = set().union(*(set(fp) for fp in fingerprints))
        if not keys:
            verdict["kinds"][kind] = {"status": "무효(결과 지문 없음)"}
            continue
        mismatches = []
        for key in keys:
            values = [fp.get(key) for fp in fingerprints]
            if any(value is None for value in values):
                mismatches.append(key)
                continue
            if any(value[2] for value in values):
                same = len({value[1] for value in values}) == 1
            else:
                same = len({(value[0], value[1]) for value in values}) == 1
            if not same:
                mismatches.append(key)
        if mismatches:
            verdict["kinds"][kind] = {"status": "무효(결과 불일치)", "samples": sorted(mismatches)[:5]}
            continue
        n = min(len(s_ms), len(f1_ms))
        use_p95 = n >= 100
        if f2_requested:
            f2_ms = sorted(pooled(f2_valid, kind))
            band50 = max(0.10, abs(statistics.median(f1_ms) - statistics.median(f2_ms)) / statistics.median(f1_ms))
            band95 = max(0.15, abs(pct(f1_ms, 0.95) - pct(f2_ms, 0.95)) / pct(f1_ms, 0.95))
            lower_bound = False
        else:
            # 같은 사본을 라운드마다 다시 잰 폭: 라운드별 값과 풀링 값의 최대 상대 편차(하한)
            def spread(q: float) -> float:
                base = pct(f1_ms, q) if q != 0.5 else statistics.median(f1_ms)
                devs = []
                for r in f1_valid:
                    xs = sorted(r["raw"][kind])
                    v = pct(xs, q) if q != 0.5 else statistics.median(xs)
                    devs.append(abs(v - base) / base)
                return max(devs) if devs else 0.0
            band50, band95 = max(0.10, spread(0.5)), max(0.15, spread(0.95))
            lower_bound = True
        p50_s, p50_f = statistics.median(s_ms), statistics.median(f1_ms)
        p95_s, p95_f = pct(s_ms, 0.95), pct(f1_ms, 0.95)

        def grade(value: float, base: float, band: float) -> str:
            if value <= base * (1 + band):
                return "충족"
            if lower_bound:
                return "미충족" if value > base * (1 + 3 * band) else "미결(잡음 폭 하한)"
            return "미충족"

        entry = {"n_S": len(s_ms), "n_F1": len(f1_ms), "p50_S": round(p50_s, 3), "p50_F1": round(p50_f, 3),
                 "ratio_p50": round(p50_s / p50_f, 3), "band50": round(band50, 3),
                 "grade_p50": grade(p50_s, p50_f, band50), "p95_S": round(p95_s, 3), "p95_F1": round(p95_f, 3),
                 "ratio_p95": round(p95_s / p95_f, 3), "band95": round(band95, 3)}
        entry["grade_p95"] = grade(p95_s, p95_f, band95) if use_p95 else "판정 안 함(n<100)"
        grades = [entry["grade_p50"]] + ([entry["grade_p95"]] if use_p95 else [])
        if kind in ("graph_bfs", "graph_cte"):
            entry["gate_100ms_p95_S"] = round(p95_s, 3)
            entry["grade_gate_100ms"] = "충족" if p95_s <= 100.0 else "미충족"
            grades.append(entry["grade_gate_100ms"])
        entry["status"] = ("미충족" if "미충족" in grades else
                           "미결" if any(g.startswith("미결") for g in grades) else "충족")
        verdict["kinds"][kind] = entry
    print(json.dumps(verdict, indent=1, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- concurrent

def table_counts(stores: Stores) -> dict[str, int]:
    return {
        "graph_nodes": stores.graph._conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0],
        "graph_edges": stores.graph._conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0],
        "doc_sources": stores.docs._conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
        "vectors": stores.vec._conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0],
    }


def restore_copy(stores: Stores, baseline_path: Path) -> dict:
    """이전 실행이 쓴 시험 행(id 접두어 ``bench-w-``)을 지우고 행 수가 기준과 같은지 본다."""
    ids = [r[0] for r in stores.vec._conn.execute(
        "SELECT id FROM vectors_kure_rowids WHERE id LIKE 'bench-w-%'")]
    if ids:
        for i in range(0, len(ids), 500):
            stores.vec.delete(ids[i:i + 500])
    doc_ids = [r[0] for r in stores.docs._conn.execute(
        "SELECT source_id FROM doc_sources WHERE source_id LIKE 'bench-w-%'")]
    if doc_ids:
        for i in range(0, len(doc_ids), 500):
            stores.docs.delete_sources_with_state(doc_ids[i:i + 500])
    with stores.graph._tx(immediate=True) as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id LIKE 'bench-w-%' OR to_id LIKE 'bench-w-%'")
        conn.execute("DELETE FROM graph_nodes WHERE node_id LIKE 'bench-w-%'")
    counts = table_counts(stores)
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text())
        if base != counts:
            raise BenchError(f"copy not restored: {counts} != baseline {base}")
    else:
        baseline_path.write_text(json.dumps(counts))
    return counts


def writer_loop(stores: Stores, kind: str, stop: threading.Event, batch: int, out: dict, pause: float) -> None:
    rnd = random.Random(7)
    i = 0
    try:
        while not stop.is_set():
            ids = [f"bench-w-{i}-{j}" for j in range(batch)]
            for sid in ids:
                stores.ef.vectors[sid] = [rnd.random() - 0.5 for _ in range(stores.dim)]
            t0 = time.perf_counter()
            with stores.unit():
                stores.vec.upsert_texts(texts=ids, ids=ids, metadatas=[{"pack_id": "bench-pack"}] * batch)
                if kind == "reingest":
                    for sid in ids:
                        stores.docs.upsert_source(sid, f"bench body {sid} alpha", {"pack_id": "bench-pack"})
                for sid in ids:
                    stores.graph.upsert_node("Concept", sid, {"pack_id": "bench-pack"}, "subject")
                for a, b in zip(ids, ids[1:]):
                    stores.graph.upsert_edge("Concept", a, "relates_to", "Concept", b, {"pack_id": "bench-pack"})
            out["unit_ms"].append((time.perf_counter() - t0) * 1000.0)
            i += 1
            if pause > 0:
                time.sleep(pause)
    except BaseException as exc:  # noqa: BLE001 -- 쓰기 예외는 유효 조건 위반으로 기록한다
        out["writer_exception"] = f"{type(exc).__name__}: {exc}"


def reader_loop(samples: dict, stores: Stores, stop: threading.Event, out: dict, pause: float, wal_path: Path) -> None:
    nodes = samples["graph_hubs"] + samples["graph_random"]
    packs = samples.get("bm25_packs") or []
    terms = samples.get("bm25_terms_mid") or []
    k = 0
    while not stop.is_set():
        try:
            nid = nodes[k % len(nodes)]
            ms, _ = timed(lambda: stores.graph.find_neighbors(nid, "both", 3, GRAPH_LIMIT))
            out["graph_bfs"].append(ms)
            if packs and terms:
                term = terms[k % len(terms)]
                ms, _ = timed(lambda: stores.docs.node_bm25_search(term, pack_ids=packs, limit=20))
                out["bm25_node"].append(ms)
        except Exception as exc:  # noqa: BLE001
            out["failures"].append(f"reader {type(exc).__name__}: {exc}")
        try:
            out["wal_max"] = max(out["wal_max"], wal_path.stat().st_size)
        except OSError:
            pass
        k += 1
        time.sleep(pause)


def billing_probe_loop(path: Path, stop: threading.Event, out: dict, pause: float) -> None:
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)  # 제품 busy_timeout과 같은 5초
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS bench_probe_events (id INTEGER PRIMARY KEY, ts REAL)")
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO bench_probe_events(ts) VALUES (?)", (time.time(),))
                conn.execute("COMMIT")
                out["billing"].append((time.perf_counter() - t0) * 1000.0)
            except sqlite3.Error as exc:
                out["failures"].append(f"billing {type(exc).__name__}: {exc}")
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            time.sleep(pause)
    finally:
        conn.close()


def cmd_concurrent(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    check_dirs(d)
    if args.layout == "five" and args.billing_target == "same":
        print(json.dumps({"error": "billing-target same needs the single layout"}))
        return 2
    state0 = machine_state()
    if state0["load1"] > args.max_load1:
        print(json.dumps({"noisy": True, "reason": "load1 above limit at start", "state": state0}))
        return 4
    os.environ["OPENCRAB_BM25_CACHE"] = "1" if args.bm25_cache == "on" else "0"
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    stores = Stores(args.layout, d, args.single_name)
    try:
        counts_start = restore_copy(stores, d / f"baseline-counts-{args.layout}.json")
    except BenchError as exc:
        stores.close()
        print(json.dumps({"error": str(exc)}))
        return 1
    load_query_vectors(stores, samples)
    stores.ef.delay = args.embed_delay_s
    billing_path = stores.paths["graph"] if args.billing_target == "same" else d / "bench_billing_probe.db"
    wal_path = Path(str(stores.paths["graph"]) + "-wal")
    stop = threading.Event()
    wout: dict = {"unit_ms": [], "writer_exception": None}
    shared: dict = {"graph_bfs": [], "bm25_node": [], "billing": [], "failures": [], "wal_max": 0}
    threads = [
        threading.Thread(target=writer_loop, args=(stores, args.writer, stop, args.batch, wout, args.writer_pause)),
        threading.Thread(target=reader_loop, args=(samples, stores, stop, shared, args.reader_pause, wal_path)),
        threading.Thread(target=billing_probe_loop, args=(billing_path, stop, shared, args.reader_pause)),
    ]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join(timeout=180)
    wall = time.perf_counter() - t0
    stores.close()
    batches = len(wout["unit_ms"])
    min_batches = 3 if args.embed_delay_s >= 5 else 5
    lock_ratio = (sum(wout["unit_ms"]) / 1000.0) / args.seconds
    invalid = []
    if wout["writer_exception"]:
        invalid.append(f"writer exception: {wout['writer_exception']}")
    if batches < min_batches:
        invalid.append(f"completed units {batches} < {min_batches}")
    if lock_ratio < 0.30:
        invalid.append(f"unit time ratio {lock_ratio:.2f} < 0.30")
    if len(shared["graph_bfs"]) < 20:
        invalid.append("reader samples < 20")
    print(json.dumps({
        "mode": "concurrent", "layout": args.layout, "label": args.label, "writer": args.writer,
        "embed_delay_s": args.embed_delay_s, "bm25_cache": args.bm25_cache,
        "billing_target": args.billing_target, "seconds": round(wall, 1), "valid": not invalid,
        "invalid_reasons": invalid, "writer_batches": batches, "unit_time_ratio": round(lock_ratio, 3),
        "unit_ms": summarize(wout["unit_ms"]), "reader_graph_bfs": summarize(shared["graph_bfs"]),
        "reader_bm25_node": summarize(shared["bm25_node"]), "billing_insert": summarize(shared["billing"]),
        "failures": len(shared["failures"]), "failure_samples": shared["failures"][:3],
        "wal_max_bytes": shared["wal_max"], "counts_start": counts_start,
        "state_start": state0, "state_end": machine_state(),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }))
    return 0 if not invalid else 3


# ---------------------------------------------------------------- maintenance

def tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def db_files(layout: str, d: Path, single_name: str) -> list[Path]:
    """billing은 어느 구조에서도 따로 둔다. 다섯 구조는 다섯 파일, 단일 구조는 병합 파일과 billing 둘이다."""
    base = ([d / single_name] if layout == "single"
            else [d / n for n in ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")])
    return base + ([d / "billing.db"] if (d / "billing.db").exists() else [])


def cmd_restore(args: argparse.Namespace) -> int:
    """쓰기 실행이 남긴 시험 행(``bench-w-``)을 지우고 행 수를 기준과 대조한다."""
    d = Path(args.dir)
    check_dirs(d)
    stores = Stores(args.layout, d, args.single_name)
    try:
        counts = restore_copy(stores, d / f"baseline-counts-{args.layout}.json")
    except BenchError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        stores.close()
    print(json.dumps({"mode": "restore", "layout": args.layout, "restored_counts_match_baseline": True,
                      "counts": counts}))
    return 0


def cmd_maintenance(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    out_dir = Path(args.backup_dir) if args.backup_dir else d / "backup"
    check_dirs(d, out_dir)
    files = db_files(args.layout, d, args.single_name)
    sizes = [f.stat().st_size for f in files]
    need = {"backup": int(sum(sizes) * 1.1), "vacuum": int(max(sizes) * 2.2), "check": 0,
            "restore": int(sum(sizes) * 1.0)}[args.op]
    try:
        free_start = merge_tool.check_free(d, args.min_free_gib, need)
    except merge_tool.DiskFloorError as exc:
        print(json.dumps({"error": f"disk preflight: {exc}"}))
        return 3
    tmp_dir = Path(os.environ.get("SQLITE_TMPDIR") or os.environ.get("TMPDIR") or "/var/tmp")
    low = {"min_free": free_start, "tmp_peak": 0}
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            low["min_free"] = min(low["min_free"], shutil.disk_usage(d).free)
            low["tmp_peak"] = max(low["tmp_peak"], tree_bytes(tmp_dir))
            time.sleep(0.25)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    per_file: dict[str, float] = {}
    t_all = time.perf_counter()
    try:
        for f in files:
            t0 = time.perf_counter()
            if args.op == "backup":
                from opencrab.stores.backup import backup_sqlite

                out_dir.mkdir(parents=True, exist_ok=True)
                backup_sqlite(f, out_dir / f.name, deadline=time.monotonic() + args.deadline)
            elif args.op == "vacuum":
                conn = sqlite3.connect(str(f), isolation_level=None)
                conn.execute("PRAGMA temp_store=FILE")
                conn.execute("PRAGMA cache_size=-65536")
                conn.execute("VACUUM")
                conn.close()
            elif args.op == "check":
                conn = ro_conn(f)
                conn.execute("PRAGMA cache_size=-65536")
                res = conn.execute("PRAGMA quick_check").fetchall()
                conn.close()
                if res != [("ok",)]:
                    print(json.dumps({"error": "quick_check", "file": f.name, "result": str(res)[:200]}))
                    return 1
            elif args.op == "restore":
                dst = d / "restored" / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(out_dir / f.name, dst)
                conn = ro_conn(dst)
                conn.execute("PRAGMA cache_size=-65536")
                ok = conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
                conn.close()
                if not ok:
                    print(json.dumps({"error": "restored quick_check failed", "file": f.name}))
                    return 1
            per_file[f.name] = round((time.perf_counter() - t0) * 1000.0, 1)
    finally:
        stop.set()
        sampler.join(timeout=2)
    print(json.dumps({
        "mode": "maintenance", "op": args.op, "layout": args.layout, "label": args.label,
        "total_ms": round((time.perf_counter() - t_all) * 1000.0, 1), "per_file_ms": per_file,
        "files": len(files), "source_bytes": sum(sizes), "largest_bytes": max(sizes),
        "extra_disk_peak_bytes_statvfs": free_start - low["min_free"],
        "extra_disk_peak_bytes_tmpdir": low["tmp_peak"], "free_bytes_at_start": free_start,
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "state_end": machine_state(),
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--layout", choices=("five", "single"), required=True)
    common.add_argument("--dir", required=True, help="시험 사본 디렉터리")
    common.add_argument("--single-name", default="merged.db")
    common.add_argument("--label", default="")
    common.add_argument("--max-load1", type=float, default=MAX_LOAD1)
    s = sub.add_parser("samples", parents=[common])
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=671)
    s.add_argument("--hubs", type=int, default=5)
    s.add_argument("--randoms", type=int, default=15)
    s.add_argument("--vector-queries", type=int, default=20)
    s.add_argument("--terms", type=int, default=7)
    lt = sub.add_parser("latency", parents=[common])
    lt.add_argument("--samples", required=True)
    lt.add_argument("--rounds", type=int, default=5)
    lt.add_argument("--bm25-cache", choices=("on", "off"), default="on")
    lt.add_argument("--kinds", default="", help="쉼표로 구분한 질의 종류만 잰다(기본은 전부)")
    lt.add_argument("--ann", choices=("", "binary"), default="",
                    help="binary: 벡터 전역 질의가 이진 양자화 2단계 경로를 쓴다(embedding_bit 열이 있어야 한다)")
    j = sub.add_parser("judge")
    j.add_argument("--s", nargs="+", required=True)
    j.add_argument("--f1", nargs="+", required=True)
    j.add_argument("--f2", nargs="*", default=[])
    c = sub.add_parser("concurrent", parents=[common])
    c.add_argument("--samples", required=True)
    c.add_argument("--seconds", type=float, default=30.0)
    c.add_argument("--writer", choices=("reingest", "vecgraph"), default="reingest")
    c.add_argument("--batch", type=int, default=64)
    c.add_argument("--embed-delay-s", type=float, default=0.0)
    c.add_argument("--billing-target", choices=("separate", "same"), default="separate")
    c.add_argument("--writer-pause", type=float, default=0.0)
    c.add_argument("--reader-pause", type=float, default=0.05)
    c.add_argument("--bm25-cache", choices=("on", "off"), default="on")
    sub.add_parser("restore", parents=[common])
    m = sub.add_parser("maintenance", parents=[common])
    m.add_argument("--op", choices=("backup", "vacuum", "check", "restore"), required=True)
    m.add_argument("--backup-dir", default="")
    m.add_argument("--deadline", type=float, default=1200.0)
    m.add_argument("--min-free-gib", type=float, default=30.0)
    args = p.parse_args(argv)
    return {"samples": cmd_samples, "latency": cmd_latency, "judge": cmd_judge,
            "concurrent": cmd_concurrent, "maintenance": cmd_maintenance,
            "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
