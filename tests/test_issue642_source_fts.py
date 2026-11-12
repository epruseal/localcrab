"""#642: source FTS rows are found by rowid, and orphan FTS rows are removed
only by an operator command.

The design, the test table and the reverse mutations are in
docs/issue-642-source-fts-lookup.md. The test names below carry the row of that
table they cover.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from opencrab.cli import main
from tests.test_cli import _parse_leading_json

INDEX = "idx_doc_sources_fts_source_id"
FTS_DELETE = "DELETE FROM doc_sources_fts"
ROWID_DELETE_MARKER = "WHERE rowid IN (SELECT id FROM doc_sources_fts_content"
PACKS = ["p", "q"]
QUERIES = ["alpha", "beta", "gamma", "delta", "alpha beta", "orphanword"]


def _fts5_available() -> bool:
    """Probe FTS5 without the store, so a store defect cannot look like a skip."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


_FTS5_AVAILABLE = _fts5_available()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open(path: Path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    if not _FTS5_AVAILABLE:
        pytest.skip("SQLite FTS5 is not available in this build")
    store = LocalSQLDocStore(str(path))
    # FTS5 works here, so a false _fts_ok is a bootstrap defect.
    assert store._fts_ok, "the store bootstrap turned the FTS index off"
    return store


def _raw_source(conn, source_id, text, pack="p"):
    conn.execute(
        "INSERT INTO doc_sources(source_id, text, metadata, ingested_at) VALUES (?, ?, ?, ?)",
        (source_id, text, json.dumps({"pack_id": pack}), "2026-01-01T00:00:00+00:00"),
    )


def _raw_fts(conn, source_id, text) -> int:
    return conn.execute(
        "INSERT INTO doc_sources_fts(source_id, text) VALUES (?, ?)", (source_id, text)
    ).lastrowid


def _content(conn):
    rows = conn.execute("SELECT id, c0, c1 FROM doc_sources_fts_content ORDER BY id")
    return [tuple(row) for row in rows]


def _index_present(conn) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
    ).fetchone()[0])


def _keyword_snapshot(store):
    hits = []
    for query in QUERIES:
        rows = store.keyword_search(query, pack_ids=PACKS, limit=20)
        hits.append([(r["source_id"], r["node_id"], r["score"], r["text"]) for r in rows])
        per = store.keyword_search_per_pack(query, pack_ids=PACKS, per_pack_limit=3)
        hits.append({p: [(r["source_id"], r["score"]) for r in rs] for p, rs in sorted(per.items())})
    return hits


def _drop_index(path: Path) -> None:
    """Turn a DB into the layout older code left: no lookup index."""
    conn = sqlite3.connect(str(path))
    conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    conn.commit()
    conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema(path: Path):
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()


def _capture_fts_deletes(store, action) -> list[str]:
    seen: list[str] = []
    conn = store._conn
    conn.set_trace_callback(seen.append)
    try:
        action()
    finally:
        conn.set_trace_callback(None)
    return [sql for sql in seen if sql.startswith(FTS_DELETE)]


def _fts_delete_steps(store, action) -> int:
    """VM steps while a top-level FTS delete runs.

    SQLite marks the statements that FTS5 runs inside a statement with a
    leading "-- " in the trace. Those steps count for the delete that is
    running.
    """
    conn = store._conn
    state = {"in_delete": False}
    count = [0]

    def trace(sql: str) -> None:
        if sql.startswith("-- "):
            return
        state["in_delete"] = sql.startswith(FTS_DELETE)

    def progress() -> int:
        if state["in_delete"]:
            count[0] += 1
        return 0

    conn.set_trace_callback(trace)
    conn.set_progress_handler(progress, 1)
    try:
        action()
    finally:
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 1)
    return count[0]


def _seed_sources(store) -> None:
    store.upsert_source("s1", "alpha beta", {"pack_id": "p"})
    store.upsert_source("s2", "alpha gamma", {"pack_id": "p"})
    store.upsert_source("s3", "beta delta", {"pack_id": "q"})


