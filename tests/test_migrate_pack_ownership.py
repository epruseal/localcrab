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
            nodes_stats = migrate._backfill_doc_table(
                docs, "doc_nodes", "properties", ("space", "node_id"), "node_id",
                migrate.DEFAULT_PACK_ID, apply=False,
            )
            sources_stats = migrate._backfill_doc_table(
                docs, "doc_sources", "metadata", ("source_id",), "source_id",
                migrate.DEFAULT_PACK_ID, apply=False,
            )
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
# _register_graph_packs foreign-owner overlap guard (#177 review round 2,
# design "B v2" / gate W v3): a graph pack_id that already has REGISTRY
# content but is owned by someone other than the bootstrap owner must abort
# by default -- silently skipping it (the pre-fix behaviour) would hand
# whoever squatted that slug the legacy graph content it already carries.
# ---------------------------------------------------------------------------


class TestForeignOwnedPackOverlap:
    def _seed_foreign_overlap(self, env, sql):
        """Registry row for "foreign-pack" owned by someone else, PLUS
        graph content already tagged with that exact pack_id -- the
        "legacy content silently reassigned to a squatter" scenario."""
        from opencrab.pack.ownership import _insert_pack
        from opencrab.stores.local_graph_store import LocalGraphStore

        assert _insert_pack(sql, "foreign-pack", "someone-else-owner", None, None, None)
        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node(
                "Dataset",
                "dataset:foreign",
                {"pack_id": "foreign-pack", "title": "Foreign"},
                space_id="resource",
            )
        finally:
            store.close()

    def test_w1_dry_run_aborts_with_state_fully_unchanged(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        self._seed_foreign_overlap(env, sql)
        before_packs = _packs_snapshot(sql)
        graph_path = env / "graph.db"
        before_graph = (graph_path.stat().st_size, graph_path.stat().st_mtime_ns)

        rc = migrate.main([])

        assert rc == 1
        assert _packs_snapshot(sql) == before_packs
        assert (graph_path.stat().st_size, graph_path.stat().st_mtime_ns) == before_graph

    def test_w1_apply_aborts_with_default_and_backfill_already_run(
        self, bootstrapped_owner, env, capsys
    ):
        """W v3's explicit non-atomicity: stages BEFORE the foreign-owner
        check (default-pack registration, graph backfill) already ran --
        both are self-contained/harmless (bootstrap-owned) -- while
        enumeration's own registration and every stage after it did not.
        The summary must show which is which."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        self._seed_foreign_overlap(env, sql)

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 1

        default = get_pack(sql, migrate.DEFAULT_PACK_ID)
        assert default is not None
        assert default["owner_id"] == bootstrapped_owner  # stage 1 ran
        foreign = get_pack(sql, "foreign-pack")
        assert foreign["owner_id"] == "someone-else-owner"  # untouched by enumeration

        out = capsys.readouterr().out
        assert "graph_backfill: clean" in out  # stage 2 ran (nothing to backfill here)
        assert "registry_enumeration: failed" in out  # stage 3 aborted here
        assert "docs_backfill: failed" in out  # never reached
        assert "vector_backfill: failed" in out  # never reached

    def test_error_message_names_packs_owners_and_flag(self, bootstrapped_owner, env, capsys):
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        self._seed_foreign_overlap(env, sql)

        assert migrate.main(["--apply", "--skip-backup"]) == 1
        err = capsys.readouterr().err
        assert "foreign-pack" in err
        assert "someone-else-owner" in err
        assert "--accept-foreign-owned-packs" in err

    def test_w1b_flag_skips_and_proceeds(self, bootstrapped_owner, env, capsys):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        self._seed_foreign_overlap(env, sql)

        rc = migrate.main(["--apply", "--skip-backup", "--accept-foreign-owned-packs"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "skipping 1 graph pack_id" in out
        assert "foreign-pack" in out
        assert "someone-else-owner" in out
        foreign = get_pack(sql, "foreign-pack")
        assert foreign["owner_id"] == "someone-else-owner"  # left alone, not reassigned

    def test_w2_bootstrap_owned_rerun_is_unaffected(self, bootstrapped_owner, env):
        """A pack_id already registered to the BOOTSTRAP owner (the normal
        re-run case) is not foreign-owned -- no abort, unchanged from
        before this guard existed, and a second re-run stays idempotent."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, "existing-pack", bootstrapped_owner, None, None, None)
        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node(
                "Dataset",
                "dataset:existing",
                {"pack_id": "existing-pack", "title": "E"},
                space_id="resource",
            )
        finally:
            store.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0
        assert migrate.main(["--apply", "--skip-backup"]) == 0  # idempotent re-run

    def test_non_vacuous_old_candidate_formula_would_have_silently_skipped_it(
        self, bootstrapped_owner, env
    ):
        """비공허성: reproduces the PRE-fix candidate formula (``pid not in
        already`` with no owner check, exactly what ``_register_graph_packs``
        used before this guard) directly against the same seed -- proving
        "foreign-pack" really would have been silently excluded from
        ``candidates`` (0 unregistered -> stage reports "clean") instead of
        aborting. The new function instead raises for this exact scenario
        (see test_w1_dry_run_aborts_with_state_fully_unchanged)."""
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_graph_store, make_sql_store

        sql = make_sql_store(get_settings())
        self._seed_foreign_overlap(env, sql)
        graph = make_graph_store(get_settings())
        try:
            already = migrate._registered_pack_ids(sql)
            rows = graph.list_packs(min_nodes=1)
            node_meta = {r["pack_id"]: r for r in rows if r.get("pack_id")}
            old_candidates = [pid for pid in node_meta if pid not in already]
        finally:
            graph.close()
        assert "foreign-pack" in already
        assert "foreign-pack" in node_meta
        assert old_candidates == []


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
# dry-run enumeration sees path-inferred/ambiguous pack_ids (#177 review
# round 2, design "C v2" / gate X): _predict_node_pack_map already computes
# what backfill_pack_ids WOULD write, but a dry-run's graph.list_packs()
# call only sees pack_ids already ON DISK -- a node whose pack_id would be
# path-inferred is invisible to enumeration until this union is applied.
# ---------------------------------------------------------------------------


class TestDryRunEnumerationSeesPredictedPacks:
    def _seed(self, env):
        """n-solo infers "pack-solo" cleanly; "dup" is one node_id shared
        by two rows (legal PK: (node_type, node_id)) that infer DIFFERENT
        packs -- both "pack-a" and "pack-b" must still reach enumeration
        even though the node_id itself is excluded as ambiguous."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(env / "graph.db"))
        try:
            store.upsert_node(
                "Entity", "n-solo", {"source_path": "/packs/pack-solo/x.md"}, space_id="concept"
            )
            store.upsert_node(
                "TypeA", "dup", {"source_path": "/packs/pack-a/x.md"}, space_id="concept"
            )
            store.upsert_node(
                "TypeB", "dup", {"source_path": "/packs/pack-b/x.md"}, space_id="concept"
            )
        finally:
            store.close()

    def test_x1_predicted_and_ambiguous_packs_counted_unregistered(
        self, bootstrapped_owner, env
    ):
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_graph_store, make_sql_store

        self._seed(env)
        db_path = env / "graph.db"
        predicted, ambiguous = migrate._predict_node_pack_map(
            db_path, ["n-solo", "dup"], migrate.DEFAULT_PACK_ID
        )
        assert predicted == {"n-solo": "pack-solo"}
        assert ambiguous == {"dup": sorted(["pack-a", "pack-b"])}

        sql = make_sql_store(get_settings())
        graph = make_graph_store(get_settings())
        try:
            stats = migrate._register_graph_packs(
                sql,
                graph,
                bootstrapped_owner,
                apply=False,
                node_pack_map=predicted,
                ambiguous_nodes=ambiguous,
            )
        finally:
            graph.close()
        # pack-solo + pack-a + pack-b -- none of them written to
        # graph_nodes yet (dry-run backfill never runs), so
        # graph.list_packs() alone would report 0 here.
        assert stats["graph_distinct_packs"] == 3
        assert stats["unregistered"] == 3

    def test_non_vacuous_undercounts_without_the_union(self, bootstrapped_owner, env):
        """비공허성: the SAME seed with node_pack_map/ambiguous_nodes
        omitted (the pre-C-v2 call shape) undercounts to 0 -- proving the
        dry-run report really was blind to path-inferred/ambiguous pack_ids
        before this fix."""
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_graph_store, make_sql_store

        self._seed(env)
        sql = make_sql_store(get_settings())
        graph = make_graph_store(get_settings())
        try:
            stats = migrate._register_graph_packs(sql, graph, bootstrapped_owner, apply=False)
        finally:
            graph.close()
        assert stats["unregistered"] == 0

    def test_x2_dry_run_report_matches_apply_result(self, bootstrapped_owner, env, capsys):
        """set + count + outcome-label triple-check between dry-run and a
        real --apply over the identical seed."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store

        self._seed(env)

        rc_dry = migrate.main([])
        out_dry = capsys.readouterr().out
        assert rc_dry == 0
        assert "3 not yet in the registry" in out_dry
        # R5-B (PR #177 review round 5): registry_enumeration's reason now
        # folds in step 3.5's doc-derived count too (0 here -- no doc
        # content seeded by this test). The first number counts the default
        # pack alongside the graph-derived ones, which is why it is labelled
        # "default+graph-derived" rather than "graph-derived".
        assert (
            "registry_enumeration: applied (4 row(s) needed registering "
            "(default+graph-derived=4, doc-derived=0))" in out_dry
        )
        assert "edge-inferred pack_id" in out_dry  # apply-only-limitation note

        rc_apply = migrate.main(["--apply", "--skip-backup"])
        out_apply = capsys.readouterr().out
        assert rc_apply == 0
        assert "3 not yet in the registry" in out_apply
        assert (
            "registry_enumeration: applied (4 row(s) needed registering "
            "(default+graph-derived=4, doc-derived=0))" in out_apply
        )

        sql = make_sql_store(get_settings())
        for pid in ("pack-solo", "pack-a", "pack-b", migrate.DEFAULT_PACK_ID):
            assert get_pack(sql, pid) is not None


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


# ---------------------------------------------------------------------------
# #146 P1(b) (PR #177 review round 3): doc backfill dropping inferred
# pack_id -- design v5's 15-item reproduction list.
# ---------------------------------------------------------------------------


class TestGraphTwinDocBackfill:
    """gates 1/2/3/5/6/7/9/10/12: the priority graph-twin(exact) ->
    graph-twin(fallback) -> self path-inference -> default, exercised
    through real LocalGraphStore/LocalSQLDocStore files and ``main()``."""

    def test_1_both_graph_and_doc_twin_missing_source_path_gives_pack_not_default(
        self, bootstrapped_owner, env
    ):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node(
                "Entity", "n1", {"source_path": "/packs/pack-a/x.md"}, space_id="concept"
            )
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "n1", {})
        finally:
            docs.close()

        # dry-run prediction, BEFORE any write.
        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            exact, fallback, ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["n1"], migrate.DEFAULT_PACK_ID, actual=False
            )
        finally:
            graph.close()
        assert exact == {("concept", "n1"): "pack-a"}
        # fallback_map carries ONLY blank-space_id graph rows (see test_5).
        # This row has a real space_id, so the exact key can always match a
        # doc row's space and the node_id-only fallback must stay empty --
        # otherwise a doc row in a DIFFERENT space would inherit this pack
        # just for sharing a node_id (see test_5b).
        assert fallback == {}
        assert ambiguous == {}
        assert fallback_ambiguous == set()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "n1")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-a"

    def test_2_graph_already_packed_doc_twin_missing_gets_the_same_pack(
        self, bootstrapped_owner, env
    ):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "n2", {"pack_id": "pack-b"}, space_id="concept")
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "n2", {})
        finally:
            docs.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "n2")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-b"

    def test_3_codex_counterexample_same_node_id_different_space_resolves_exact(
        self, bootstrapped_owner, env
    ):
        """v2 결함 5's exact-key scenario: TypeA/shared in space "concept"
        (pack-a) and TypeB/shared in space "evidence" (pack-b) do NOT
        collide -- a doc row keyed (concept, shared) resolves cleanly to
        pack-a, it is not excluded as ambiguous."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("TypeA", "shared", {"pack_id": "pack-a"}, space_id="concept")
            graph.upsert_node("TypeB", "shared", {"pack_id": "pack-b"}, space_id="evidence")
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "TypeA", "shared", {})
        finally:
            docs.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            exact, _fallback, ambiguous, _fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["shared"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()
        assert exact[("concept", "shared")] == "pack-a"
        assert ("concept", "shared") not in ambiguous

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "shared")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-a"

    def test_4_true_ambiguous_twin_excluded_docs_skipped_rc3(
        self, bootstrapped_owner, env, capsys
    ):
        """same (space, node_id) resolving to two DIFFERENT pack_ids across
        graph rows is genuinely ambiguous -- excluded, reported, and demotes
        docs_backfill to skipped (rc 3)."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("TypeA", "dup", {"pack_id": "pack-a"}, space_id="concept")
            graph.upsert_node("TypeB", "dup", {"pack_id": "pack-b"}, space_id="concept")
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "TypeA", "dup", {})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "docs_backfill: skipped" in out
        assert "left unattributed" in out

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "dup")
        finally:
            docs.close()
        assert not row["properties"].get("pack_id")

    def test_5b_nonblank_cross_space_graph_row_is_not_a_fallback_twin(
        self, bootstrapped_owner, env
    ):
        """A graph node in space X must NOT attribute a doc row in space Y.

        `doc_nodes`' PK is `(space, node_id)`, so `(concept, same)` and
        `(evidence, same)` are DIFFERENT rows, not twins. An earlier build
        put every matched graph row into the node_id-only fallback, so the
        concept doc row inherited the evidence node's pack -- the exact
        wrong-pack/wrong-visibility outcome P1(b) exists to prevent. Only
        blank-space_id rows may feed the fallback.
        """
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node(
                "Evidence", "same", {"pack_id": "pack-a"}, space_id="evidence"
            )
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            # No graph twin at (concept, same), and nothing inferable from
            # the doc row's own properties.
            docs.upsert_node_doc("concept", "Entity", "same", {"note": "unrelated"})
        finally:
            docs.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            exact, fallback, _ambiguous, _fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["same"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()
        assert exact == {("evidence", "same"): "pack-a"}
        assert fallback == {}

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "same")
        finally:
            docs.close()
        # Falls through to the catch-all, NOT to the unrelated space's pack.
        assert row["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID

    def test_5_blank_graph_space_id_uses_fallback_map(self, bootstrapped_owner, env):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "legacy1", {"pack_id": "pack-legacy"}, space_id=None)
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "legacy1", {})
        finally:
            docs.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            exact, fallback, _ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["legacy1"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()
        # exact is keyed (None, "legacy1") -- never matches a real doc space,
        # so only the node_id-only fallback closes the gap.
        assert exact == {(None, "legacy1"): "pack-legacy"}
        assert fallback == {"legacy1": "pack-legacy"}
        assert fallback_ambiguous == set()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "legacy1")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-legacy"

    def test_6_doc_self_path_inference_without_graph_twin(self, bootstrapped_owner, env):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "solo-doc", {"source_path": "/packs/pack-c/x.md"}
            )
        finally:
            docs.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "solo-doc")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-c"

    def test_7_no_evidence_at_all_gets_default(self, bootstrapped_owner, env):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "no-hint", {"note": "nothing to infer"})
        finally:
            docs.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "no-hint")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID

    def test_8_pk_predicate_leaves_non_dict_row_untouched_when_sharing_node_id(
        self, bootstrapped_owner, env
    ):
        """v2 결함 6 反례 in the P1(b) context: ONE node_id ("shared-id")
        shared by a resolvable row (space=concept) and a non-dict row
        (space=evidence). A ``node_id IN (...)`` predicate would touch
        both; the real PK predicate must only touch the resolvable one."""
        import json
        import sqlite3

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "shared-id", {"source_path": "/packs/pack-z/x.md"}
            )
        finally:
            docs.close()
        with sqlite3.connect(str(env / "doc_store.db")) as conn:
            conn.execute(
                "INSERT INTO doc_nodes (space, node_id, node_type, properties, updated_at) "
                "VALUES ('evidence', 'shared-id', 'Entity', ?, datetime('now'))",
                (json.dumps("just a string"),),
            )

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 3  # the non-dict row remains unattributed -- correct

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            resolvable = docs.get_node_doc("concept", "shared-id")
        finally:
            docs.close()
        assert resolvable["properties"]["pack_id"] == "pack-z"

        with sqlite3.connect(str(env / "doc_store.db")) as conn:
            raw = conn.execute(
                "SELECT properties FROM doc_nodes WHERE space='evidence' AND node_id='shared-id'"
            ).fetchone()[0]
        assert raw == json.dumps("just a string")  # untouched, not overwritten

    def test_9_doc_sources_self_inference_and_default(self, bootstrapped_owner, env):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_source("src-inferred", "text", {"source_path": "/packs/pack-s/doc.md"})
            docs.upsert_source("src-bare", "text", {"title": "nothing to infer"})
        finally:
            docs.close()

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            inferred = docs.get_source("src-inferred")
            bare = docs.get_source("src-bare")
        finally:
            docs.close()
        assert inferred["metadata"]["pack_id"] == "pack-s"
        assert bare["metadata"]["pack_id"] == migrate.DEFAULT_PACK_ID

    def test_10_dry_run_by_pack_distribution_matches_apply_actual(
        self, bootstrapped_owner, env
    ):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "twin-a", {"pack_id": "pack-a"}, space_id="concept")
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "twin-a", {})
            docs.upsert_node_doc(
                "concept", "Entity", "self-b", {"source_path": "/packs/pack-b/x.md"}
            )
            docs.upsert_node_doc("concept", "Entity", "none-c", {"note": "nada"})
        finally:
            docs.close()

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            missing = migrate._doc_missing_node_ids(docs)
            exact, fallback, ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, missing, migrate.DEFAULT_PACK_ID, actual=False
            )
            dry = migrate._backfill_doc_table(
                docs,
                "doc_nodes",
                "properties",
                ("space", "node_id"),
                "node_id",
                migrate.DEFAULT_PACK_ID,
                apply=False,
                twin_exact=exact,
                twin_fallback=fallback,
                twin_ambiguous=ambiguous,
                twin_fallback_ambiguous=fallback_ambiguous,
            )
        finally:
            docs.close()
            graph.close()
        assert dry["by_pack"] == {"pack-a": 1, "pack-b": 1, migrate.DEFAULT_PACK_ID: 1}

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            actual_dist: dict[str, int] = {}
            for nid in ("twin-a", "self-b", "none-c"):
                pid = docs.get_node_doc("concept", nid)["properties"]["pack_id"]
                actual_dist[pid] = actual_dist.get(pid, 0) + 1
        finally:
            docs.close()
        assert actual_dist == dry["by_pack"]

    def test_12_normal_run_with_doc_twin_backfill_still_exits_0(
        self, bootstrapped_owner, env
    ):
        """regression: the P1(b) rewrite must not break the ordinary
        clean-completion exit code."""
        _seed_graph(env)
        _seed_doc(env)
        assert migrate.main(["--apply", "--skip-backup"]) == 0


