"""scripts/migrate_pack_ownership.py (#146).

Sealed test environment per repo convention: LOCAL_DATA_DIR points at a
pytest tmp_path, settings cache is cleared before/after so no other test
module's env leaks in. The real live data directory
(~/.local/share/localcrab) is never opened -- every store here is built
against the scratch dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package (see tests/test_migrate_to_local.py's identical
# pattern) -- import it directly off sys.path instead.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_pack_ownership as migrate  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """LOCAL_DATA_DIR/STORAGE_MODE fixed to an isolated tmp dir; settings
    cache cleared before/after (same pattern as tests/test_cli.py's
    cli_env fixture)."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def bootstrapped_owner(env):
    """A local user bootstrapped in opencrab.db, returning its user_id."""
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    sql = make_sql_store(get_settings())
    user_id, _secret = bootstrap_local_user(sql)
    return user_id


def _seed_graph(local_data_dir: Path) -> None:
    """graph.db with one packed node/edge and two packless nodes."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(str(local_data_dir / "graph.db"))
    try:
        store.upsert_node("Dataset", "dataset:existing-pack", {"pack_id": "existing-pack", "title": "Existing"}, space_id="resource")
        store.upsert_node("Entity", "packless-1", {"name": "no pack here"}, space_id="concept")
        store.upsert_node("Entity", "packless-2", {"name": "also no pack"}, space_id="concept")
        store.upsert_edge("concept", "packless-1", "related_to", "concept", "packless-2", {})
    finally:
        store.close()


def _seed_doc(local_data_dir: Path) -> None:
    """doc_store.db with one packed doc node and one packless doc node/source."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    store = LocalSQLDocStore(str(local_data_dir / "doc_store.db"))
    try:
        store.upsert_node_doc("concept", "Entity", "packed-doc", {"pack_id": "existing-pack"})
        store.upsert_node_doc("concept", "Entity", "packless-doc", {"name": "no pack"})
        store.upsert_source("packless-source", "some text", {"title": "no pack metadata"})
    finally:
        store.close()


