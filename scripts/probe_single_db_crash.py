#!/usr/bin/env python3
"""쓰기 도중 SIGKILL 시험: 다섯 파일 구조(대조군)와 단일 파일의 고아 행을 센다 (#671).

시험 대상은 청크 적재 경로(``opencrab.pack.load.load_chunks``)다. 이 경로는 벡터 배치를
쓴 뒤 doc 행을 쓴다. 자식 프로세스가 실제 ``load_chunks``를 돌리고, 지정한 지점에서 부모가
자식에게 SIGKILL을 보낸다. 그 뒤 순수 sqlite 연결이 파일을 열어 고아를 센다(제품 저장소 클래스는
계수가 끝나기 전에 열지 않는다. ``_init_db``의 복구성 쓰기가 고아를 가리지 않게 하려는 것이다).

- ``--layout five``: 벡터와 doc이 서로 다른 파일(``vectors.db``, ``doc_store.db``)에 따로 커밋된다.
- ``--layout single``: 둘이 한 파일을 공유하고 ``load_chunks``가 한 트랜잭션으로 쓴다
  (``opencrab.stores._single_file``).

고아의 정의(합성 적재 경로 한정. builder가 쓰는 노드 벡터는 정상적으로 ``doc_sources`` 행이 없으므로
실데이터 대사에 쓰지 않는다):

- ``vec_without_doc``: 벡터 행이 있는데 같은 id의 ``doc_sources`` 행이 없다.
- ``doc_without_vec``: 그 반대.
- ``fts_without_doc``: ``doc_sources_fts`` 행이 있는데 ``doc_sources`` 행이 없다.
- ``doc_without_fts``: 그 반대.

중단 지점(결정적이다. 배치 크기를 B, doc 쓰기 완료 건수를 K라 한다):

- ``--kill-after-docs K``: doc 쓰기 K건이 끝난 직후이고 (K+1)번째 doc 쓰기 직전. K는 적재 전체를 통틀어 센다.
  K=B는 첫 배치 commit 직후, 둘째 배치의 첫 doc 쓰기 직전이다.
- ``--kill-after-last-doc``: 첫 배치의 마지막(B번째) doc 쓰기가 끝난 직후이고 commit 전.
- ``--none``: 죽이지 않고 완주시킨다(시험 자체의 대조).

자식은 신호를 보내기 직전에 **자기 연결에서** 보이는 벡터 행 수, doc 행 수, ``in_transaction``을
파이프로 넘긴다. 열린 트랜잭션에 쌓인 쓰기가 있는 채로 죽었다는 증거다.

사용법:
    python probe_single_db_crash.py run --layout five --kill-after-docs 2 --dir <새 임시 디렉터리>
    python probe_single_db_crash.py run --layout single --kill-after-last-doc --dir <새 임시 디렉터리>
    python probe_single_db_crash.py run --layout five --none --dir <새 임시 디렉터리>

출력은 JSON 한 줄이다. 모든 데이터는 합성이며 ``--dir`` 아래에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIM = 8
PACK = "crash-pack"


def _embed(texts: list[str]) -> list[list[float]]:
    return [[float(len(t) % 7) + i * 0.01 for i in range(DIM)] for t in texts]


def _paths(layout: str, d: Path) -> tuple[Path, Path]:
    """(doc 파일, 벡터 파일)."""
    if layout == "single":
        return d / "single.db", d / "single.db"
    return d / "doc_store.db", d / "vectors.db"


def _supported_single_fixture(directory: Path, *, dim: int) -> Path:
    """실제 이관기가 만든 빈 지원 스키마를 합성 시험에 제공한다."""
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore
    from scripts.merge_sqlite_stores import FILES, finalize, merge, plan

    source = directory / "fixture-source"
    source.mkdir()
    stores = [
        LocalGraphStore(str(source / "graph.db")),
        LocalSQLDocStore(str(source / "doc_store.db")),
        SqliteVecStore(str(source / "vectors.db"), embedding_function=_embed, dim=dim),
        SQLStore(f"sqlite:///{source / 'opencrab.db'}"),
        SQLStore(f"sqlite:///{source / 'billing.db'}"),
    ]
    for store in reversed(stores):
        store.close()
    output = directory / "single.db"
    layout = plan(source, FILES, immutable=True)
    assert not layout["collisions"]
    result = merge(source, output, FILES, layout, immutable=True, cache_kib=1024,
                   min_free_gib=0, log=lambda _: None)
    finalize(Path(result["partial"]), output)
    return output


def child(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    os.environ["LOCAL_DATA_DIR"] = str(d)
    sys.path.insert(0, str(REPO))
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.pack import load as pack_load
    from opencrab.pack.ownership import create_pack
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    doc_path, vec_path = _paths(args.layout, d)
    shared = None
    if args.layout == "single":
        from opencrab.stores._single_file import SharedSqliteFile

        _supported_single_fixture(d, dim=DIM)
        shared = SharedSqliteFile(str(doc_path))
    kw = {"shared": shared} if shared is not None else {}
    docs = LocalSQLDocStore(str(doc_path), **kw)
    vec = SqliteVecStore(str(vec_path), embedding_function=_embed, dim=DIM, **kw)
    sql_path = doc_path if shared is not None else d / "opencrab.db"
    sql = SQLStore(f"sqlite:///{sql_path}", **kw)
    user_id = create_user(sql, "crash-owner", is_local=True)
    create_pack(sql, user_id, PACK)

    chunks = d / "chunks.jsonl"
    with chunks.open("w", encoding="utf-8") as fh:
        for i in range(args.batches * args.batch_size):
            row = {"id": f"chunk-{i}", "text": f"합성 본문 {i} alpha beta", "document_id": f"doc-{i}",
                   "metadata": {}}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    state = {"docs": 0}
    original = docs.upsert_source

    def signal_parent() -> None:
        """자기 연결에서 보이는 상태를 넘기고 멈춘다(부모가 SIGKILL을 보낸다)."""
        info = {
            "vec_in_txn": vec._conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0],
            "doc_in_txn": docs._conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
            "in_transaction": bool(docs._conn.in_transaction),
        }
        os.write(args.ready_fd, (json.dumps(info) + "\n").encode())
        time.sleep(3600)

    def hooked(source_id, text, metadata):  # noqa: ANN001
        n = state["docs"]
        if args.pause_after_docs is not None and n == args.pause_after_docs:
            signal_parent()
        result = original(source_id, text, metadata)
        state["docs"] = n + 1
        if args.pause_after_last_doc and state["docs"] == args.batch_size:
            signal_parent()
        return result

    docs.upsert_source = hooked  # type: ignore[method-assign]
    principal = Principal(user_id=user_id, is_local=True, disabled=False)
    with principal_scope(principal):
        ok, err = pack_load.load_chunks(PACK, chunks, vec, docs, batch_size=args.batch_size, sql=sql)
    print(json.dumps({"child_done": True, "ok": ok, "err": err}), flush=True)
    return 0


def _wal_bytes(*paths: Path) -> int:
    seen: set[Path] = set()
    total = 0
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        wal = Path(f"{p}-wal")
        if wal.exists():
            total += wal.stat().st_size
    return total


def inspect(layout: str, d: Path) -> dict:
    """순수 sqlite 연결로 센다. WAL 복구는 이 연결이 연다. 두 구조에 같은 함수를 쓴다."""
    doc_path, vec_path = _paths(layout, d)
    import sqlite_vec

    result: dict = {"layout": layout, "wal_bytes_before_open": _wal_bytes(doc_path, vec_path)}

    def connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    vec_ids: set[str] = set()
    doc_ids: set[str] = set()
    fts_ids: set[str] = set()
    if vec_path.exists():
        vconn = connect(vec_path)
        try:
            vec_ids = {r[0] for r in vconn.execute("SELECT node_id FROM vectors_kure")}
        except sqlite3.OperationalError:
            pass
        result["integrity"] = vconn.execute("PRAGMA integrity_check").fetchall()[0][0]
        if layout == "single":
            try:
                vconn.execute("INSERT INTO doc_sources_fts(doc_sources_fts) VALUES('integrity-check')")
                result["fts_integrity"] = "ok"
            except sqlite3.DatabaseError as exc:
                result["fts_integrity"] = type(exc).__name__
        if vec_ids:
            q = vconn.execute("SELECT embedding FROM vectors_kure LIMIT 1").fetchone()[0]
            result["knn_rows"] = len(vconn.execute(
                "SELECT node_id FROM vectors_kure WHERE embedding MATCH ? AND k=3", (q,)).fetchall())
        vconn.close()
    if doc_path.exists():
        dconn = connect(doc_path)
        try:
            doc_ids = {r[0] for r in dconn.execute("SELECT source_id FROM doc_sources")}
            fts_ids = {r[0] for r in dconn.execute("SELECT source_id FROM doc_sources_fts")}
        except sqlite3.OperationalError:
            pass
        if layout == "five":
            try:
                dconn.execute("INSERT INTO doc_sources_fts(doc_sources_fts) VALUES('integrity-check')")
                result["fts_integrity"] = "ok"
            except sqlite3.DatabaseError as exc:
                result["fts_integrity"] = type(exc).__name__
        dconn.close()
    result.update({
        "vec_rows": len(vec_ids),
        "doc_rows": len(doc_ids),
        "vec_without_doc": len(vec_ids - doc_ids),
        "doc_without_vec": len(doc_ids - vec_ids),
        "fts_without_doc": len(fts_ids - doc_ids),
        "doc_without_fts": len(doc_ids - fts_ids),
    })
    return result


def run(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    if d.exists() and any(d.iterdir()):
        print("refusing: --dir must be empty or absent", file=sys.stderr)
        return 2
    d.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "child", "--layout", args.layout,
        "--dir", str(d), "--batches", str(args.batches), "--batch-size", str(args.batch_size),
        "--ready-fd", str(write_fd),
    ]
    if not args.none:
        if args.kill_after_last_doc:
            cmd += ["--pause-after-last-doc"]
        else:
            cmd += ["--pause-after-docs", str(args.kill_after_docs)]
    proc = subprocess.Popen(cmd, pass_fds=(write_fd,), stdout=subprocess.PIPE, text=True)
    os.close(write_fd)
    killed = False
    signal_info: dict | None = None
    deadline = time.monotonic() + args.timeout
    try:
        if args.none:
            proc.communicate(timeout=args.timeout)
        else:
            ready, _, _ = select.select([read_fd], [], [], args.timeout)
            if not ready:
                proc.kill()
                print(json.dumps({"error": "child never signalled", "layout": args.layout}))
                return 1
            signal_info = json.loads(os.read(read_fd, 4096).decode().strip().splitlines()[0])
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
                killed = True
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        os.close(read_fd)
    result = inspect(args.layout, d)
    result.update({
        "killed": killed,
        "child_returncode": proc.returncode,
        "kill_after_docs": None if args.none or args.kill_after_last_doc else args.kill_after_docs,
        "kill_after_last_doc": bool(args.kill_after_last_doc and not args.none),
        "signal_state": signal_info,
        "expected_rows": args.batches * args.batch_size,
    })
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--layout", choices=("five", "single"), required=True)
    common.add_argument("--dir", required=True)
    common.add_argument("--batches", type=int, default=2)
    common.add_argument("--batch-size", type=int, default=8)
    p_run = sub.add_parser("run", parents=[common])
    p_run.add_argument("--kill-after-docs", type=int, default=2)
    p_run.add_argument("--kill-after-last-doc", action="store_true")
    p_run.add_argument("--none", action="store_true", help="죽이지 않고 완주시킨다")
    p_run.add_argument("--timeout", type=float, default=120.0)
    p_child = sub.add_parser("child", parents=[common])
    p_child.add_argument("--ready-fd", type=int, required=True)
    p_child.add_argument("--pause-after-docs", type=int, default=None)
    p_child.add_argument("--pause-after-last-doc", action="store_true")
    args = parser.parse_args(argv)
    return child(args) if args.cmd == "child" else run(args)


if __name__ == "__main__":
    sys.exit(main())