class TestGraphTwinFallbackAmbiguity:
    """PR #177 review round 4 R4-B: several blank-space_id graph rows
    sharing ONE node_id but resolving to DIFFERENT pack_ids must not have
    that disagreement silently disappear through the fallback_map dict
    comprehension -- the ambiguity has to reach ``_backfill_doc_table`` via
    the dedicated ``fallback_ambiguous`` set so the affected doc row gets
    EXCLUDED, not silently defaulted or self-inferred."""

    def test_r4b_4_disagreeing_blank_space_rows_populate_fallback_ambiguous(
        self, bootstrapped_owner, env
    ):
        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("TypeA", "dup-blank", {"pack_id": "pack-x"}, space_id=None)
            graph.upsert_node("TypeB", "dup-blank", {"pack_id": "pack-y"}, space_id=None)
        finally:
            graph.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            _exact, fallback, _ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["dup-blank"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()

        assert "dup-blank" in fallback_ambiguous
        assert "dup-blank" not in fallback  # dropped from fallback_map, not averaged/guessed

    def test_r4b_5_exact_miss_and_fallback_ambiguous_excludes_not_default_not_self_inferred(
        self, bootstrapped_owner, env, capsys
    ):
        """The core R4-B repro: (blank_space, node_id) ambiguity must not
        leak past the (document_space, node_id) lookup key mismatch into
        self-inference or default. A resolvable self-inference path is
        deliberately present on the doc row's own properties to prove the
        row is EXCLUDED outright, not merely "happened to land on the
        right answer via self-inference" -- if fallback_ambiguous were not
        consulted, this doc row would fall through past the exact miss
        straight to that self-inferred pack (or, absent even that, to
        ``default``)."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("TypeA", "same", {"pack_id": "pack-x"}, space_id=None)
            graph.upsert_node("TypeB", "same", {"pack_id": "pack-y"}, space_id=None)
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "same", {"source_path": "/packs/pack-self/x.md"}
            )
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "docs_backfill: skipped" in out
        assert "left unattributed" in out

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "same")
        finally:
            docs.close()
        pack_id = row["properties"].get("pack_id")
        assert not pack_id  # neither default...
        assert pack_id != migrate.DEFAULT_PACK_ID
        assert pack_id != "pack-self"  # ...nor self-inferred

    def test_r4b_6_agreeing_blank_space_rows_still_apply_fallback(
        self, bootstrapped_owner, env
    ):
        """Not over-exclusion: TWO blank-space_id rows that AGREE on the
        same pack_id still land in fallback_map (not fallback_ambiguous)
        and the doc row is still backfilled -- same discipline as the
        single-row case (test_5_blank_graph_space_id_uses_fallback_map)."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("TypeA", "agree", {"pack_id": "pack-legacy"}, space_id=None)
            graph.upsert_node("TypeB", "agree", {"pack_id": "pack-legacy"}, space_id=None)
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "agree", {})
        finally:
            docs.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            _exact, fallback, _ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["agree"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()
        assert fallback == {"agree": "pack-legacy"}
        assert "agree" not in fallback_ambiguous

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "agree")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-legacy"

    def test_r4b_6b_exact_hit_passes_through_despite_fallback_ambiguous(
        self, bootstrapped_owner, env
    ):
        """Exact takes priority over fallback ambiguity: a doc row whose
        graph twin has an EXACT (space, node_id) match must apply that
        value even when OTHER, unrelated blank-space_id rows sharing the
        same bare node_id disagree with each other -- the more specific
        exact row is correct regardless of what the space-less rows think."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node(
                "Entity", "shared-id", {"pack_id": "pack-exact"}, space_id="concept"
            )
            graph.upsert_node("TypeA", "shared-id", {"pack_id": "pack-x"}, space_id=None)
            graph.upsert_node("TypeB", "shared-id", {"pack_id": "pack-y"}, space_id=None)
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "shared-id", {})
        finally:
            docs.close()

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            exact, _fallback, _ambiguous, fallback_ambiguous = migrate._graph_twin_pack_map(
                graph, ["shared-id"], migrate.DEFAULT_PACK_ID, actual=True
            )
        finally:
            graph.close()
        assert exact[("concept", "shared-id")] == "pack-exact"
        assert "shared-id" in fallback_ambiguous

        assert migrate.main(["--apply", "--skip-backup"]) == 0

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "shared-id")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-exact"


class _FakeSingleRowDocStore:
    """Minimal doc-store double with exactly ONE row -- only supports the
    query shapes ``_backfill_doc_table`` issues (unconditional total COUNT,
    the missing-rows SELECT, the per-chunk expected-count recheck, and the
    grouped UPDATE), used to probe the PG-JSONB-dict-properties path (v3
    결함 7) without standing up a real PostgreSQL dependency."""

    def __init__(self, space: str, node_id: str, properties):
        self._space = space
        self._node_id = node_id
        self._properties = properties
        self.written_pack_id: str | None = None

    def _table(self, name: str) -> str:
        return name

    def _fetch_one(self, sql, params):
        return (1,)  # single-row fixture: total COUNT and the per-chunk
        # expected-count recheck both always match exactly 1.

    def _fetch_all(self, sql, params):
        return [(self._space, self._node_id, self._properties)]

    def _exec_write(self, sql, params):
        self.written_pack_id = params["pid"]
        return 1


class TestPgDictPropertiesAndTwinDictStrEquivalence:
    """gates 14/15 (v3 결함 7 / v5 결함 8): PostgreSQL's psycopg2 hands back
    an already-decoded ``dict`` for a JSONB column, not a JSON string --
    both the doc row's OWN self-inference (14) and the graph-twin lookup's
    apply-time ground-truth read (15) must treat that dict identically to
    the JSON string SQLite always stores."""

    def test_14_pg_style_dict_properties_get_inferred_not_default(self):
        store = _FakeSingleRowDocStore("concept", "pg-node", {"source_path": "/packs/pack-p/x.md"})

        stats = migrate._backfill_doc_table(
            store, "doc_nodes", "properties", ("space", "node_id"), "node_id",
            migrate.DEFAULT_PACK_ID, apply=True,
        )

        assert stats["by_pack"] == {"pack-p": 1}
        assert stats["excluded"] == 0
        assert store.written_pack_id == "pack-p"

    class _FakeGraphTwinStore:
        def __init__(self, node_id: str, space_id: str, pack_id: str, *, as_dict: bool):
            import json

            self._node_id = node_id
            self._space_id = space_id
            props = {"pack_id": pack_id}
            self._properties = props if as_dict else json.dumps(props)

        def _table(self, name: str) -> str:
            return name

        def _fetch_all(self, sql, params):
            return [(self._node_id, self._space_id, self._properties)]

    def test_15_twin_map_actual_branch_dict_and_str_properties_agree(self):
        str_store = self._FakeGraphTwinStore("n1", "concept", "pack-a", as_dict=False)
        dict_store = self._FakeGraphTwinStore("n1", "concept", "pack-a", as_dict=True)

        exact_str, fb_str, amb_str, famb_str = migrate._graph_twin_pack_map(
            str_store, ["n1"], migrate.DEFAULT_PACK_ID, actual=True
        )
        exact_dict, fb_dict, amb_dict, famb_dict = migrate._graph_twin_pack_map(
            dict_store, ["n1"], migrate.DEFAULT_PACK_ID, actual=True
        )
        assert exact_str == exact_dict == {("concept", "n1"): "pack-a"}
        # Nonblank space_id -> exact key always matchable -> no fallback.
        assert fb_str == fb_dict == {}
        assert amb_str == amb_dict == {}
        assert famb_str == famb_dict == set()

        # The fallback branch itself must agree across str/dict too, so run
        # the blank-space_id variant through both representations as well.
        blank_str = self._FakeGraphTwinStore("n1", "", "pack-a", as_dict=False)
        blank_dict = self._FakeGraphTwinStore("n1", "", "pack-a", as_dict=True)
        _, blank_fb_str, _, _ = migrate._graph_twin_pack_map(
            blank_str, ["n1"], migrate.DEFAULT_PACK_ID, actual=True
        )
        _, blank_fb_dict, _, _ = migrate._graph_twin_pack_map(
            blank_dict, ["n1"], migrate.DEFAULT_PACK_ID, actual=True
        )
        assert blank_fb_str == blank_fb_dict == {"n1": "pack-a"}

        for exact, fallback, ambiguous, fallback_ambiguous in (
            (exact_str, fb_str, amb_str, famb_str),
            (exact_dict, fb_dict, amb_dict, famb_dict),
        ):
            doc_store = _FakeSingleRowDocStore("concept", "n1", {})
            stats = migrate._backfill_doc_table(
                doc_store, "doc_nodes", "properties", ("space", "node_id"), "node_id",
                migrate.DEFAULT_PACK_ID, apply=True,
                twin_exact=exact, twin_fallback=fallback, twin_ambiguous=ambiguous,
                twin_fallback_ambiguous=fallback_ambiguous,
            )
            assert stats["by_pack"] == {"pack-a": 1}
            assert doc_store.written_pack_id == "pack-a"


class TestBackfillMongoPathInference:
    """gate 11: Mongo (docker mode) applies the same existing -> inferred ->
    assumed priority to its own documents' properties/metadata instead of
    an unconditional blanket default."""

    class _FakeMongoUpdateResult:
        def __init__(self, modified_count: int):
            self.modified_count = modified_count

    class _FakeMongoCollection:
        def __init__(self, docs: list[dict]):
            self._docs = {d["_id"]: dict(d) for d in docs}

        def estimated_document_count(self) -> int:
            return len(self._docs)

        def find(self, query=None):
            # ``query=None`` (#177 review round 6 P1): a FULL-COLLECTION
            # scan, as issued by ``_mongo_existing_pack_ids`` -- it no
            # longer filters via ``$exists``/``$ne`` (see that function's
            # docstring for why: those operators match ARRAY ELEMENTS in
            # real MongoDB, which silently mis-classified a document like
            # ``{"pack_id": [""]}``). This double is a records store, not a
            # query engine, so it does not attempt to interpret every
            # possible Mongo operator -- it only understands the exact
            # shapes this script's own functions issue.
            if query is None:
                return [dict(doc) for doc in self._docs.values()]
            field_root = next(iter(query["$or"][0])).split(".")[0]
            results = []
            for doc in self._docs.values():
                # Real MongoDB dotted-path traversal into a non-document
                # field (missing key, None, a list, a bare string, ...)
                # never finds "<field>.pack_id" either -- it just doesn't
                # exist, same as an absent key -- so $exists:False matches
                # it too. A non-dict field must NOT be treated as "has a
                # pack_id" by calling .get() on it (#146 P1(b), PR #177
                # review round 4 R4-C -- the old ``doc.get(field_root) or {}``
                # here crashed on a non-empty list/string with AttributeError
                # since a list/str has no ``.get``).
                field = doc.get(field_root, None)
                if not isinstance(field, dict) or self._pack_id_missing(field):
                    results.append(dict(doc))
            return results

        @staticmethod
        def _pack_id_missing(field: dict) -> bool:
            """Whether ``field["pack_id"]`` satisfies missing_q's ``$or``
            (absent / ``None`` / ``""``) -- INCLUDING real MongoDB's dotted-
            equality-matches-array-elements quirk (#177 review round 6 P1):
            ``{"pack_id": [""]}`` or ``{"pack_id": [None]}`` also counts as
            "missing" here, even though the value itself is a truthy list,
            because a dotted ``{"field.pack_id": ""}``/``None`` query in
            real MongoDB matches when ANY array element equals the target.
            This is exactly what pulls such a document into
            ``_backfill_mongo``'s ``missing_docs`` despite its pack_id being
            malformed rather than genuinely missing -- the pre-resolver
            ``_classify_pack_id`` check inside ``_backfill_mongo`` is what
            then correctly excludes it instead of fabricating a pack_id."""
            if "pack_id" not in field:
                return True
            value = field["pack_id"]
            if not value:  # None / "" / [] / 0 / {} -- matches the old falsy rule
                return True
            return isinstance(value, list) and (None in value or "" in value)

        def update_many(self, filt, update):
            ids = set(filt["_id"]["$in"])
            set_ops = update["$set"]
            count = 0
            for _id, doc in self._docs.items():
                if _id not in ids:
                    continue
                for path, value in set_ops.items():
                    root, key = path.split(".", 1)
                    doc.setdefault(root, {})[key] = value
                count += 1
            return TestBackfillMongoPathInference._FakeMongoUpdateResult(count)

    class _FakeMongoDb:
        def __init__(self, nodes: list[dict], sources: list[dict]):
            self._colls = {
                "nodes": TestBackfillMongoPathInference._FakeMongoCollection(nodes),
                "sources": TestBackfillMongoPathInference._FakeMongoCollection(sources),
            }

        def __getitem__(self, name):
            return self._colls[name]

    def test_11_mongo_uses_path_inference_not_blanket_default(self):
        nodes = [
            {"_id": 1, "node_id": "m1", "properties": {"source_path": "/packs/pack-m/x.md"}},
            {"_id": 2, "node_id": "m2", "properties": {"note": "nothing to infer"}},
        ]
        db = self._FakeMongoDb(nodes, sources=[])

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        assert db._colls["nodes"]._docs[1]["properties"]["pack_id"] == "pack-m"
        assert db._colls["nodes"]._docs[2]["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID
        assert results["nodes"]["missing"] == 2
        assert results["nodes"]["updated"] == 2

    def test_r4c_7_native_non_dict_properties_excluded_not_defaulted(self):
        """PR #177 review round 4 R4-C: Mongo fields are NATIVE BSON, not
        JSON strings -- ``resolve_row_pack_id``'s own non-dict detection
        (built for a JSON-string column) never fires for these; the OLDER
        design (trusting its ``reason`` string) would have silently
        defaulted every one of these 4 types instead of excluding them.
        Type judgment must happen in ``_backfill_mongo`` itself, before the
        resolver is ever called."""
        nodes = [
            {"_id": 1, "node_id": "list-bad", "properties": ["bad"]},
            {"_id": 2, "node_id": "str-bad", "properties": "bad"},
            {"_id": 3, "node_id": "empty-list", "properties": []},
            {"_id": 4, "node_id": "null-props", "properties": None},
            {"_id": 5, "node_id": "resolvable", "properties": {"source_path": "/packs/pack-m/x.md"}},
        ]
        db = self._FakeMongoDb(nodes, sources=[])

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        # None of the 4 non-dict docs were touched by update_many -- their
        # raw properties value is byte-for-byte unchanged (no dotted $set
        # was ever attempted against them).
        assert db._colls["nodes"]._docs[1]["properties"] == ["bad"]
        assert db._colls["nodes"]._docs[2]["properties"] == "bad"
        assert db._colls["nodes"]._docs[3]["properties"] == []
        assert db._colls["nodes"]._docs[4]["properties"] is None
        # The one genuinely resolvable doc still gets backfilled normally.
        assert db._colls["nodes"]._docs[5]["properties"]["pack_id"] == "pack-m"
        assert results["nodes"]["missing"] == 5
        assert results["nodes"]["updated"] == 1
        assert results["nodes"]["excluded"] == 4

    def test_r4c_7b_missing_properties_key_not_excluded_gets_default(self):
        """The over-exclusion guard: a document where the properties key is
        ENTIRELY ABSENT (not merely non-dict) must NOT be excluded -- a
        dotted ``$set`` happily creates the nested document, so this still
        goes through the resolver (-> assumed -> default), exactly as
        before R4-C."""
        nodes = [{"_id": 1, "node_id": "no-props-key"}]
        db = self._FakeMongoDb(nodes, sources=[])

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        assert db._colls["nodes"]._docs[1]["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID
        assert results["nodes"]["missing"] == 1
        assert results["nodes"]["updated"] == 1
        assert results["nodes"]["excluded"] == 0

    def test_r4c_8_mongo_excluded_count_demotes_docs_stage_to_skipped_rc3(self):
        """#146 P1(b): _docs_stage_outcome's existing excluded-count
        demotion rule (-> skipped -> rc 3, see TestStageOutcomesAndExitCodes
        for the generic docs_backfill-skipped-gates-rc3 wiring) must reach
        the Mongo-shaped stats dict too, not just the SQL-backed
        doc_nodes/doc_sources shape."""
        stats = {
            "nodes": {"total": 5, "missing": 5, "updated": 1, "excluded": 4},
            "sources": {"total": 0, "missing": 0, "updated": 0, "excluded": 0},
        }
        outcome, reason = migrate._docs_stage_outcome(stats)
        assert outcome == "skipped"
        assert "4 row(s) left unattributed" in reason


# ---------------------------------------------------------------------------
# R5-B (PR #177 review round 5 P1): a pack_id that ONLY exists in doc
# storage (self path-inference, or a value an interrupted prior run already
# stamped) with NO graph content of its own is invisible to step 3's
# graph-only enumeration -- main()'s new step 3.5 (``_register_doc_packs``)
# must register it BEFORE step 4 writes it onto any doc row, or the
# migration exits 0 having attributed content to an unregistered pack_id.
# ---------------------------------------------------------------------------


class TestDocDerivedPackIdRegistration:
    def test_1_doc_node_self_path_only_pack_gets_registered(self, bootstrapped_owner, env):
        """No graph content for "pack-s" at all -- only step 3.5's
        doc-derived preflight can find and register it."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "solo-doc", {"source_path": "/packs/pack-s/x.md"}
            )
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        sql = make_sql_store(get_settings())
        pack = get_pack(sql, "pack-s")
        assert pack is not None
        assert pack["owner_id"] == bootstrapped_owner

    def test_2_doc_source_self_path_only_pack_gets_registered(self, bootstrapped_owner, env):
        """Same as test 1 but inferred from doc_sources' metadata.source_path
        (the OTHER SQL-backed doc table, no graph twin lookup involved)."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_source("src-t", "some text", {"source_path": "/packs/pack-t/doc.md"})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        sql = make_sql_store(get_settings())
        pack = get_pack(sql, "pack-t")
        assert pack is not None
        assert pack["owner_id"] == bootstrapped_owner

    def test_3_dry_run_preview_finds_candidate_and_writes_nothing(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_doc_store, make_graph_store, make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        seed = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            seed.upsert_node_doc(
                "concept", "Entity", "solo-doc", {"source_path": "/packs/pack-s/x.md"}
            )
        finally:
            seed.close()

        settings = get_settings()
        sql = make_sql_store(settings)
        docs = make_doc_store(settings)
        graph = make_graph_store(settings)
        try:
            preview = migrate._register_doc_packs(
                sql,
                docs,
                graph,
                bootstrapped_owner,
                migrate.DEFAULT_PACK_ID,
                apply=False,
                accept_foreign_owned_packs=False,
            )
        finally:
            docs.close()
            graph.close()
        assert preview["doc_distinct_packs"] == 1
        assert preview["unregistered"] == 1
        assert preview["created"] == 0  # dry-run: nothing actually inserted
        assert get_pack(sql, "pack-s") is None

        doc_path = env / "doc_store.db"
        before = (doc_path.stat().st_size, doc_path.stat().st_mtime_ns)

        rc = migrate.main([])  # full dry-run

        assert rc == 0
        after = (doc_path.stat().st_size, doc_path.stat().st_mtime_ns)
        assert before == after
        assert get_pack(sql, "pack-s") is None

    def test_4_rerun_self_heals_pack_id_already_stamped_but_unregistered(
        self, bootstrapped_owner, env
    ):
        """Simulates an INTERRUPTED prior run: the doc row already carries
        pack_id="pack-s" (so a plain missing-row rescan would never see it
        again), but the registry has no "pack-s" row -- the exact state an
        old run stopping between step 4 and a (nonexistent, pre-fix) doc
        registration step would leave behind."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "already-stamped", {"pack_id": "pack-s"})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        sql = make_sql_store(get_settings())
        pack = get_pack(sql, "pack-s")
        assert pack is not None
        assert pack["owner_id"] == bootstrapped_owner

    def test_5_foreign_owned_doc_pack_id_aborts_before_any_doc_write(
        self, bootstrapped_owner, env, capsys
    ):
        """"pack-foreign-doc" has NO graph content anywhere in this test
        (graph.db is never seeded) -- if this still aborts, it proves step
        3.5's OWN gate caught it, not step 3's graph-derived one."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack, get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, "pack-foreign-doc", "someone-else-owner", None, None, None)

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "solo-doc", {"source_path": "/packs/pack-foreign-doc/x.md"}
            )
        finally:
            docs.close()

        doc_path = env / "doc_store.db"
        before_size_mtime = (doc_path.stat().st_size, doc_path.stat().st_mtime_ns)
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            before_row = docs.get_node_doc("concept", "solo-doc")
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 1

        after_size_mtime = (doc_path.stat().st_size, doc_path.stat().st_mtime_ns)
        assert before_size_mtime == after_size_mtime  # the file itself is untouched

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            after_row = docs.get_node_doc("concept", "solo-doc")
        finally:
            docs.close()
        assert after_row == before_row
        assert after_row["properties"].get("pack_id") is None  # never backfilled

        foreign = get_pack(sql, "pack-foreign-doc")
        assert foreign["owner_id"] == "someone-else-owner"  # untouched

        captured = capsys.readouterr()
        assert "pack-foreign-doc" in captured.err
        assert "someone-else-owner" in captured.err
        assert "--accept-foreign-owned-packs" in captured.err
        assert "registry_enumeration: failed" in captured.out

    def test_5b_accept_foreign_owned_packs_flag_lets_doc_registration_proceed(
        self, bootstrapped_owner, env
    ):
        from opencrab.config import get_settings
        from opencrab.pack.ownership import _insert_pack, get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        sql = make_sql_store(get_settings())
        assert _insert_pack(sql, "pack-foreign-doc-2", "someone-else-owner", None, None, None)

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept",
                "Entity",
                "solo-doc-2",
                {"source_path": "/packs/pack-foreign-doc-2/x.md"},
            )
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup", "--accept-foreign-owned-packs"])
        assert rc == 0

        foreign = get_pack(sql, "pack-foreign-doc-2")
        assert foreign["owner_id"] == "someone-else-owner"  # left alone, not reassigned

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "solo-doc-2")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == "pack-foreign-doc-2"  # step 4 still ran

    def test_6_all_default_doc_rows_create_no_extra_registrations(self, bootstrapped_owner, env):
        """Existing-contract regression: nothing in doc storage resolves to
        anything but "default" -- step 3.5 must not manufacture spurious
        registrations."""
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "no-hint-1", {"note": "nothing to infer"})
            docs.upsert_source("no-hint-src", "some text", {"title": "nothing to infer"})
        finally:
            docs.close()

        sql = make_sql_store(get_settings())
        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        assert _packs_snapshot(sql) == [(migrate.DEFAULT_PACK_ID, bootstrapped_owner)]

    def test_7_registry_equals_default_union_graph_union_doc(self, bootstrapped_owner, env):
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node(
                "Dataset", "d1", {"pack_id": "pack-graph-only"}, space_id="resource"
            )
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "solo-doc", {"source_path": "/packs/pack-doc-only/x.md"}
            )
        finally:
            docs.close()

        sql = make_sql_store(get_settings())
        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 0

        registered = {pid for pid, _owner in _packs_snapshot(sql)}
        assert registered == {migrate.DEFAULT_PACK_ID, "pack-graph-only", "pack-doc-only"}

    def test_8_malformed_existing_pack_id_excluded_empty_string_treated_as_missing(
        self, bootstrapped_owner, env, capsys
    ):
        """A present-but-non-string existing pack_id (a JSON number here) is
        malformed -- excluded from registration AND left untouched by the
        backfill (it is NOT "missing" by the SQL definition). A
        present-but-EMPTY-STRING pack_id, by contrast, IS "missing" by that
        same SQL definition -- it must fall through to the ordinary
        resolver (-> default here), not be excluded as malformed.

        PR #177 review round 6 P1: a nonzero malformed count must demote
        ``registry_enumeration`` to ``skipped`` (rc 3) -- before this round,
        malformed values were warned about but no stage ever demoted, so
        this exact scenario used to exit 0 while the malformed row still
        pointed at no registry entry (see module docstring / round 6 fix
        design for the full bug)."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import get_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "bad-type-existing", {"pack_id": 12345})
            docs.upsert_node_doc("concept", "Entity", "empty-string-existing", {"pack_id": ""})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 3

        out = capsys.readouterr().out
        assert "malformed" in out
        assert "registry_enumeration: skipped" in out

        sql = make_sql_store(get_settings())
        assert get_pack(sql, "12345") is None

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            bad_row = docs.get_node_doc("concept", "bad-type-existing")
            empty_row = docs.get_node_doc("concept", "empty-string-existing")
        finally:
            docs.close()
        assert bad_row["properties"]["pack_id"] == 12345  # untouched, not overwritten
        assert empty_row["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID  # backfilled normally

    def test_9_step_3_5_failure_overwrites_registry_enumeration_to_failed(
        self, bootstrapped_owner, env, monkeypatch, capsys
    ):
        """A step 3.5 failure must surface as registry_enumeration: failed
        with rc 1, and must not let step 3's own outcome text reach the
        summary.

        NOTE on what this does and does not prove: today the
        stage_outcomes["registry_enumeration"] assignment happens AFTER step
        3.5, so the outer handler's setdefault would already produce
        "failed" here on its own -- the outcome assertions below therefore
        pin the CONTRACT, not the explicit overwrite. What is specific to
        the explicit overwrite is the reason text: the outer handler writes
        a bare str(exc), so the "(step 3.5) failed:" prefix appears only if
        the explicit overwrite ran. That is asserted separately below.
        """
        _seed_graph(env)
        _seed_doc(env)

        def _boom(*a, **kw):
            raise RuntimeError("simulated doc-registry failure")

        monkeypatch.setattr(migrate, "_register_doc_packs", _boom)

        rc = migrate.main(["--apply", "--skip-backup"])
        assert rc == 1

        out = capsys.readouterr().out
        assert "registry_enumeration: failed" in out
        assert "simulated doc-registry failure" in out
        # step 3's own outcome text must NOT survive into the summary.
        assert "registry_enumeration: applied" not in out
        assert "registry_enumeration: clean" not in out
        # Only the explicit overwrite produces this prefix -- the outer
        # handler's setdefault would write a bare str(exc) instead.
        assert "document-derived pack_id registration (step 3.5) failed" in out


class TestRoundFiveReviewConformance:
    """PR #177 review round 5 follow-ups: the step 3.5 extraction must not
    have changed step 3's observable behaviour, and the dry-run registry
    count must stay a count of DISTINCT ids."""

    def test_graph_foreign_owner_gate_aborts_before_printing_the_summary(
        self, bootstrapped_owner, env, capsys
    ):
        """The gate sat before the "graph distinct pack_id" summary before
        step 3.5 was extracted. On an abort the operator must still see the
        error and NO summary line for a run that is stopping."""
        from opencrab.config import get_settings
        from opencrab.pack.ownership import create_pack
        from opencrab.stores.factory import make_sql_store
        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "n1", {"pack_id": "squatted"}, space_id="concept")
        finally:
            graph.close()
        sql = make_sql_store(get_settings())
        create_pack(sql, "someone-else", "squatted", title="Not yours")

        rc = migrate.main([])
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert rc == 1
        # Original wording, byte-for-byte with the pre-refactor message.
        assert "already exist in the graph store's content" in combined
        # ...and no summary line for the aborted enumeration. That line IS
        # printed on a normal run (see test_x2_dry_run_report_matches_apply_result,
        # which asserts its "N not yet in the registry" tail), so its absence
        # here really does pin the gate-before-summary ordering.
        assert "graph distinct pack_id:" not in combined

    def test_dry_run_does_not_double_count_a_pack_in_both_graph_and_docs(
        self, bootstrapped_owner, env, capsys
    ):
        """A pack_id present in BOTH graph and doc content is ONE registry
        row. In a dry-run nothing is inserted, so step 3.5's re-read of the
        registry would report it as unregistered a second time unless step
        3's planned ids are folded in."""
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "g1", {"pack_id": "pack-shared"}, space_id="concept")
        finally:
            graph.close()
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc(
                "concept", "Entity", "d1", {"source_path": "/packs/pack-shared/x.md"}
            )
        finally:
            docs.close()

        rc_dry = migrate.main([])
        out_dry = capsys.readouterr().out
        assert rc_dry == 0
        # default + pack-shared == 2 distinct rows, counted once each.
        assert "2 row(s) needed registering" in out_dry
        assert "doc-derived=0" in out_dry
        # The step 3.5 stage line must agree with that summary: pack-shared
        # was already planned by step 3, so step 3.5 has nothing left to
        # register. (Pins the stage print's own de-duplication, not just the
        # summed total.)
        assert (
            "doc-derived distinct pack_id: 1 total "
            "(already-present=0, predicted=1), 0 not yet in the registry" in out_dry
        )

        rc_apply = migrate.main(["--apply", "--skip-backup"])
        out_apply = capsys.readouterr().out
        assert rc_apply == 0
        # The apply run must report the SAME total the dry-run predicted.
        assert "2 row(s) needed registering" in out_apply

        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        assert migrate._registered_pack_ids(sql) == {migrate.DEFAULT_PACK_ID, "pack-shared"}

    def test_dry_run_does_not_double_count_default_from_graph_prediction(
        self, bootstrapped_owner, env, capsys
    ):
        """A packless graph row makes step 2 predict `default` for it, so in a
        dry-run (where step 1 has not really inserted the row) `default` shows
        up BOTH in default_pending and in the graph candidates. Counting it
        twice made the dry-run total exceed the --apply total."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(str(env / "graph.db"))
        try:
            graph.upsert_node("Entity", "packless", {"name": "no pack"}, space_id="concept")
        finally:
            graph.close()

        assert migrate.main([]) == 0
        out_dry = capsys.readouterr().out

        assert migrate.main(["--apply", "--skip-backup"]) == 0
        out_apply = capsys.readouterr().out

        # Exactly one distinct row (`default`) needs registering in both modes.
        assert "1 row(s) needed registering" in out_dry
        assert "1 row(s) needed registering" in out_apply

        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        assert migrate._registered_pack_ids(sql) == {migrate.DEFAULT_PACK_ID}


# ---------------------------------------------------------------------------
# PR #177 review round 6 P1: a truthy non-string pack_id (e.g.
# {"pack_id": 12345}) is malformed -- neither "missing" (SQL's predicate
# only matches NULL/'') nor safely coercible to a string (PG's JSONB ->>
# could make it collide with a foreign-owned string pack_id). Before this
# round no stage ever demoted on a nonzero malformed count, so a run left
# real content pointing at no registry row and still exited 0. See
# scripts/migrate_pack_ownership.py's _classify_pack_id and the round 6 fix
# design doc for the full bug and its Mongo array-matching codex counter-
# example.
# ---------------------------------------------------------------------------


class TestClassifyPackId:
    """Table-based unit test for the one shared classifier (regression
    test 8 of the round 6 fix design)."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (migrate._MISSING, "missing"),
            (None, "missing"),
            ("", "missing"),
            ("pack-a", "valid"),
            (12345, "malformed"),
            ([""], "malformed"),
            ({"a": 1}, "malformed"),
            (True, "malformed"),
            (False, "malformed"),
        ],
    )
    def test_classification_table(self, raw, expected):
        assert migrate._classify_pack_id(raw) == expected

    def test_bool_is_malformed_not_confused_with_int(self):
        """Explicit regression: ``isinstance(True, int)`` is ``True`` in
        Python, so a naive numeric-first check would misclassify a bool.
        ``_classify_pack_id`` must check ``isinstance(raw, str)`` before any
        numeric-shaped branch so ``True``/``False`` fall through to
        malformed like any other non-string truthy value."""
        assert migrate._classify_pack_id(True) == "malformed"
        assert migrate._classify_pack_id(False) == "malformed"