# ---------------------------------------------------------------------------
# dry-run: nothing written
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_touches_no_files(self, bootstrapped_owner, env):
        _seed_graph(env)
        _seed_doc(env)
        graph_path = env / "graph.db"
        doc_path = env / "doc_store.db"
        sql_path = env / "opencrab.db"
        before = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (graph_path, doc_path, sql_path)
            if p.exists()
        }

        rc = migrate.main([])

        after = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (graph_path, doc_path, sql_path)
            if p.exists()
        }
        assert rc == 0
        assert before == after

    def test_dry_run_reports_but_does_not_register(self, bootstrapped_owner, env):
        _seed_graph(env)
        _seed_doc(env)

        assert migrate.main([]) == 0

        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        assert get_pack(sql, "existing-pack") is None
        assert get_pack(sql, migrate.DEFAULT_PACK_ID) is None

    def test_no_bootstrap_user_errors(self, env):
        _seed_graph(env)
        with pytest.raises(SystemExit) as exc_info:
            migrate.main([])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_requires_backup_or_skip(self, bootstrapped_owner, env):
        _seed_graph(env)
        rc = migrate.main(["--apply"])
        assert rc == 2
        # Nothing written -- graph.db untouched.
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            node = store.get_node("Entity", "packless-1")
        finally:
            store.close()
        assert node.get("pack_id") is None

    def test_apply_backfills_graph_and_registers_packs(self, bootstrapped_owner, env):
        _seed_graph(env)
        _seed_doc(env)

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            assert graph.get_node("Entity", "packless-1")["pack_id"] == migrate.DEFAULT_PACK_ID
            assert graph.get_node("Entity", "packless-2")["pack_id"] == migrate.DEFAULT_PACK_ID
            # Pre-existing pack_id is untouched.
            assert graph.get_node("Dataset", "dataset:existing-pack")["pack_id"] == "existing-pack"
        finally:
            graph.close()

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            packless = docs.get_node_doc("concept", "packless-doc")
            assert packless["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID
            packed = docs.get_node_doc("concept", "packed-doc")
            assert packed["properties"]["pack_id"] == "existing-pack"
            source = docs.get_source("packless-source")
            assert source["metadata"]["pack_id"] == migrate.DEFAULT_PACK_ID
        finally:
            docs.close()

        sql = make_sql_store(get_settings())
        existing = get_pack(sql, "existing-pack")
        assert existing is not None
        assert existing["owner_id"] == bootstrapped_owner
        default = get_pack(sql, migrate.DEFAULT_PACK_ID)
        assert default is not None
        assert default["owner_id"] == bootstrapped_owner

    def test_acceptance_no_pack_id_missing_rows_after_apply(self, bootstrapped_owner, env):
        """Acceptance criterion: after --apply, graph/doc have 0 rows
        missing pack_id."""
        _seed_graph(env)
        _seed_doc(env)
        assert migrate.main(["--apply", "--skip-backup"]) == 0

        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            missing = migrate._graph_missing_node_ids(graph)
        finally:
            graph.close()
        assert missing == []

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            nodes_stats = migrate._backfill_sql_table(docs, "doc_nodes", "properties", "node_id", migrate.DEFAULT_PACK_ID, apply=False)
            sources_stats = migrate._backfill_sql_table(docs, "doc_sources", "metadata", "source_id", migrate.DEFAULT_PACK_ID, apply=False)
        finally:
            docs.close()
        assert nodes_stats["missing"] == 0
        assert sources_stats["missing"] == 0

    def test_acceptance_registry_row_count(self, bootstrapped_owner, env):
        """Acceptance criterion: the registry's pack_id set equals the
        graph's distinct pack_id set exactly (set equality, not a "+1 for
        the default pack" arithmetic formula -- the default pack_id is
        itself one of the graph's distinct pack_ids once backfilled rows
        carry it, so a separate "+1" would double-count it)."""
        _seed_graph(env)
        assert migrate.main(["--apply", "--skip-backup"]) == 0

        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            distinct_pack_ids = {r["pack_id"] for r in graph.list_packs(min_nodes=1)}
        finally:
            graph.close()

        sql = make_sql_store(get_settings())
        registered = migrate._registered_pack_ids(sql)
        # distinct_pack_ids already includes "default" (backfilled rows now
        # carry it) -- registry must have exactly that many rows, no more,
        # no fewer.
        assert migrate.DEFAULT_PACK_ID in distinct_pack_ids
        assert registered == distinct_pack_ids

    def test_apply_is_idempotent(self, bootstrapped_owner, env):
        _seed_graph(env)
        _seed_doc(env)
        assert migrate.main(["--apply", "--skip-backup"]) == 0
        # Second run must not error and must not double-register/duplicate.
        assert migrate.main(["--apply", "--skip-backup"]) == 0

        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        registered = migrate._registered_pack_ids(sql)
        assert len(registered) == len(set(registered))  # trivially true, but
        # the real assertion: exactly one "default" row exists.
        from sqlalchemy import text

        with sql._engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM packs WHERE pack_id = :pid"),
                {"pid": migrate.DEFAULT_PACK_ID},
            ).fetchone()[0]
        assert count == 1

    def test_apply_with_backup_to_copies_sqlite_files(self, bootstrapped_owner, env, tmp_path):
        _seed_graph(env)
        backup_dir = tmp_path / "backups"
        rc = migrate.main(["--apply", "--backup-to", str(backup_dir)])
        assert rc == 0
        assert (backup_dir / "opencrab.db").is_file()
        assert (backup_dir / "graph.db").is_file()

    def test_backup_refuses_to_overwrite_existing_target(self, bootstrapped_owner, env, tmp_path):
        _seed_graph(env)
        backup_dir = tmp_path / "backups"
        assert migrate.main(["--apply", "--backup-to", str(backup_dir)]) == 0
        with pytest.raises(SystemExit) as exc_info:
            migrate.main(["--apply", "--backup-to", str(backup_dir)])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _ensure_default_pack owner-mismatch guard (#146 M P1-3)
# ---------------------------------------------------------------------------


def _packs_snapshot(sql) -> list[tuple]:
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        return sorted(tuple(r) for r in conn.execute(text("SELECT pack_id, owner_id FROM packs")).fetchall())


def _graph_doc_snapshot(env):
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    graph = LocalGraphStore(str(env / "graph.db"))
    try:
        nodes = sorted(
            (n, graph.get_node("Entity", n).get("pack_id")) for n in ("packless-1", "packless-2")
        )
    finally:
        graph.close()
    docs = LocalSQLDocStore(str(env / "doc_store.db"))
    try:
        packless_doc = docs.get_node_doc("concept", "packless-doc")["properties"].get("pack_id")
    finally:
        docs.close()
    return nodes, packless_doc


class TestDefaultPackOwnerMismatch:
    """gate R1: a pre-existing ``default`` row owned by someone OTHER than
    the bootstrap owner aborts (rc 1) in BOTH dry-run and --apply, with the
    registry/graph/doc state provably unchanged before vs. after."""

    def test_dry_run_and_apply_both_abort_state_unchanged(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack
        from opencrab.stores.factory import make_sql_store

        _seed_graph(env)
        _seed_doc(env)
        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, migrate.DEFAULT_PACK_ID, "someone-else-owner", None, None, None)

        before_packs = _packs_snapshot(sql)
        before_data = _graph_doc_snapshot(env)

        rc_dry = migrate.main([])
        assert rc_dry == 1
        assert _packs_snapshot(sql) == before_packs
        assert _graph_doc_snapshot(env) == before_data

        rc_apply = migrate.main(["--apply", "--skip-backup"])
        assert rc_apply == 1
        assert _packs_snapshot(sql) == before_packs
        assert _graph_doc_snapshot(env) == before_data

    def test_dry_run_aborts_even_with_zero_unattributed_legacy_data(self, bootstrapped_owner, env):
        """clean-DB policy: no packless data at all still aborts -- the
        reserved-identity invariant matters going forward, not just for
        today's row count."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, migrate.DEFAULT_PACK_ID, "someone-else-owner", None, None, None)

        assert migrate.main([]) == 1
        assert migrate.main(["--apply", "--skip-backup"]) == 1

    def test_error_message_names_reserved_identity_and_resolution_steps(
        self, bootstrapped_owner, env, capsys
    ):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, migrate.DEFAULT_PACK_ID, "someone-else-owner", None, None, None)

        assert migrate.main(["--apply", "--skip-backup"]) == 1
        err = capsys.readouterr().err
        assert "reserved catch-all identity" in err
        assert "renaming/transferring" in err or "re-bootstrapping" in err


class TestDefaultPackOwnerMatchesBootstrap:
    """gate R2: a pre-existing ``default`` row owned by the SAME bootstrap
    owner is reused normally -- rc unaffected, and legacy data is actually
    attributed to it with that owner intact."""

    def test_apply_reuses_default_and_attributes_legacy_data(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack, get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        sql = make_sql_store(get_settings())
        assert _insert_pack(
            sql, migrate.DEFAULT_PACK_ID, bootstrapped_owner, "Pre-existing default", None, None
        )
        _seed_graph(env)
        _seed_doc(env)

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        default = get_pack(sql, migrate.DEFAULT_PACK_ID)
        assert default is not None
        assert default["owner_id"] == bootstrapped_owner

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            assert graph.get_node("Entity", "packless-1")["pack_id"] == migrate.DEFAULT_PACK_ID
        finally:
            graph.close()


# ---------------------------------------------------------------------------
# _register_graph_packs edge-only pack_id enumeration (#146 M P1-2)
# ---------------------------------------------------------------------------


class TestRegisterGraphPacksFromEdges:
    """gate R3: real LocalGraphStore + real graph_edges.properties (no
    mocked ``list_packs``) -- an edge whose endpoints carry no pack_id of
    their own, but whose OWN properties do, must still reach the registry."""

    def test_edge_only_pack_id_is_registered(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node("Entity", "e1", {"name": "no pack"}, space_id="concept")
            store.upsert_node("Entity", "e2", {"name": "no pack"}, space_id="concept")
            store.upsert_edge(
                "concept", "e1", "related_to", "concept", "e2", {"pack_id": "edge-only-pack"}
            )
        finally:
            store.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        sql = make_sql_store(get_settings())
        assert get_pack(sql, "edge-only-pack") is not None

    def test_malformed_edge_json_does_not_crash_enumeration(self, bootstrapped_owner, env):
        import sqlite3

        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node("Entity", "e1", {"name": "x"}, space_id="concept")
            store.upsert_node("Entity", "e2", {"name": "y"}, space_id="concept")
            store.upsert_edge(
                "concept", "e1", "related_to", "concept", "e2", {"pack_id": "good-pack"}
            )
        finally:
            store.close()
        with sqlite3.connect(str(env / "graph.db")) as conn:
            conn.execute(
                "INSERT INTO graph_edges (from_type, from_id, relation, to_type, to_id, properties) "
                "VALUES ('Entity', 'e1', 'broken_rel', 'Entity', 'e2', ?)",
                ("not-json{{{",),
            )

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        sql = make_sql_store(get_settings())
        assert get_pack(sql, "good-pack") is not None


# ---------------------------------------------------------------------------
# node-pack-map builders: dry-run prediction vs. post-apply ground truth
# (#146 M P1-1, gate R5)
# ---------------------------------------------------------------------------


class TestNodePackMapPredictionMatchesActual:
    def test_prediction_matches_post_apply_actual(self, bootstrapped_owner, env):
        from opencrab.ontology.pack_provenance import backfill_pack_ids
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node(
                "Entity", "n-inferred", {"source_path": "/data/packs/pack-a/x.md"}, space_id="concept"
            )
            store.upsert_node("Entity", "n-assumed", {"note": "nothing to infer"}, space_id="concept")
        finally:
            store.close()

        db_path = env / "graph.db"
        node_ids = ["n-inferred", "n-assumed"]

        predicted, pred_ambiguous = migrate._predict_node_pack_map(
            db_path, node_ids, migrate.DEFAULT_PACK_ID
        )
        assert predicted == {"n-inferred": "pack-a", "n-assumed": migrate.DEFAULT_PACK_ID}
        assert pred_ambiguous == {}

        backfill_pack_ids(db_path, assume_pack_id=migrate.DEFAULT_PACK_ID, dry_run=False)

        actual, act_ambiguous = migrate._read_actual_node_pack_ids(db_path, node_ids)
        assert actual == predicted
        assert act_ambiguous == {}

    def test_skipped_non_dict_row_excluded_from_both_maps(self, bootstrapped_owner, env):
        import json
        import sqlite3

        from opencrab.ontology.pack_provenance import backfill_pack_ids
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        store.close()  # tables created, no rows

        db_path = env / "graph.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO graph_nodes (node_type, node_id, space_id, properties) "
                "VALUES ('Entity', 'n-non-dict', 'concept', ?)",
                (json.dumps("just a string"),),
            )

        predicted, _ = migrate._predict_node_pack_map(db_path, ["n-non-dict"], migrate.DEFAULT_PACK_ID)
        assert predicted == {}

        backfill_pack_ids(db_path, assume_pack_id=migrate.DEFAULT_PACK_ID, dry_run=False)

        actual, _ = migrate._read_actual_node_pack_ids(db_path, ["n-non-dict"])
        assert actual == {}


class TestDuplicateNodeIdAmbiguity:
    """#146 M P1-1 review round 2 (M4 gates): the graph PK is
    (node_type, node_id), so one node_id may legally map to several rows.
    The vector store's identity is node_id ALONE -- rows resolving to
    DIFFERENT packs have no single correct vector value and must be
    excluded (ambiguous), never last-row-wins."""

    def _seed_dup(self, env, pack_b: str | None):
        """TypeA/dup with a path inferring pack-a; TypeB/dup either inferring
        ``pack_b`` or left bare (-> assumed default)."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node(
                "TypeA", "dup", {"source_path": "/packs/pack-a/doc.md"}, space_id="concept"
            )
            props_b = {"source_path": f"/packs/{pack_b}/doc.md"} if pack_b else {"note": "bare"}
            store.upsert_node("TypeB", "dup", props_b, space_id="concept")
        finally:
            store.close()
        return env / "graph.db"

    def test_conflicting_duplicate_is_ambiguous_not_last_row_wins(self, bootstrapped_owner, env):
        """M4-1 (codex repro): TypeA/dup -> pack-a, TypeB/dup -> assumed
        default. Conflict -> excluded from the map, reported ambiguous."""
        db_path = self._seed_dup(env, pack_b=None)
        resolved, ambiguous = migrate._predict_node_pack_map(
            db_path, ["dup"], migrate.DEFAULT_PACK_ID
        )
        assert resolved == {}
        assert ambiguous == {"dup": sorted({"pack-a", migrate.DEFAULT_PACK_ID})}

    def test_agreeing_duplicates_collapse_to_the_shared_value(self, bootstrapped_owner, env):
        """M4-2: both rows resolve to pack-a -> one map entry, no ambiguity,
        and _backfill_vector upserts exactly once for the deduped id."""
        db_path = self._seed_dup(env, pack_b="pack-a")
        resolved, ambiguous = migrate._predict_node_pack_map(
            db_path, ["dup", "dup"], migrate.DEFAULT_PACK_ID
        )
        assert resolved == {"dup": "pack-a"}
        assert ambiguous == {}

        class _SpyVec:
            available = True

            def __init__(self):
                self.upserts = []

            def get_by_id(self, node_id):
                return {"id": node_id, "document": "d", "metadata": {}}

            def upsert_texts(self, texts, metadatas=None, ids=None):
                self.upserts.append(list(ids or []))
                return list(ids or [])

        vec = _SpyVec()
        # deduped list -- main() dedupes via dict.fromkeys before this call
        migrate._backfill_vector(vec, ["dup"], resolved, apply=True)
        assert vec.upserts == [["dup"]]

    class _SpyVec:
        """available vector store double: records every upsert so the e2e
        gates can observe "0 upserts" / "exactly one deduped upsert" as
        real calls, not as an inference from the count line."""

        available = True

        def __init__(self):
            self.upserts = []

        def get_by_id(self, node_id):
            return {"id": node_id, "document": "d", "metadata": {}}

        def upsert_texts(self, texts, metadatas=None, ids=None):
            self.upserts.append((list(ids or []), [dict(m) for m in metadatas or []]))
            return list(ids or [])

    def test_ambiguous_node_gets_no_vector_upsert_and_is_counted(
        self, bootstrapped_owner, env, capsys, monkeypatch
    ):
        """M4-1 end-to-end through main(): ambiguous node -> vector upsert
        **observed 0 via a spy store** (not inferred), vector_ambiguous
        count + WARNING with node_id and both packs."""
        self._seed_dup(env, pack_b=None)
        _seed_doc(env)
        spy = self._SpyVec()
        monkeypatch.setattr("opencrab.stores.factory.make_vector_store", lambda settings: spy)
        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "vector_ambiguous=1" in out
        assert "'dup'" in out and "pack-a" in out
        assert "CONFLICTING pack_ids" in out
        assert spy.upserts == []

    def test_agreeing_duplicates_upsert_exactly_once_through_main(
        self, bootstrapped_owner, env, monkeypatch
    ):
        """M4-2 through main(): the graph naturally yields the duplicate
        node_id twice from _graph_missing_node_ids (two rows share it), and
        main's dict.fromkeys dedupe must collapse that to EXACTLY ONE vector
        upsert carrying the shared pack."""
        self._seed_dup(env, pack_b="pack-a")
        _seed_doc(env)
        spy = self._SpyVec()
        monkeypatch.setattr("opencrab.stores.factory.make_vector_store", lambda settings: spy)
        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0
        assert len(spy.upserts) == 1
        ids, metas = spy.upserts[0]
        assert ids == ["dup"]
        assert metas[0]["pack_id"] == "pack-a"

    def test_prediction_and_actual_agree_on_the_ambiguous_set(self, bootstrapped_owner, env):
        """M4-3: predicted ambiguous set == actual ambiguous set after a real
        backfill (values and keys)."""
        from opencrab.ontology.pack_provenance import backfill_pack_ids
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = self._seed_dup(env, pack_b=None)
        # 같은 시나리오에 비ambiguous 노드도 넣는다 -- ambiguous 집합만이
        # 아니라 resolved 값과 카운트도 predict==actual 이어야 한다 (M4-3).
        store = LocalGraphStore(str(db_path))
        try:
            store.upsert_node(
                "Entity", "plain", {"source_path": "/packs/pack-c/x.md"}, space_id="concept"
            )
        finally:
            store.close()
        node_ids = ["dup", "plain"]
        predicted, pred_amb = migrate._predict_node_pack_map(
            db_path, node_ids, migrate.DEFAULT_PACK_ID
        )
        backfill_pack_ids(db_path, assume_pack_id=migrate.DEFAULT_PACK_ID, dry_run=False)
        actual, act_amb = migrate._read_actual_node_pack_ids(db_path, node_ids)
        assert predicted == actual == {"plain": "pack-c"}
        assert len(predicted) == len(actual) == 1
        assert set(pred_amb) == set(act_amb) == {"dup"}
        assert pred_amb["dup"] == act_amb["dup"]