def _upsert(store):
    store.upsert_source("s1", "alpha beta changed", {"pack_id": "p"})


def _rewrite(store):
    state = store.read_source_state_exact("s2")
    store.rewrite_source_metadata("s2", {"pack_id": "p", "extra": 1}, state)


def _delete(store):
    store.delete_sources_with_state(["s1", "s3"])


ACTIONS = {"upsert": _upsert, "rewrite": _rewrite, "delete": _delete}


# ---------------------------------------------------------------------------
# criterion 1: no full FTS scan in the three delete sites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", sorted(ACTIONS))
def test_c1_fts_delete_plans_use_rowid_and_lookup_index(tmp_path, site):
    store = _open(tmp_path / "doc_store.db")
    try:
        _seed_sources(store)
        statements = _capture_fts_deletes(store, lambda: ACTIONS[site](store))
        assert statements, f"{site} ran no FTS delete"
        for sql in statements:
            plan = [r[-1] for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql)]
            fts = [d for d in plan if re.search(r"\bdoc_sources_fts VIRTUAL TABLE\b", d)]
            assert fts, plan
            # "INDEX 0:" with an empty idxStr is the unconstrained FTS scan.
            assert not any(re.search(r"VIRTUAL TABLE INDEX 0:$", d) for d in fts), plan
            assert any(
                d.startswith("SEARCH") and "doc_sources_fts_content" in d and INDEX in d
                for d in plan
            ), plan
    finally:
        store.close()


# ---------------------------------------------------------------------------
# criterion 2: the write cost does not grow with the FTS row count
# ---------------------------------------------------------------------------


def _store_with_filler(path: Path, filler: int):
    store = _open(path)
    _seed_sources(store)
    conn = store._conn
    conn.executemany(
        "INSERT INTO doc_sources_fts(source_id, text) VALUES (?, ?)",
        ((f"filler-{i}", "filler words only") for i in range(filler)),
    )
    conn.commit()
    return store


@pytest.mark.parametrize("site", sorted(ACTIONS) + ["insert"])
def test_c2_fts_delete_steps_do_not_grow_with_fts_rows(tmp_path, site):
    def action(store):
        if site == "insert":
            store.upsert_source("new-source", "brand new text", {"pack_id": "p"})
        else:
            ACTIONS[site](store)

    small = _store_with_filler(tmp_path / "small.db", 50)
    large = _store_with_filler(tmp_path / "large.db", 2000)
    try:
        small_steps = _fts_delete_steps(small, lambda: action(small))
        large_steps = _fts_delete_steps(large, lambda: action(large))
    finally:
        small.close()
        large.close()
    assert small_steps > 0
    assert large_steps == small_steps, (small_steps, large_steps)


# ---------------------------------------------------------------------------
# criterion 3a: the index creation keeps FTS rows and search results
# ---------------------------------------------------------------------------


def test_c3a_index_creation_keeps_fts_rows_and_results(tmp_path):
    path = tmp_path / "doc_store.db"
    store = _open(path)
    for i in range(10):
        text = "alpha beta" if i % 2 == 0 else "alpha gamma delta"
        store.upsert_source(f"s{i}", text, {"pack_id": "p" if i < 6 else "q"})
    store.delete_sources_with_state(["s2", "s6"])  # rowid gaps
    store.upsert_source("s3", "alpha beta", {"pack_id": "p"})  # moves s3 to a new rowid
    store.upsert_source("s10", "alpha beta", {"pack_id": "q"})
    store.close()
    _drop_index(path)
    raw = sqlite3.connect(str(path))
    before_content = _content(raw)
    raw.close()
    ids = [row[0] for row in before_content]
    assert ids != list(range(ids[0], ids[0] + len(ids))), "the fixture needs rowid gaps"
    legacy = _open_without_bootstrap_index(path)
    try:
        before_hits = _keyword_snapshot(legacy)
    finally:
        legacy.close()

    store = _open(path)
    try:
        assert _index_present(store._conn)
        assert _content(store._conn) == before_content
        assert _keyword_snapshot(store) == before_hits
    finally:
        store.close()