class TestMalformedPackIdDemotesRegistryEnumeration:
    """Regression tests 1-6 of the round 6 fix design: a truthy non-string
    pack_id anywhere in doc storage must demote registry_enumeration to
    skipped (-> rc 3), never silently pass with rc 0."""

    def test_1_doc_nodes_truthy_numeric_pack_id_demotes_to_rc3(
        self, bootstrapped_owner, env, capsys
    ):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "malformed-node", {"pack_id": 12345})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out

        assert rc == 3
        assert "registry_enumeration: skipped" in out
        assert "1 document-derived pack_id value(s) were malformed" in out

    def test_2_doc_nodes_row_left_unchanged_by_step_4(self, bootstrapped_owner, env, capsys):
        """Pins the fix's premise 2: the malformed row is NOT "missing" by
        the SQL definition, so step 4 must never touch it -- it is not a
        backfill target, only an operator-visible registration gap."""
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "malformed-node", {"pack_id": 12345})
        finally:
            docs.close()

        migrate.main(["--apply", "--skip-backup"])

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "malformed-node")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == 12345  # untouched, still a raw int

    def test_3_doc_sources_metadata_truthy_list_pack_id_demotes_to_rc3(
        self, bootstrapped_owner, env, capsys
    ):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_source("malformed-source", "some text", {"pack_id": ["a", "b"]})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out

        assert rc == 3
        assert "registry_enumeration: skipped" in out
        assert "1 document-derived pack_id value(s) were malformed" in out

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_source("malformed-source")
        finally:
            docs.close()
        assert row["metadata"]["pack_id"] == ["a", "b"]  # untouched

    def test_4_no_malformed_values_stays_rc0(self, bootstrapped_owner, env, capsys):
        """Over-demotion guard: a run with zero malformed values must keep
        exiting 0, same as before this round."""
        _seed_graph(env)
        _seed_doc(env)

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "malformed" not in out
        assert "registry_enumeration: skipped" not in out

    def test_5_existing_skip_reason_is_preserved_not_overwritten(
        self, bootstrapped_owner, env, monkeypatch, capsys
    ):
        """When registry_enumeration is ALREADY skipped for a more specific
        reason (graph wrapper unavailable, same setup as
        test_exit_code_3_when_wrapper_unavailable_but_graph_db_readable),
        the malformed reason must be appended, never replace it."""
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        class _UnavailableGraph:
            available = False

        _seed_graph(env)
        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "malformed-node", {"pack_id": 12345})
        finally:
            docs.close()
        monkeypatch.setattr(
            "opencrab.stores.factory.make_graph_store", lambda settings: _UnavailableGraph()
        )

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out

        assert rc == 3
        assert "registry_enumeration: skipped" in out
        # Original, more specific reason survives...
        assert "graph unavailable -- pack_id enumeration skipped" in out
        # ...with the malformed reason appended, not replacing it.
        assert "1 document-derived pack_id value(s) were malformed" in out

    def test_6_empty_string_pack_id_still_treated_as_missing_not_malformed(
        self, bootstrapped_owner, env, capsys
    ):
        """Existing round 5 contract regression guard: an empty-string
        pack_id is "missing", backfilled normally to default, and must
        NEVER be counted toward malformed_excluded."""
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            docs.upsert_node_doc("concept", "Entity", "empty-string-node", {"pack_id": ""})
        finally:
            docs.close()

        rc = migrate.main(["--apply", "--skip-backup"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "malformed" not in out

        docs = LocalSQLDocStore(str(env / "doc_store.db"))
        try:
            row = docs.get_node_doc("concept", "empty-string-node")
        finally:
            docs.close()
        assert row["properties"]["pack_id"] == migrate.DEFAULT_PACK_ID


class TestMongoMalformedArrayPackIdCodexCounterexample:
    """Regression test 7 of the round 6 fix design -- the exact codex
    counterexample from the design-verification loop: MongoDB's dotted
    equality matches ARRAY ELEMENTS, so a document like
    ``{"properties": {"pack_id": [""]}}`` used to be invisible to BOTH
    ``_mongo_existing_pack_ids``'s ``$ne`` filter (excluded because one
    array element does equal ``""``) and (before this round) any
    pre-resolver check in ``_backfill_mongo`` -- letting
    ``resolve_row_pack_id`` stringify the truthy list into a FABRICATED
    pack_id like ``"['']"``."""

    class _FakeMongoUpdateResult:
        def __init__(self, modified_count: int):
            self.modified_count = modified_count

    class _FakeMongoCollection(TestBackfillMongoPathInference._FakeMongoCollection):
        """Reuses the existing fake's find()/update_many() -- see that
        class for the query-shape support this double implements."""

    class _FakeMongoDb:
        def __init__(self, nodes: list[dict], sources: list[dict]):
            self._colls = {
                "nodes": TestMongoMalformedArrayPackIdCodexCounterexample._FakeMongoCollection(nodes),
                "sources": TestMongoMalformedArrayPackIdCodexCounterexample._FakeMongoCollection(sources),
            }

        def __getitem__(self, name):
            return self._colls[name]

    def test_a_array_pack_id_counted_as_malformed_by_existing_scan(self):
        nodes = [{"_id": 1, "node_id": "arr-empty", "properties": {"pack_id": [""]}}]
        sources = [{"_id": 2, "source_id": "arr-none", "metadata": {"pack_id": [None]}}]
        db = self._FakeMongoDb(nodes, sources)

        pack_ids, malformed = migrate._mongo_existing_pack_ids(db)

        assert pack_ids == set()
        assert malformed == 2

    def test_b_update_many_never_called_for_array_pack_id(self):
        """update_many is only invoked (via the fake's own bookkeeping) for
        ids present in `by_pack` -- the malformed doc's `_id` must never
        land there, so `_docs[...]` stays byte-for-byte unchanged."""
        nodes = [{"_id": 1, "node_id": "arr-empty", "properties": {"pack_id": [""]}}]
        db = self._FakeMongoDb(nodes, sources=[])

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        # Untouched -- no $set was ever attempted against this document.
        assert db._colls["nodes"]._docs[1]["properties"]["pack_id"] == [""]
        assert results["nodes"]["excluded"] == 1
        assert results["nodes"]["updated"] == 0

    def test_c_no_fabricated_string_key_in_by_pack(self):
        """Before this round, resolve_row_pack_id's ``str(existing)`` on a
        truthy list would fabricate a by_pack key like ``"['']"`` -- assert
        that key structurally cannot appear now that the pre-resolver check
        excludes the row before the resolver is ever called."""
        nodes = [{"_id": 1, "node_id": "arr-empty", "properties": {"pack_id": [""]}}]
        db = self._FakeMongoDb(nodes, sources=[])

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        assert "['']" not in results["nodes"]["by_pack"]
        assert results["nodes"]["by_pack"] == {}

    def test_d_sources_metadata_array_none_pack_id_also_excluded(self):
        sources = [{"_id": 1, "source_id": "arr-none", "metadata": {"pack_id": [None]}}]
        db = self._FakeMongoDb(nodes=[], sources=sources)

        results = migrate._backfill_mongo(db, migrate.DEFAULT_PACK_ID, apply=True)

        assert db._colls["sources"]._docs[1]["metadata"]["pack_id"] == [None]
        assert results["sources"]["excluded"] == 1
        assert results["sources"]["updated"] == 0
        assert "[None]" not in results["sources"]["by_pack"]


# ---------------------------------------------------------------------------
# R8-A (PR #177 review round 8 P2): _backup_sqlite_files must also back up
# the sqlite-vec vector file before _backfill_vector rewrites it in place
# (delete+reinsert, unrecoverable -- upsert_texts RE-EMBEDS), and must warn,
# with the actual path, when the resolved local vector backend is NOT a
# single SQLite file this online-.backup()-based helper can copy.
# ---------------------------------------------------------------------------


class TestBackupIncludesVectorFile:
    def _settings(self, monkeypatch, **env_overrides):
        from opencrab.config import get_settings

        for key, value in env_overrides.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    def test_sqlite_vec_file_is_backed_up_and_returned(self, env, tmp_path, monkeypatch):
        """Test 1: helper called DIRECTLY (main() never returns/uses the
        backed_up list, so this is the only way to observe it)."""
        import sqlite3

        settings = self._settings(monkeypatch, VECTOR_BACKEND="sqlite-vec")
        assert settings.vector_backend_resolved == "sqlite-vec"
        vec_path = env / settings.vector_db_file
        conn = sqlite3.connect(str(vec_path))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        backup_dir = tmp_path / "backups"
        backed_up = migrate._backup_sqlite_files(str(env), str(backup_dir), settings)

        dst = backup_dir / settings.vector_db_file
        assert str(dst) in backed_up
        assert dst.is_file()

    def test_backup_content_matches_source_plain_sqlite_table(self, env, tmp_path, monkeypatch):
        """Test 2, non-skip half: a plain SQLite table's rows survive the
        online .backup() copy identically, regardless of whether sqlite_vec
        is installed in this environment."""
        import sqlite3

        settings = self._settings(monkeypatch, VECTOR_BACKEND="sqlite-vec")
        vec_path = env / settings.vector_db_file
        conn = sqlite3.connect(str(vec_path))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.executemany("INSERT INTO t (id, val) VALUES (?, ?)", [(1, "a"), (2, "b"), (3, "c")])
        conn.commit()
        conn.close()

        backup_dir = tmp_path / "backups"
        migrate._backup_sqlite_files(str(env), str(backup_dir), settings)

        dst = backup_dir / settings.vector_db_file
        dst_conn = sqlite3.connect(str(dst))
        try:
            rows = dst_conn.execute("SELECT id, val FROM t ORDER BY id").fetchall()
        finally:
            dst_conn.close()
        assert rows == [(1, "a"), (2, "b"), (3, "c")]

    def test_backup_content_matches_source_real_vec0_table(self, env, tmp_path, monkeypatch):
        """Test 2, vec0 half (skipped if sqlite_vec is not installed): this
        change's actual premise is that a plain sqlite3.connect() + .backup()
        copies the vec0 SHADOW TABLES too, not just the virtual table's own
        rows -- prove it by round-tripping real vec0 content, not an empty
        file."""
        pytest.importorskip("sqlite_vec")
        from _vec_helpers import MockEF

        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        settings = self._settings(monkeypatch, VECTOR_BACKEND="sqlite-vec")
        vec_path = env / settings.vector_db_file
        store = SqliteVecStore(
            db_path=str(vec_path),
            embedding_function=MockEF(16),
            dim=16,
            collection_name=settings.vector_collection,
        )
        if not store.available:
            pytest.skip("sqlite-vec가 이 환경에서 초기화되지 않음")
        try:
            store.upsert_texts(
                ["hello world", "goodbye"],
                [{"pack_id": "pack-a"}, {"pack_id": "pack-b"}],
                ["v1", "v2"],
            )
        finally:
            store.close()

        backup_dir = tmp_path / "backups"
        migrate._backup_sqlite_files(str(env), str(backup_dir), settings)

        dst = backup_dir / settings.vector_db_file
        restored = SqliteVecStore(
            db_path=str(dst),
            embedding_function=MockEF(16),
            dim=16,
            collection_name=settings.vector_collection,
        )
        try:
            assert restored.count() == 2
            doc1 = restored.get_by_id("v1")
            doc2 = restored.get_by_id("v2")
        finally:
            restored.close()
        assert doc1 is not None
        assert doc1["document"] == "hello world"
        assert doc1["metadata"]["pack_id"] == "pack-a"
        assert doc2 is not None
        assert doc2["document"] == "goodbye"
        assert doc2["metadata"]["pack_id"] == "pack-b"

    def test_vector_db_file_env_override_uses_that_name(self, env, tmp_path, monkeypatch):
        """Test 3: VECTOR_DB_FILE renamed -- the backup must follow the
        configured name, never a hardcoded "vectors.db"."""
        import sqlite3

        settings = self._settings(
            monkeypatch, VECTOR_BACKEND="sqlite-vec", VECTOR_DB_FILE="custom_vectors.db"
        )
        assert settings.vector_db_file == "custom_vectors.db"
        vec_path = env / "custom_vectors.db"
        conn = sqlite3.connect(str(vec_path))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        backup_dir = tmp_path / "backups"
        backed_up = migrate._backup_sqlite_files(str(env), str(backup_dir), settings)

        assert (backup_dir / "custom_vectors.db").is_file()
        assert not (backup_dir / "vectors.db").exists()
        assert str(backup_dir / "custom_vectors.db") in backed_up

    def test_missing_vector_file_is_silently_skipped(self, env, tmp_path, monkeypatch):
        """Test 4: no vectors.db on disk -> quietly skipped, same rule the
        three pre-existing files already follow."""
        settings = self._settings(monkeypatch, VECTOR_BACKEND="sqlite-vec")
        backup_dir = tmp_path / "backups"
        backed_up = migrate._backup_sqlite_files(str(env), str(backup_dir), settings)
        assert backed_up == []
        assert not (backup_dir / settings.vector_db_file).exists()

    def test_existing_vector_backup_target_raises_system_exit_2(self, env, tmp_path, monkeypatch):
        """Test 5: the pre-existing "refuse to overwrite" discipline applies
        to the vector file too -- SystemExit(2), not a plain return."""
        import sqlite3

        settings = self._settings(monkeypatch, VECTOR_BACKEND="sqlite-vec")
        vec_path = env / settings.vector_db_file
        conn = sqlite3.connect(str(vec_path))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(parents=True)
        (backup_dir / settings.vector_db_file).write_bytes(b"pre-existing")

        with pytest.raises(SystemExit) as exc_info:
            migrate._backup_sqlite_files(str(env), str(backup_dir), settings)
        assert exc_info.value.code == 2

    def test_non_sqlite_vec_local_backend_warns_with_path_and_still_backs_up_three_files(
        self, env, tmp_path, monkeypatch, capsys
    ):
        """Test 6: local chroma (VECTOR_BACKEND=chroma, is_local) is a
        DIRECTORY (<local_data_dir>/chroma, PersistentClient), not a single
        SQLite file -- warn with that exact path, do not abort, and the
        three SQLite files still back up normally."""
        import sqlite3

        settings = self._settings(monkeypatch, VECTOR_BACKEND="chroma")
        assert settings.vector_backend_resolved == "chroma"
        assert settings.is_local

        conn = sqlite3.connect(str(env / "opencrab.db"))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        backup_dir = tmp_path / "backups"
        backed_up = migrate._backup_sqlite_files(str(env), str(backup_dir), settings)
        out = capsys.readouterr().out

        expected_chroma_path = str(env / "chroma")
        assert expected_chroma_path in out
        assert (backup_dir / "opencrab.db").is_file()
        assert str(backup_dir / "opencrab.db") in backed_up


# ---------------------------------------------------------------------------
# R8-B (PR #177 review round 8 P2): _predict_node_pack_map and
# _read_actual_node_pack_ids must chunk their node_id IN (...) query at
# _GRAPH_TWIN_CHUNK_SIZE, same as _graph_twin_pack_map already does --
# otherwise a large unattributed-node-id set can exceed SQLite's
# bound-parameter limit and even dry-run fails outright.
# ---------------------------------------------------------------------------


class TestNodePackMapChunking:
    def _seed_many(self, env, n: int):
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = env / "graph.db"
        store = LocalGraphStore(str(db_path))
        node_ids: list[str] = []
        try:
            for i in range(n):
                nid = f"n{i}"
                # A realistic mix: every 5th node path-infers a distinct
                # pack, the rest fall through to the assumed default.
                props = (
                    {"source_path": f"/packs/pack-{i % 3}/x.md"}
                    if i % 5 == 0
                    else {"note": "nothing to infer"}
                )
                store.upsert_node("Entity", nid, props, space_id="concept")
                node_ids.append(nid)
        finally:
            store.close()
        return db_path, node_ids

    def test_predict_survives_low_bind_limit_with_over_500_ids(self, env, monkeypatch):
        """Test 7 (predict half): SQLITE_LIMIT_VARIABLE_NUMBER lowered to
        exactly the chunk size (500) via a monkeypatched sqlite3.connect --
        each function opens its OWN connection, so patching the connect
        constructor (not a pre-existing connection) is the only way to make
        the limit apply to it. 501 node_ids means an unchunked single IN
        (...) query would fail with "too many SQL variables"; the chunked
        implementation must complete and match a chunk-safe (one id at a
        time) oracle -- never the unbounded/unlimited query result."""
        import sqlite3

        from opencrab.ontology.pack_provenance import resolve_row_pack_id

        db_path, node_ids = self._seed_many(env, 501)
        assert len(node_ids) > migrate._GRAPH_TWIN_CHUNK_SIZE

        # Chunk-safe oracle, built over a normal connection BEFORE the
        # connect() monkeypatch below is installed.
        expected: dict[str, str] = {}
        oracle = sqlite3.connect(str(db_path))
        oracle.row_factory = sqlite3.Row
        try:
            cur = oracle.cursor()
            for nid in node_ids:
                cur.execute(
                    "SELECT node_id, properties FROM graph_nodes WHERE node_id = ?", (nid,)
                )
                row = cur.fetchone()
                pid, _reason = resolve_row_pack_id(row["properties"], row, migrate.DEFAULT_PACK_ID)
                if pid is not None:
                    expected[row["node_id"]] = pid
        finally:
            oracle.close()

        orig_connect = sqlite3.connect

        def _limited_connect(*a, **kw):
            conn = orig_connect(*a, **kw)
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, migrate._GRAPH_TWIN_CHUNK_SIZE)
            return conn

        monkeypatch.setattr(sqlite3, "connect", _limited_connect)

        predicted, ambiguous = migrate._predict_node_pack_map(
            db_path, node_ids, migrate.DEFAULT_PACK_ID
        )
        assert predicted == expected
        assert ambiguous == {}

    def test_read_actual_survives_low_bind_limit_with_over_500_ids(self, env, monkeypatch):
        """Test 7 (read-actual half): same limit-lowering technique, applied
        AFTER a real backfill_pack_ids run, against _read_actual_node_pack_ids."""
        import json
        import sqlite3

        from opencrab.ontology.pack_provenance import backfill_pack_ids

        db_path, node_ids = self._seed_many(env, 501)
        backfill_pack_ids(db_path, assume_pack_id=migrate.DEFAULT_PACK_ID, dry_run=False)

        # Chunk-safe oracle over the POST-backfill state, one id at a time,
        # built BEFORE the connect() monkeypatch below is installed.
        expected: dict[str, str] = {}
        oracle = sqlite3.connect(str(db_path))
        try:
            cur = oracle.cursor()
            for nid in node_ids:
                cur.execute(
                    "SELECT node_id, properties FROM graph_nodes WHERE node_id = ?", (nid,)
                )
                node_id, raw = cur.fetchone()
                try:
                    props = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    props = {}
                if isinstance(props, dict) and props.get("pack_id"):
                    expected[node_id] = str(props["pack_id"])
        finally:
            oracle.close()

        orig_connect = sqlite3.connect

        def _limited_connect(*a, **kw):
            conn = orig_connect(*a, **kw)
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, migrate._GRAPH_TWIN_CHUNK_SIZE)
            return conn

        monkeypatch.setattr(sqlite3, "connect", _limited_connect)

        actual, ambiguous = migrate._read_actual_node_pack_ids(db_path, node_ids)
        assert actual == expected
        assert ambiguous == {}