# ---------------------------------------------------------------------------
# Structured per-stage outcomes + exit codes (#146 M)
# ---------------------------------------------------------------------------


class TestStageOutcomesAndExitCodes:
    def test_exit_code_0_when_every_stage_is_clean_or_applied_vector_skip_excluded(
        self, bootstrapped_owner, env
    ):
        """The sandboxed test env has no sqlite_vec installed, so
        vector_backfill is ALWAYS "skipped" here -- this is exactly the
        real-world "optional dependency not installed" case, not a
        graph/doc SCOPE gap, so it must NOT gate the exit code to 3 (see
        main()'s ``gating`` comment)."""
        _seed_graph(env)
        _seed_doc(env)
        assert migrate.main(["--apply", "--skip-backup"]) == 0

    def test_exit_code_3_when_graph_backfill_is_out_of_scope(
        self, bootstrapped_owner, env, monkeypatch
    ):
        """graph_backfill/docs_backfill are the two stages whose SCOPE
        limits gate the exit code -- inject the "out of scope" outcome
        directly (same shape _backfill_graph already returns for a
        non-local storage_mode or missing graph.db) rather than standing up
        a real non-local backend."""
        _seed_doc(env)
        monkeypatch.setattr(migrate, "_backfill_graph", lambda *a, **kw: {"skipped": True})
        assert migrate.main(["--apply", "--skip-backup"]) == 3

    def test_exit_code_3_when_docs_backfill_is_out_of_scope(
        self, bootstrapped_owner, env, monkeypatch
    ):
        _seed_graph(env)
        monkeypatch.setattr(migrate, "_backfill_doc", lambda *a, **kw: {"skipped": True})
        assert migrate.main(["--apply", "--skip-backup"]) == 3

    def test_exit_code_3_when_wrapper_unavailable_but_graph_db_readable(
        self, bootstrapped_owner, env, monkeypatch, capsys
    ):
        """M-g1 (codex counterexample from the implementation review): a
        readable graph.db with an UNAVAILABLE graph *wrapper* backfills
        fine, but pack_id enumeration is skipped -- the graph's packs never
        reach the registry, and #147 deployed against that run would hide
        them all. registry_enumeration must gate the exit code in its own
        right (asserting all three stage outcomes pins registry_enumeration
        as the stage that fired, not a graph/docs skip). This must hold
        regardless of default-pack registration: registering the default
        pack is NOT the same thing as having enumerated the graph."""

        class _UnavailableGraph:
            available = False  # no _dialect attr -> _graph_missing_node_ids returns []

        _seed_graph(env)
        _seed_doc(env)
        monkeypatch.setattr(
            "opencrab.stores.factory.make_graph_store", lambda settings: _UnavailableGraph()
        )
        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "registry_enumeration: skipped" in out
        assert "graph_backfill: skipped" not in out
        assert "docs_backfill: skipped" not in out

    def test_exit_code_3_when_graph_rows_are_left_unattributed(
        self, bootstrapped_owner, env, capsys
    ):
        """M-g2: a graph_nodes row whose properties is valid JSON but not a
        dict (backfill_pack_ids counts it as nodes_skipped and cannot
        attribute it) leaves a pack_id-less row behind -- invariant 5.
        The stage must demote to skipped, surface the real count in the
        summary, and gate the exit code."""
        import json
        import sqlite3

        _seed_graph(env)
        _seed_doc(env)
        with sqlite3.connect(str(env / "graph.db")) as conn:
            conn.execute(
                "INSERT INTO graph_nodes (node_type, node_id, space_id, properties) "
                "VALUES ('Entity', 'non-dict-props', 'concept', ?)",
                (json.dumps("just a string"),),
            )
        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "graph_backfill: skipped" in out
        assert "nodes_skipped=1" in out
        assert "row(s) left unattributed" in out

    def test_exit_code_1_when_a_stage_raises_and_does_not_propagate(
        self, bootstrapped_owner, env, monkeypatch
    ):
        """#146 M: a stage failure (e.g. the concurrent-writer RuntimeError
        _register_graph_packs already raises on a rowcount mismatch) is
        caught inside main() and turned into exit code 1 -- it must not
        escape main() as an unhandled exception."""
        _seed_graph(env)

        def _boom(*a, **kw):
            raise RuntimeError("simulated concurrent-writer race")

        monkeypatch.setattr(migrate, "_register_graph_packs", _boom)
        rc = migrate.main(["--apply", "--skip-backup"])  # must not raise
        assert rc == 1

    def test_failed_stage_stops_later_stages_from_running(
        self, bootstrapped_owner, env, monkeypatch
    ):
        """A failure in an earlier stage must stop the pipeline -- writing
        docs/vector backfills after a failed graph/registry stage would
        leave an inconsistent partial migration."""
        _seed_graph(env)
        _seed_doc(env)
        doc_calls: list[int] = []
        monkeypatch.setattr(
            migrate, "_backfill_doc", lambda *a, **kw: doc_calls.append(1) or {"skipped": True}
        )
        monkeypatch.setattr(
            migrate,
            "_register_graph_packs",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert migrate.main(["--apply", "--skip-backup"]) == 1
        assert doc_calls == []


# ---------------------------------------------------------------------------
# _backfill_vector unit coverage (fake store, not a real sqlite-vec/pgvector
# dependency -- see module docstring for why this is best-effort)
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    def __init__(self, rows: dict[str, dict]):
        self.available = True
        self._rows = rows
        self.upserts: list[tuple[str, dict]] = []

    def get_by_id(self, node_id):
        row = self._rows.get(node_id)
        if row is None:
            return None
        return {"id": node_id, "document": row["document"], "metadata": row["metadata"]}

    def upsert_texts(self, texts, metadatas, ids):
        for node_id, meta in zip(ids, metadatas, strict=True):
            self._rows[node_id]["metadata"] = meta
            self.upserts.append((node_id, meta))
        return ids


class TestBackfillVector:
    def test_dry_run_reports_without_writing(self):
        store = _FakeVectorStore({"n1": {"document": "d1", "metadata": {}}})
        stats = migrate._backfill_vector(store, ["n1"], {"n1": "default"}, apply=False)
        assert stats == {"checked": 1, "missing": 1, "updated": 0}
        assert store.upserts == []

    def test_apply_backfills_missing_only(self):
        store = _FakeVectorStore(
            {
                "n1": {"document": "d1", "metadata": {}},
                "n2": {"document": "d2", "metadata": {"pack_id": "already-tagged"}},
            }
        )
        stats = migrate._backfill_vector(
            store, ["n1", "n2"], {"n1": "default", "n2": "default"}, apply=True
        )
        assert stats == {"checked": 2, "missing": 1, "updated": 1}
        assert store.upserts == [("n1", {"pack_id": "default"})]
        assert store._rows["n2"]["metadata"]["pack_id"] == "already-tagged"

    def test_unknown_node_id_is_skipped_not_an_error(self):
        store = _FakeVectorStore({})
        stats = migrate._backfill_vector(store, ["ghost"], {"ghost": "default"}, apply=True)
        assert stats == {"checked": 1, "missing": 0, "updated": 0}

    def test_store_without_get_by_id_is_skipped(self):
        class NoGetById:
            available = True

        stats = migrate._backfill_vector(NoGetById(), ["n1"], {"n1": "default"}, apply=True)
        assert stats == {"checked": 0, "missing": 0, "updated": 0}

    def test_unavailable_store_is_skipped(self):
        store = _FakeVectorStore({"n1": {"document": "d1", "metadata": {}}})
        store.available = False
        stats = migrate._backfill_vector(store, ["n1"], {"n1": "default"}, apply=True)
        assert stats == {"checked": 0, "missing": 0, "updated": 0}

    # -- #146 M P1-1: vector must follow node_pack_map, never a fixed default --

    def test_node_id_absent_from_map_gets_no_upsert(self):
        """A node_id graph itself could not attribute a pack_id to (absent
        from node_pack_map -- the "skipped" resolve_row_pack_id path) must
        not get vector-tagged with a guess: zero get_by_id/upsert_texts
        activity for it (gate R5 v3)."""
        store = _FakeVectorStore({"n-skipped": {"document": "d", "metadata": {}}})
        stats = migrate._backfill_vector(store, ["n-skipped"], {}, apply=True)
        assert stats == {"checked": 1, "missing": 0, "updated": 0}
        assert store.upserts == []

    def test_each_node_gets_its_own_mapped_pack_id_not_a_blanket_default(self):
        """gate R5 v3: existing/path-inferred/assumed each land in the
        vector row exactly as node_pack_map says -- never squashed to a
        single default_pack_id regardless of the graph's real resolution."""
        store = _FakeVectorStore(
            {
                "n-existing": {"document": "d1", "metadata": {}},
                "n-inferred": {"document": "d2", "metadata": {}},
                "n-assumed": {"document": "d3", "metadata": {}},
            }
        )
        node_pack_map = {
            "n-existing": "already-set-pack",
            "n-inferred": "pack-x",
            "n-assumed": "default",
        }
        stats = migrate._backfill_vector(
            store, ["n-existing", "n-inferred", "n-assumed"], node_pack_map, apply=True
        )
        assert stats == {"checked": 3, "missing": 3, "updated": 3}
        assert store._rows["n-existing"]["metadata"]["pack_id"] == "already-set-pack"
        assert store._rows["n-inferred"]["metadata"]["pack_id"] == "pack-x"
        assert store._rows["n-assumed"]["metadata"]["pack_id"] == "default"