def _open_without_bootstrap_index(path: Path):
    """A store whose keyword reads run on the layout older code left.

    Opening a store creates the lookup index, so drop it again behind the
    open store before the reads.
    """
    store = _open(path)
    store._conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    store._conn.commit()
    assert not _index_present(store._conn)
    return store


# ---------------------------------------------------------------------------
# criterion 3b: the orphan condition is the complement of the search join
# ---------------------------------------------------------------------------


NORMAL_IDS = ["Doc-A", "b ", "1", "café"]
ORPHAN_IDS = ["doc-a", "café", "01", 1, None]


def _build_orphan_fixture(path: Path, *, duplicate: bool = False):
    """Normal sources and FTS-only rows. Returns (normal_rowids, orphan_rowids)."""
    store = _open(path)
    conn = store._conn
    normal, orphans = {}, []
    for i, sid in enumerate(NORMAL_IDS):
        text = f"alpha beta normal{i}" if i % 2 == 0 else f"alpha gamma normal{i}"
        _raw_source(conn, sid, text, pack="p" if i % 2 == 0 else "q")
        normal[sid] = _raw_fts(conn, sid, text)
    for i, sid in enumerate(ORPHAN_IDS):
        # Longer orphan bodies change the bm25 averages when they go.
        orphans.append(_raw_fts(conn, sid, "orphanword alpha delta " * (i + 3)))
    if duplicate:
        _raw_fts(conn, "Doc-A", "alpha beta normal0")
    conn.commit()
    store.close()
    return normal, orphans


def _join_complement(conn):
    return {r[0] for r in conn.execute(
        "SELECT f.rowid FROM doc_sources_fts AS f "
        "WHERE NOT EXISTS (SELECT 1 FROM doc_sources AS s WHERE s.source_id = f.source_id)"
    )}


def test_c3b_orphan_selection_matches_the_search_join_complement(tmp_path):
    from opencrab.stores import _source_fts_sqlite as sfts

    path = tmp_path / "doc_store.db"
    normal, orphans = _build_orphan_fixture(path)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        complement = _join_complement(conn)
        assert complement == set(orphans)
        before = _content(conn)
        conn.execute("BEGIN IMMEDIATE")
        receipt = sfts.prune_orphans(conn)
        conn.execute("COMMIT")
        after = _content(conn)
    finally:
        conn.close()
    deleted = {row[0] for row in before} - {row[0] for row in after}
    assert deleted == complement == set(orphans)
    assert receipt == {
        "deleted": len(orphans), "rowid_min": min(orphans), "rowid_max": max(orphans),
    }
    assert [row for row in after if row[0] in normal.values()] == [
        row for row in before if row[0] in normal.values()
    ]


# ---------------------------------------------------------------------------
# criteria 3c to 3f: the operator command
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def runner():
    return CliRunner()


def _legacy_orphan_db(data_dir: Path, *, duplicate: bool = False):
    path = data_dir / "doc_store.db"
    normal, orphans = _build_orphan_fixture(path, duplicate=duplicate)
    _drop_index(path)
    return path, normal, orphans


def test_c3c_dry_run_reports_and_changes_nothing(cli_env, runner):
    path, normal, orphans = _legacy_orphan_db(cli_env)
    sha, schema = _sha256(path), _schema(path)

    result = runner.invoke(main, ["packs", "source-fts-orphans"])

    assert result.exit_code == 0, result.output
    report = _parse_leading_json(result.output)
    assert report["dry_run"] is True
    assert report["layout_ok"] is True
    assert report["index_present"] is False
    assert report["orphan_rows"] == len(orphans)
    assert (report["orphan_rowid_min"], report["orphan_rowid_max"]) == (min(orphans), max(orphans))
    assert report["doc_sources"] == len(normal)
    assert report["fts_rows"] == len(normal) + len(orphans)
    assert report["sources_without_fts"] == 0
    assert report["duplicate_source_ids"] == 0
    assert _sha256(path) == sha
    assert _schema(path) == schema
    for leaked in ("orphanword", "normal0", "Doc-A", "caf"):
        assert leaked not in result.output


