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
        from opencrab.packs.registry import get_pack
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
        from opencrab.packs.registry import get_pack
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
        """Acceptance criterion: registry row count == distinct graph
        pack_id count + 1 (the default pack)."""
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
        stats = migrate._backfill_vector(store, ["n1"], "default", apply=False)
        assert stats == {"checked": 1, "missing": 1, "updated": 0}
        assert store.upserts == []

    def test_apply_backfills_missing_only(self):
        store = _FakeVectorStore(
            {
                "n1": {"document": "d1", "metadata": {}},
                "n2": {"document": "d2", "metadata": {"pack_id": "already-tagged"}},
            }
        )
        stats = migrate._backfill_vector(store, ["n1", "n2"], "default", apply=True)
        assert stats == {"checked": 2, "missing": 1, "updated": 1}
        assert store.upserts == [("n1", {"pack_id": "default"})]
        assert store._rows["n2"]["metadata"]["pack_id"] == "already-tagged"

    def test_unknown_node_id_is_skipped_not_an_error(self):
        store = _FakeVectorStore({})
        stats = migrate._backfill_vector(store, ["ghost"], "default", apply=True)
        assert stats == {"checked": 1, "missing": 0, "updated": 0}

    def test_store_without_get_by_id_is_skipped(self):
        class NoGetById:
            available = True

        stats = migrate._backfill_vector(NoGetById(), ["n1"], "default", apply=True)
        assert stats == {"checked": 0, "missing": 0, "updated": 0}

    def test_unavailable_store_is_skipped(self):
        store = _FakeVectorStore({"n1": {"document": "d1", "metadata": {}}})
        store.available = False
        stats = migrate._backfill_vector(store, ["n1"], "default", apply=True)
        assert stats == {"checked": 0, "missing": 0, "updated": 0}