class TestNodePackMapChunkingSemanticAmbiguity:
    """Test 8: a semantic ambiguity -- same node_id, two DIFFERENT
    node_types, disagreeing pack_ids -- must still be classified ambiguous
    by BOTH functions after the R8-B chunking change. This does not depend
    on chunk boundaries at all (see the R8-B inline comment in
    migrate_pack_ownership.py: the query chunks the node_id VALUE LIST, not
    graph rows, and the caller already deduped node_ids, so a given id's
    graph_nodes rows are always returned together by whichever single chunk
    contains that id) -- this is the same invariant
    TestDuplicateNodeIdAmbiguity already covers pre-round-8; repeated here
    scoped to both chunked functions as the round-8 fix design's explicit
    regression item."""

    def test_different_node_type_same_node_id_disagreeing_packs_is_ambiguous(self, env):
        from opencrab.ontology.pack_provenance import backfill_pack_ids
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = env / "graph.db"
        store = LocalGraphStore(str(db_path))
        try:
            store.upsert_node(
                "TypeA", "shared", {"source_path": "/packs/pack-a/x.md"}, space_id="concept"
            )
            store.upsert_node(
                "TypeB", "shared", {"source_path": "/packs/pack-b/x.md"}, space_id="concept"
            )
        finally:
            store.close()

        predicted, pred_ambiguous = migrate._predict_node_pack_map(
            db_path, ["shared"], migrate.DEFAULT_PACK_ID
        )
        assert predicted == {}
        assert pred_ambiguous == {"shared": sorted(["pack-a", "pack-b"])}

        backfill_pack_ids(db_path, assume_pack_id=migrate.DEFAULT_PACK_ID, dry_run=False)

        actual, act_ambiguous = migrate._read_actual_node_pack_ids(db_path, ["shared"])
        assert actual == {}
        assert act_ambiguous == {"shared": sorted(["pack-a", "pack-b"])}


class TestNodePackMapSmallInputUnchanged:
    """Test 9: a small (<500) input's result is unchanged by chunking --
    _chunked([...], 500) on a short list yields exactly one chunk, so this
    is byte-for-byte the same query as the pre-round-8 unchunked version."""

    def test_small_input_result_unchanged(self, env):
        from opencrab.ontology.pack_provenance import backfill_pack_ids
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = env / "graph.db"
        store = LocalGraphStore(str(db_path))
        try:
            store.upsert_node(
                "Entity",
                "n-inferred",
                {"source_path": "/data/packs/pack-a/x.md"},
                space_id="concept",
            )
            store.upsert_node("Entity", "n-assumed", {"note": "nothing to infer"}, space_id="concept")
        finally:
            store.close()

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