def test_c3d_apply_matches_the_reference_copy(cli_env, runner, tmp_path_factory):
    path, normal, orphans = _legacy_orphan_db(cli_env)
    reference_path = tmp_path_factory.mktemp("reference") / "doc_store.db"
    src, ref = sqlite3.connect(str(path)), sqlite3.connect(str(reference_path))
    src.backup(ref)
    src.close()
    ref.executemany("DELETE FROM doc_sources_fts WHERE rowid = ?", [(r,) for r in orphans])
    ref.commit()
    ref.close()
    before = sqlite3.connect(str(path))
    normal_before = [row for row in _content(before) if row[0] in normal.values()]
    before.close()

    result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])

    assert result.exit_code == 0, result.output
    summary = _parse_leading_json(result.output)
    assert summary["dry_run"] is False
    assert summary["deleted"]["deleted"] == len(orphans)
    after = summary["after"]
    assert after["orphan_rows"] == 0
    assert after["sources_without_fts"] == 0
    assert after["duplicate_source_ids"] == 0
    assert after["fts_rows"] == after["doc_sources"] == len(normal)
    assert after["index_present"] is True
    conn = sqlite3.connect(str(path))
    assert _content(conn) == normal_before
    conn.close()
    applied, reference = _open(path), _open(reference_path)
    try:
        assert _keyword_snapshot(applied) == _keyword_snapshot(reference)
    finally:
        applied.close()
        reference.close()


class TestApplyWriteLock:
    """3e: --apply holds write.lock from its first report to its last one;
    a dry run takes no lock. Same four checks as the #630 tests in
    tests/test_cli.py."""

    def test_apply_plans_after_the_holder_releases(self, cli_env, runner, monkeypatch):
        import opencrab.locking as locking
        from opencrab.locking import file_lock
        from opencrab.stores import _source_fts_sqlite as sfts

        path, normal, orphans = _legacy_orphan_db(cli_env)
        main_thread = threading.get_ident()
        order: list[str] = []
        moved = threading.Event()
        real_lock, real_report, real_prune = locking.write_lock, sfts.report, sfts.prune_orphans

        def lock_and_signal(*args, **kwargs):
            if threading.get_ident() == main_thread and not order:
                order.append("lock")
                moved.set()
            return real_lock(*args, **kwargs)

        def report(conn):
            order.append("report")
            moved.set()
            return real_report(conn)

        def prune(conn):
            order.append("prune")
            moved.set()
            return real_prune(conn)

        monkeypatch.setattr(locking, "write_lock", lock_and_signal)
        monkeypatch.setattr(sfts, "report", report)
        monkeypatch.setattr(sfts, "prune_orphans", prune)
        rescued_rowid = orphans[0]  # FTS row of "doc-a"
        holding = threading.Event()
        saw_move: list[bool] = []

        def holder() -> None:
            with file_lock("write.lock", str(cli_env), timeout=30):
                holding.set()
                saw_move.append(moved.wait(timeout=30))
                conn = sqlite3.connect(str(path))
                conn.execute("DELETE FROM doc_sources WHERE source_id = 'b '")  # new orphan
                _raw_source(conn, "doc-a", "rescued text")  # existing orphan gets a source
                conn.commit()
                conn.close()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert holding.wait(timeout=10)
        try:
            result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])
        finally:
            thread.join(timeout=30)

        assert saw_move == [True]
        assert order[:2] == ["lock", "report"], order
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(path))
        rowids = {row[0] for row in _content(conn)}
        conn.close()
        assert normal["b "] not in rowids
        assert rescued_rowid in rowids

    def test_apply_blocks_other_writers_after_open_and_during_delete(
        self, cli_env, runner, monkeypatch
    ):
        from opencrab.locking import file_lock
        from opencrab.stores import _source_fts_sqlite as sfts

        _legacy_orphan_db(cli_env)
        tries: list[str] = []
        real_report, real_prune = sfts.report, sfts.prune_orphans

        def contend() -> None:
            def attempt() -> None:
                try:
                    with file_lock("write.lock", str(cli_env), timeout=0):
                        tries.append("acquired")
                except TimeoutError:
                    tries.append("blocked")

            thread = threading.Thread(target=attempt, daemon=True)
            thread.start()
            thread.join(timeout=10)

        def report(conn):
            if not tries:  # the first report runs right after the store opened
                contend()
            return real_report(conn)

        def prune(conn):
            contend()
            return real_prune(conn)

        monkeypatch.setattr(sfts, "report", report)
        monkeypatch.setattr(sfts, "prune_orphans", prune)
        result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])

        assert result.exit_code == 0, result.output
        assert tries == ["blocked", "blocked"]

    def test_apply_times_out_with_an_operator_message(self, cli_env, runner, monkeypatch):
        from opencrab.config import get_settings
        from tests.test_store_bootstrap_lock_timeout_578 import _Holder

        path, _normal, _orphans = _legacy_orphan_db(cli_env)
        sha = _sha256(path)
        monkeypatch.setenv("WRITE_LOCK_TIMEOUT", "0.3")
        get_settings.cache_clear()

        with _Holder(str(cli_env)) as holder:
            result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])
            holder_alive = holder.alive()

        assert holder_alive
        assert result.exit_code == 1
        assert "timed out after" in " ".join(result.output.split())
        assert _sha256(path) == sha

    def test_dry_run_does_not_wait_for_write_lock(self, cli_env, runner, monkeypatch):
        from opencrab.config import get_settings
        from tests.test_store_bootstrap_lock_timeout_578 import _Holder

        _legacy_orphan_db(cli_env)
        free = runner.invoke(main, ["packs", "source-fts-orphans"])
        assert free.exit_code == 0, free.output
        monkeypatch.setenv("WRITE_LOCK_TIMEOUT", "0.3")
        get_settings.cache_clear()

        with _Holder(str(cli_env)) as holder:
            held = runner.invoke(main, ["packs", "source-fts-orphans"])
            holder_alive = holder.alive()

        assert holder_alive
        assert held.exit_code == 0, held.output
        assert _parse_leading_json(held.output) == _parse_leading_json(free.output)


