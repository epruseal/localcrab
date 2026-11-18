#!/usr/bin/env python3
"""시제품: local 모드의 SQLite 파일 여럿을 한 파일로 합친다 (#671).

대상은 graph.db, doc_store.db, vectors.db, opencrab.db 네 파일이다. billing.db는 대상이
아니다. billing_events와 idx_billing_tenant가 opencrab.db와 이름이 겹쳐 합칠 수 없고, 이름을
바꿔야 하는 병합은 전환 설계 4단계의 항목이다. 두 번째 파일이 같은 이름의 테이블이나 인덱스를
가지면 합치지 않고 중단한다.

원본은 읽기 전용으로만 연다. 결과는 ``<out>.partial``에 만들고 ``<out>``으로 이름을 바꿔
게시한다. ``--verify``를 주면 검증을 통과해야 게시한다. ``--verify`` 없이 ``--apply``만 주면
검증 없이 게시하므로 운영에서는 ``--verify``를 함께 준다. 중단되면 ``.partial``이 남는다.
다음 실행은 그것을 거부하므로 운영자가 지운다(재개 절차는 없다).

WAL 모드 원본을 읽기 전용으로 열어도 SQLite가 원본 옆에 빈 ``-wal``과 ``-shm`` 곁파일을
만들 수 있다. 원본 데이터는 바뀌지 않는다. ``--immutable-source``를 주면 곁파일도 만들지
않지만 WAL이 남은 원본에서는 오래된 내용을 읽으므로 WAL이 없는 정지된 스냅숏에만 쓴다.

사용법(계획만 출력한다. 출력 파일을 만들지 않는다):
    python merge_sqlite_stores.py --source-dir <디렉터리> --out <새 파일>

실제 병합:
    python merge_sqlite_stores.py --source-dir <디렉터리> --out <새 파일> --apply --verify

복사 방식: 원본을 ATTACH하고 테이블마다 INSERT ... SELECT를 한다. fts5와 vec0
가상 테이블은 원본의 CREATE VIRTUAL TABLE 문으로 만든 뒤 그림자 테이블의
내용을 덮어쓴다. 인덱스와 트리거는 데이터를 옮긴 뒤 만든다. 대량 복사 중에는
출력에 journal_mode=OFF와 synchronous=OFF를 건다. 복사가 끝나면 WAL 모드로 바꾸고
(``--verify``를 줬다면 검증을 통과한 뒤) 파일 fsync, 이름 바꾸기, 디렉터리 fsync 순서로 게시한다.

안전 점검(운영 장치에서 돌리는 도구이므로 시작 전과 복사 중에 건다):

- 임시 디렉터리와 출력 경로가 tmpfs나 ramfs이면 거부한다(메모리를 쓴다).
- 필요량 추정(원본 합계 x 1.3)을 빼고도 여유가 하한(``--min-free-gib``, 기본 30)보다 작으면 시작하지 않는다.
- 테이블 하나를 복사할 때마다 여유를 보고, 하한 아래로 내려가거나 디스크가 차면 ``.partial``을 지우고 끝낸다.

종료 코드: 0 성공, 1 병합이나 검증 실패, 2 사용법과 사전 점검 실패(이름 충돌 포함),
3 디스크 여유 하한 위반이나 디스크 가득 참(``.partial``은 지워진다).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

FILES = ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")
GIB = 1024 ** 3
MERGE_SPACE_FACTOR = 1.3  # 원본 합계에 곱한다. 인덱스 구축 여유를 포함한다.
RAM_FS_TYPES = {"tmpfs", "ramfs"}

# 그림자 테이블 접미사. 인식하지 못한 접미사를 가진 테이블은 일반 테이블로 취급하지 않고
# 중단한다(그림자인지 일반 테이블인지 모르는 채 덮어쓰면 데이터가 어긋난다).
_SHADOW_SUFFIXES = {
    "fts5": {"data", "idx", "content", "docsize", "config"},
    "vec0": {"chunks", "rowids", "info", "auxiliary"},
}
_SHADOW_PATTERNS = {
    "vec0": re.compile(r"^(vector_chunks|metadatachunks|metadatatext)\d+$"),
}
_MODULE_RE = re.compile(r"USING\s+(\w+)", re.IGNORECASE)


class MergeError(RuntimeError):
    """병합이나 사전 점검의 실패. 메시지에 원인을 담는다."""


class DiskFloorError(MergeError):
    """디스크 여유가 하한 아래이거나 디스크가 가득 찼다. 종료 코드 3."""


def fs_type(path: Path) -> str:
    """``/proc/mounts``에서 경로를 덮는 가장 긴 마운트 지점의 파일시스템 종류를 찾는다."""
    target = str(path.resolve())
    best_len, best_type = -1, "unknown"
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unknown"
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) > best_len:
            best_len, best_type = len(mount), parts[2]
    return best_type


def refuse_ram_fs(label: str, path: Path) -> None:
    kind = fs_type(path)
    if kind in RAM_FS_TYPES:
        raise MergeError(f"{label} is on {kind} (memory); refusing")


def check_free(path: Path, floor_gib: float, need_bytes: int = 0) -> int:
    """필요량을 빼고도 여유가 하한 이상인지 본다. 여유 바이트를 돌려준다."""
    free = shutil.disk_usage(path).free
    if free - need_bytes < floor_gib * GIB:
        raise DiskFloorError(
            f"free space would fall below the {floor_gib} GiB floor "
            f"(free {free / GIB:.1f} GiB, need {need_bytes / GIB:.1f} GiB)")
    return free


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _file_uri(path: Path, *, mode: str, immutable: bool = False) -> str:
    query = f"mode={mode}" + ("&immutable=1" if immutable else "")
    return f"file:{urllib.parse.quote(str(path.resolve()))}?{query}"


def _connect(path: Path, *, mode: str, immutable: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(_file_uri(path, mode=mode, immutable=immutable), uri=True,
                           isolation_level=None)
    return conn


def _load_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:  # vec0 가상 테이블이 없는 원본이면 필요 없다
        raise MergeError("sqlite_vec is not importable") from exc
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _module_of(sql: str) -> str | None:
    match = _MODULE_RE.search(sql or "")
    return match.group(1).lower() if match else None


def inspect_source(path: Path, *, immutable: bool) -> dict[str, Any]:
    """원본 하나의 객체 목록을 분류한다. 읽기 전용 연결만 연다."""
    conn = _connect(path, mode="ro", immutable=immutable)
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
        ).fetchall()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()

    virtual = {
        name: _module_of(sql)
        for typ, name, _tbl, sql in rows
        if typ == "table" and sql and sql.upper().startswith("CREATE VIRTUAL")
    }
    shadow_of: dict[str, str] = {}
    for typ, name, _tbl, sql in rows:
        if typ != "table" or name in virtual or name.startswith("sqlite_"):
            continue
        # 이름이 겹치는 가상 테이블이 둘 이상일 수 있다(예: notes와 notes_tags). 모든 가상 테이블을
        # 본 뒤에 판정해야 만든 순서와 무관하다. 알려진 접미사로 맞는 주인이 있으면 그림자다.
        # 접두어만 맞고 주인이 없으면 추측하지 않고 중단한다. 접두어가 맞지 않으면 일반 테이블이다.
        prefixed = []
        for vname, module in virtual.items():
            prefix = vname + "_"
            if not name.startswith(prefix):
                continue
            prefixed.append((vname, module))
            suffix = name[len(prefix):]
            known = suffix in _SHADOW_SUFFIXES.get(module or "", set())
            pattern = _SHADOW_PATTERNS.get(module or "")
            if known or (pattern is not None and pattern.match(suffix)):
                shadow_of[name] = vname
                break
        if prefixed and name not in shadow_of:
            vname, module = prefixed[0]
            raise MergeError(
                f"{path.name}: table {name!r} starts with virtual table {vname!r} but its "
                f"suffix {name[len(vname) + 1:]!r} is not a known {module} shadow suffix; "
                "refusing to guess")
    regular = [
        name for typ, name, _tbl, sql in rows
        if typ == "table" and name not in virtual and name not in shadow_of
        and not name.startswith("sqlite_")
    ]
    objects = {
        "virtual": virtual,
        "shadow": shadow_of,
        "regular": regular,
        "ddl": {name: sql for typ, name, _tbl, sql in rows if typ == "table"},
        "indexes": [(name, sql) for typ, name, _tbl, sql in rows
                    if typ == "index" and sql and not name.startswith("sqlite_autoindex")],
        "triggers": [(name, sql) for typ, name, _tbl, sql in rows if typ == "trigger"],
        "has_sequence": any(name == "sqlite_sequence" for typ, name, _t, _s in rows),
        "has_stat1": any(name == "sqlite_stat1" for typ, name, _t, _s in rows),
        "page_size": page_size,
        "bytes": page_size * page_count,
        "free_bytes": page_size * freelist,
    }
    return objects


def plan(source_dir: Path, files: list[str], *, immutable: bool) -> dict[str, Any]:
    """파일별 분류와 이름 충돌을 계산한다. 출력 파일을 쓰지 않는다."""
    sources: dict[str, dict[str, Any]] = {}
    for name in files:
        path = source_dir / name
        if not path.is_file():
            raise MergeError(f"source file missing: {name}")
        sources[name] = inspect_source(path, immutable=immutable)

    seen: dict[str, str] = {}
    collisions: list[dict[str, str]] = []
    for fname, info in sources.items():
        names = (
            list(info["virtual"]) + list(info["shadow"]) + info["regular"]
            + [n for n, _ in info["indexes"]] + [n for n, _ in info["triggers"]]
        )
        for obj in names:
            if obj in seen:
                collisions.append({"object": obj, "first": seen[obj], "second": fname})
            else:
                seen[obj] = fname
    page_sizes = {info["page_size"] for info in sources.values()}
    return {
        "files": files,
        "sources": sources,
        "collisions": collisions,
        "page_sizes": sorted(page_sizes),
        "source_bytes": sum(info["bytes"] for info in sources.values()),
    }


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({_quote(table)})")]


def _needs_physical_rowid(conn: sqlite3.Connection, table: str, ddl: str) -> bool:
    """INTEGER PRIMARY KEY 별칭이 없는 rowid 테이블은 물리 rowid를 따로 옮겨야 한다.

    vec0 그림자 테이블 ``vector_chunks00(rowid PRIMARY KEY, ...)``은 열 이름이 rowid여도 별칭이 아니다.
    sqlite-vec은 청크 번호를 물리 rowid로 blob을 연다. 열만 옮기면 물리 rowid가 1부터 새로 매겨져
    청크가 삭제된 적 있는 원본에서 조회가 깨진다. 실제 스냅숏에서 발견했다.
    """
    if re.search(r"\bWITHOUT\s+ROWID\b", ddl or "", re.IGNORECASE):
        return False
    pks = [row for row in conn.execute(f"PRAGMA main.table_info({_quote(table)})") if row[5]]
    alias = len(pks) == 1 and (pks[0][2] or "").upper() == "INTEGER"
    return not alias


def _copy_table(conn: sqlite3.Connection, table: str, *, clear_first: bool, ddl: str = "") -> int:
    cols = _columns(conn, "main", table)
    if not cols:
        raise MergeError(f"table has no columns in output: {table}")
    column_list = ", ".join(_quote(c) for c in cols)
    select_list = column_list
    if _needs_physical_rowid(conn, table, ddl):
        # 열 이름이 rowid인 표에서도 _rowid_는 물리 rowid를 가리킨다.
        column_list = "_rowid_, " + column_list
        select_list = "_rowid_, " + select_list
    conn.execute("BEGIN")
    try:
        if clear_first:
            conn.execute(f"DELETE FROM main.{_quote(table)}")
        cur = conn.execute(
            f"INSERT INTO main.{_quote(table)} ({column_list}) "
            f"SELECT {select_list} FROM src.{_quote(table)}"
        )
        copied = cur.rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return copied


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def merge(
    source_dir: Path,
    out: Path,
    files: list[str],
    pl: dict[str, Any],
    *,
    immutable: bool,
    cache_kib: int,
    min_free_gib: float,
    log: Any,
) -> dict[str, Any]:
    partial = out.with_name(out.name + ".partial")
    if out.exists():
        raise MergeError("output exists; refusing to overwrite")
    if partial.exists():
        raise MergeError("a .partial output exists; remove it first")
    page_size = pl["page_sizes"][0]
    report: dict[str, Any] = {"phases": [], "tables": {}}
    free_min = shutil.disk_usage(out.parent).free

    def mark(phase: str, started: float) -> None:
        nonlocal free_min
        size = os.path.getsize(partial) if partial.exists() else 0
        free_min = min(free_min, shutil.disk_usage(out.parent).free)
        entry = {"phase": phase, "seconds": round(time.monotonic() - started, 3),
                 "out_bytes": size, "rss_peak_mib": round(_rss_mib(), 1)}
        report["phases"].append(entry)
        log(json.dumps(entry))

    needs_vec = any(
        module == "vec0" for info in pl["sources"].values() for module in info["virtual"].values()
    )
    conn = _connect(partial, mode="rwc")
    try:
        if needs_vec:
            _load_vec(conn)
        conn.execute(f"PRAGMA page_size={page_size}")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        conn.execute("PRAGMA mmap_size=0")
        if any(info["has_stat1"] for info in pl["sources"].values()):
            # sqlite_stat1은 예약 이름이라 CREATE TABLE로 만들 수 없다. 빈 출력에서 ANALYZE를 돌리면 빈 표가 생긴다.
            conn.execute("ANALYZE main")
        post_indexes: list[tuple[str, str]] = []
        post_triggers: list[tuple[str, str]] = []
        for fname in files:
            info = pl["sources"][fname]
            started = time.monotonic()
            conn.execute(
                "ATTACH DATABASE ? AS src",
                (_file_uri(source_dir / fname, mode="ro", immutable=immutable),),
            )
            try:
                for vname in info["virtual"]:
                    conn.execute(info["ddl"][vname])
                for tname in info["regular"]:
                    conn.execute(info["ddl"][tname])
                order = info["regular"] + list(info["shadow"])
                for tname in order:
                    t0 = time.monotonic()
                    copied = _copy_table(conn, tname, clear_first=tname in info["shadow"],
                                         ddl=info["ddl"].get(tname, ""))
                    report["tables"][tname] = {
                        "file": fname, "rows": copied,
                        "seconds": round(time.monotonic() - t0, 3),
                    }
                    check_free(out.parent, min_free_gib)
                if info["has_sequence"]:
                    for name, seq in conn.execute("SELECT name, seq FROM src.sqlite_sequence").fetchall():
                        cur = conn.execute(
                            "UPDATE main.sqlite_sequence SET seq=max(seq, ?) WHERE name=?", (seq, name))
                        if cur.rowcount == 0:
                            conn.execute("INSERT INTO main.sqlite_sequence(name, seq) VALUES (?, ?)",
                                         (name, seq))
                if info["has_stat1"]:
                    conn.execute("INSERT INTO main.sqlite_stat1 SELECT tbl, idx, stat FROM src.sqlite_stat1")
            finally:
                conn.execute("DETACH DATABASE src")
            post_indexes += info["indexes"]
            post_triggers += info["triggers"]
            mark(f"copy:{fname}", started)
        started = time.monotonic()
        for _name, sql in post_indexes:
            conn.execute(sql)
            check_free(out.parent, min_free_gib)
        mark("indexes", started)
        started = time.monotonic()
        for _name, sql in post_triggers:
            conn.execute(sql)
        mark("triggers", started)
    except BaseException:
        conn.close()
        raise
    conn.close()
    report["free_bytes_min"] = free_min
    report["partial"] = str(partial)
    return report


VEC_SAMPLES = 20


def _vec_samples(conn: sqlite3.Connection, vname: str, count: int) -> list[dict[str, Any]]:
    """vec0 표본: ``<이름>_rowids`` 그림자 테이블의 rowid를 같은 간격으로 골라 질의 벡터를 만든다."""
    rowids = _quote(vname + "_rowids")
    top = conn.execute(f"SELECT max(rowid) FROM main.{rowids}").fetchone()[0] or 0
    cols = {row[1] for row in conn.execute(f"PRAGMA main.table_info({_quote(vname)})")}
    pack_col = "pack_id" in cols
    picked: list[dict[str, Any]] = []
    for i in range(count):
        start = (top * i) // count + 1
        row = conn.execute(
            f"SELECT id FROM main.{rowids} WHERE rowid >= ? ORDER BY rowid LIMIT 1", (start,)).fetchone()
        if row is None:
            continue
        sel = "embedding, pack_id" if pack_col else "embedding"
        got = conn.execute(f"SELECT {sel} FROM main.{_quote(vname)} WHERE node_id = ?", (row[0],)).fetchone()
        if got is None:
            continue
        picked.append({"node_id": row[0], "embedding": got[0], "pack": got[1] if pack_col else None})
    return picked


def verify(
    source_dir: Path,
    out_path: Path,
    files: list[str],
    pl: dict[str, Any],
    report: dict[str, Any],
    *,
    immutable: bool,
    cache_kib: int,
    log: Any,
) -> dict[str, Any]:
    """출력의 행 수를 원본과 대조하고 무결성 검사를 한다. 출력은 쓰지 않는다."""
    result: dict[str, Any] = {"count_mismatch": [], "fts_errors": [], "vec_mismatch": []}
    started = time.monotonic()
    conn = _connect(out_path, mode="ro")
    try:
        needs_vec = any(m == "vec0" for i in pl["sources"].values() for m in i["virtual"].values())
        if needs_vec:
            _load_vec(conn)
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        for tname, entry in report["tables"].items():
            src = _connect(source_dir / entry["file"], mode="ro", immutable=immutable)
            try:
                src_n = src.execute(f"SELECT count(*) FROM {_quote(tname)}").fetchone()[0]
            finally:
                src.close()
            out_n = conn.execute(f"SELECT count(*) FROM {_quote(tname)}").fetchone()[0]
            if not (src_n == out_n == entry["rows"]):
                result["count_mismatch"].append(
                    {"table": tname, "source": src_n, "out": out_n, "copied": entry["rows"]})
        result["count_seconds"] = round(time.monotonic() - started, 3)
        log(json.dumps({"verify": "counts", "mismatch": len(result["count_mismatch"])}))

        started = time.monotonic()
        quick = conn.execute("PRAGMA quick_check").fetchall()
        result["quick_check"] = [row[0] for row in quick]
        result["quick_check_seconds"] = round(time.monotonic() - started, 3)
        log(json.dumps({"verify": "quick_check", "rows": result["quick_check"][:3]}))
    finally:
        conn.close()

    # fts5 integrity-check는 쓰기 연결이 필요하다. 게시 전 사본이므로 출력 파일이 대상이다.
    rw = _connect(out_path, mode="rw")
    try:
        if needs_vec:
            _load_vec(rw)
        started = time.monotonic()
        for info in pl["sources"].values():
            for vname, module in info["virtual"].items():
                if module != "fts5":
                    continue
                try:
                    rw.execute(f"INSERT INTO {_quote(vname)}({_quote(vname)}) VALUES('integrity-check')")
                except sqlite3.DatabaseError as exc:
                    result["fts_errors"].append({"table": vname, "error": type(exc).__name__})
        result["fts_integrity_seconds"] = round(time.monotonic() - started, 3)
        for fname in files:
            info = pl["sources"][fname]
            for vname, module in info["virtual"].items():
                if module != "vec0":
                    continue
                rw.execute("ATTACH DATABASE ? AS src",
                           (_file_uri(source_dir / fname, mode="ro", immutable=immutable),))
                try:
                    samples = _vec_samples(rw, vname, VEC_SAMPLES)
                except sqlite3.OperationalError as exc:
                    # 출력의 벡터 조회가 깨졌다는 뜻이다. 비정상 종료가 아니라 검증 실패로 남긴다.
                    result["vec_mismatch"].append({"table": vname, "error": str(exc)[:160]})
                    samples = []
                try:
                    for sample in samples:
                        for scoped in (False, True):
                            if scoped and sample["pack"] is None:
                                continue
                            where = " AND pack_id = ?" if scoped else ""
                            args = (sample["embedding"],) + ((sample["pack"],) if scoped else ())
                            sql = (f"SELECT node_id, distance FROM {{schema}}.{_quote(vname)} "
                                   f"WHERE embedding MATCH ? AND k=10{where}")
                            got_out = rw.execute(sql.format(schema="main"), args).fetchall()
                            got_src = rw.execute(sql.format(schema="src"), args).fetchall()
                            # id 순서와 거리가 정확히 같아야 한다(같은 바이트에서 계산한다).
                            if got_out != got_src or not got_out:
                                result["vec_mismatch"].append(
                                    {"table": vname, "node": sample["node_id"], "scoped": scoped})
                finally:
                    rw.execute("DETACH DATABASE src")
        log(json.dumps({"verify": "fts_vec", "fts_errors": len(result["fts_errors"]),
                        "vec_mismatch": len(result["vec_mismatch"])}))
    finally:
        rw.close()
    result["ok"] = (
        not result["count_mismatch"] and not result["fts_errors"] and not result["vec_mismatch"]
        and result["quick_check"] == ["ok"]
    )
    return result


def finalize(partial: Path, out: Path) -> dict[str, Any]:
    """WAL 모드로 바꾸고 체크포인트한 뒤 게시한다."""
    started = time.monotonic()
    conn = _connect(partial, mode="rw")
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(partial) + suffix)
        if side.exists():
            side.unlink()
    # 복사 중에는 synchronous=OFF였으므로 게시 전에 내용을 디스크에 내린다. 그다음 이름을 바꾸고
    # 디렉터리 항목도 내려야 전원이 끊겨도 반쪽짜리 파일이 게시되지 않는다.
    fd = os.open(partial, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, out)
    dir_fd = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return {"journal_mode": mode, "seconds": round(time.monotonic() - started, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="실제로 병합한다(기본은 계획만 출력)")
    parser.add_argument("--verify", action="store_true",
                        help="게시 전에 행 수, quick_check, fts5와 vec0 실조회를 대조한다(운영에서는 필수)")
    parser.add_argument("--immutable-source", action="store_true",
                        help="원본이 변하지 않는다고 선언한다(WAL이 없는 정지된 스냅숏 전용. 기본은 끔)")
    parser.add_argument("--cache-kib", type=int, default=65536)
    parser.add_argument("--min-free-gib", type=float, default=30.0,
                        help="이 여유 아래로 내려가면 시작하지 않거나 복사를 멈춘다")
    parser.add_argument("--report", type=Path, help="결과 JSON을 쓸 경로")
    args = parser.parse_args(argv)

    # SQLite의 정렬 임시 파일이 시스템 임시 디렉터리(메모리 tmpfs일 수 있다)로 가지 않게 한다.
    os.environ.setdefault("SQLITE_TMPDIR", str(args.out.resolve().parent))

    files = list(FILES)

    def log(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        refuse_ram_fs("output directory", args.out.resolve().parent)
        refuse_ram_fs("temp directory", Path(os.environ["SQLITE_TMPDIR"]))
        pl = plan(args.source_dir, files, immutable=args.immutable_source)
    except (MergeError, sqlite3.DatabaseError) as exc:
        log(f"plan failed: {exc}")
        return 2
    summary = {
        "files": files,
        "collisions": pl["collisions"],
        "page_sizes": pl["page_sizes"],
        "source_bytes": pl["source_bytes"],
        "objects": {
            name: {"regular": len(i["regular"]), "virtual": len(i["virtual"]),
                   "shadow": len(i["shadow"]), "indexes": len(i["indexes"]),
                   "triggers": len(i["triggers"]), "free_bytes": i["free_bytes"]}
            for name, i in pl["sources"].items()
        },
    }
    print(json.dumps(summary, indent=2))
    if pl["collisions"] or len(pl["page_sizes"]) != 1:
        log("refusing to merge: name collisions or mixed page sizes")
        return 2
    if not args.apply:
        return 0

    started_all = time.monotonic()
    out_dir = args.out.resolve().parent
    need = int(pl["source_bytes"] * MERGE_SPACE_FACTOR)
    try:
        free_before = check_free(out_dir, args.min_free_gib, need)
    except DiskFloorError as exc:
        log(f"disk preflight failed: {exc}")
        return 3
    partial_path = args.out.with_name(args.out.name + ".partial")
    existed_before = partial_path.exists()
    try:
        report = merge(args.source_dir, args.out, files, pl, immutable=args.immutable_source,
                       cache_kib=args.cache_kib, min_free_gib=args.min_free_gib, log=log)
    except (DiskFloorError, sqlite3.OperationalError, OSError) as exc:
        full = isinstance(exc, DiskFloorError) or "disk is full" in str(exc).lower() or (
            isinstance(exc, OSError) and getattr(exc, "errno", None) == 28)
        if full:
            if not existed_before:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    Path(str(partial_path) + suffix).unlink(missing_ok=True)
            log(f"merge stopped on disk space: {type(exc).__name__}: {exc}")
            return 3
        log(f"merge failed: {type(exc).__name__}: {exc}")
        return 1
    except (MergeError, sqlite3.DatabaseError) as exc:
        log(f"merge failed: {type(exc).__name__}: {exc}")
        return 1
    partial = Path(report["partial"])
    report["free_bytes_before"] = free_before
    if args.verify:
        vr = verify(args.source_dir, partial, files, pl, report,
                    immutable=args.immutable_source, cache_kib=args.cache_kib, log=log)
        report["verify"] = vr
        if not vr["ok"]:
            log("verify failed; output stays as .partial")
            if args.report:
                args.report.write_text(json.dumps(report, indent=2))
            return 1
    report["finalize"] = finalize(partial, args.out)
    report["total_seconds"] = round(time.monotonic() - started_all, 3)
    report["out_bytes"] = os.path.getsize(args.out)
    report["rss_peak_mib"] = round(_rss_mib(), 1)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "tables"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