def test_c3f_apply_fails_when_the_final_report_is_inconsistent(cli_env, runner):
    path, normal, orphans = _legacy_orphan_db(cli_env, duplicate=True)

    result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])

    assert result.exit_code == 3, result.output
    summary = _parse_leading_json(result.output)
    assert summary["deleted"]["deleted"] == len(orphans)
    assert summary["after"]["orphan_rows"] == 0
    assert summary["after"]["duplicate_source_ids"] == 1


def test_report_counts_sources_without_fts_and_duplicates(cli_env, runner):
    store = _open(cli_env / "doc_store.db")
    conn = store._conn
    _raw_source(conn, "src-a", "alpha beta")  # no FTS row
    _raw_source(conn, "src-b", "alpha gamma")
    _raw_fts(conn, "src-b", "alpha gamma")
    _raw_fts(conn, "src-b", "alpha gamma")  # duplicate FTS row
    conn.commit()
    store.close()

    result = runner.invoke(main, ["packs", "source-fts-orphans"])

    assert result.exit_code == 0, result.output
    report = _parse_leading_json(result.output)
    assert report["sources_without_fts"] == 1
    assert report["duplicate_source_ids"] == 1
    assert report["orphan_rows"] == 0


def test_apply_refuses_when_fts_is_unavailable(cli_env, runner):
    if not _FTS5_AVAILABLE:
        pytest.skip("SQLite FTS5 is not available in this build")
    path = cli_env / "doc_store.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE VIRTUAL TABLE doc_sources_fts USING fts5(text, source_id UNINDEXED)")
    conn.execute("INSERT INTO doc_sources_fts(text, source_id) VALUES ('alpha', 'x')")
    conn.commit()
    conn.close()

    result = runner.invoke(main, ["packs", "source-fts-orphans", "--apply"])

    assert result.exit_code == 3, result.output
    assert _parse_leading_json(result.output) == {
        "dry_run": False, "error": "the source FTS index is not available",
    }
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("SELECT text, source_id FROM doc_sources_fts").fetchall() == [
            ("alpha", "x")
        ]
        assert not _index_present(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
def test_refuses_a_non_local_doc_store(cli_env, runner, monkeypatch, mode):
    from opencrab.config import get_settings

    path, _normal, _orphans = _legacy_orphan_db(cli_env)
    sha = _sha256(path)
    monkeypatch.setenv("STORAGE_MODE", "pg")
    get_settings.cache_clear()
    args = ["packs", "source-fts-orphans"] + (["--apply"] if mode == "apply" else [])

    result = runner.invoke(main, args)

    assert result.exit_code == 1, result.output
    assert _sha256(path) == sha


# ---------------------------------------------------------------------------
# criterion 4: the source write and the FTS sync are one transaction
# ---------------------------------------------------------------------------


def _state_rows(store, source_id):
    conn = store._conn
    queries = (
        ("SELECT source_id, text, metadata FROM doc_sources WHERE source_id=?"),
        ("SELECT source_id, source_integrity FROM doc_source_integrity WHERE source_id=?"),
        ("SELECT id, c0, c1 FROM doc_sources_fts_content WHERE c0=?"),
    )
    return tuple([tuple(row) for row in conn.execute(sql, (source_id,))] for sql in queries)


@pytest.mark.parametrize("site", sorted(ACTIONS))
def test_c4_rowid_delete_failure_rolls_back_the_source_write(tmp_path, site):
    store = _open(tmp_path / "doc_store.db")
    try:
        _seed_sources(store)
        before = {sid: _state_rows(store, sid) for sid in ("s1", "s2", "s3")}
        real = store._conn

        class _FailRowidDelete:
            def execute(self, sql, params=()):
                if ROWID_DELETE_MARKER in sql:
                    raise RuntimeError("forced rowid delete failure")
                return real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(real, name)

        store._local.conn = _FailRowidDelete()
        try:
            with pytest.raises(RuntimeError, match="forced rowid delete failure"):
                ACTIONS[site](store)
        finally:
            store._local.conn = real
        assert {sid: _state_rows(store, sid) for sid in before} == before
    finally:
        store.close()


# ---------------------------------------------------------------------------
# contract D2: every FTS row of a source goes, also duplicates
# ---------------------------------------------------------------------------


def test_d2_duplicate_fts_rows_are_all_replaced_and_deleted(tmp_path):
    store = _open(tmp_path / "doc_store.db")
    try:
        _seed_sources(store)
        conn = store._conn
        _raw_fts(conn, "s1", "alpha beta")
        conn.commit()
        store.upsert_source("s1", "alpha beta again", {"pack_id": "p"})
        assert len(_state_rows(store, "s1")[2]) == 1
        _raw_fts(conn, "s2", "alpha gamma")
        conn.commit()
        _rewrite(store)
        assert len(_state_rows(store, "s2")[2]) == 1
        _raw_fts(conn, "s3", "beta delta")
        conn.commit()
        store.delete_sources_with_state(["s3"])
        assert _state_rows(store, "s3")[2] == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# D3: an FTS table with another layout turns the FTS capability off
# ---------------------------------------------------------------------------


def test_d3_other_fts_layout_disables_fts_without_index(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    if not _FTS5_AVAILABLE:
        pytest.skip("SQLite FTS5 is not available in this build")
    path = tmp_path / "doc_store.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE VIRTUAL TABLE doc_sources_fts USING fts5(text, source_id UNINDEXED)")
    conn.commit()
    conn.close()

    store = LocalSQLDocStore(str(path))
    try:
        assert store.available is True
        assert store._fts_ok is False
        assert not _index_present(store._conn)
    finally:
        store.close()
