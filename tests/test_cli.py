"""Contract tests for opencrab.cli (Click command interface).

Scope (per test-plan Wave A / A2): init, status, ingest, query, manifest,
packs list/show/backfill-pack-id, extract.

Embeddings/network are mocked so these tests never touch LM Studio or
download/load a GGUF model:
  - ingest/query patch ``opencrab.stores.factory.make_vector_store`` to return
    a MockEF-backed sqlite-vec store (reusing tests/_vec_helpers.py, the same
    helper test_vector_store_parity.py/test_store_concurrency.py use).
  - extract patches ``opencrab.ontology.extractor.LLMExtractor`` so no
    Anthropic API call is made.

Each command group separates Normal / Error / Edge cases with a comment
banner, per the test-plan policy (contract-verifying only, no padding).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from _vec_helpers import build_vector_store
from click.testing import CliRunner

from opencrab.cli import main

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """LOCAL_DATA_DIR/STORAGE_MODE fixed to an isolated tmp dir; settings cache
    cleared before/after so other test modules' env leakage can't bleed in."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def bootstrapped(cli_env):
    """``cli_env`` plus a bootstrapped local user.

    Store-writing CLI commands (`ingest`, `extract`) refuse to run without a
    local principal since #145 -- a write has to be attributable. Tests that
    exercise what those commands *do*, rather than the auth gate itself, need
    a user to exist first. Returns the local user_id.
    """
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    user_id, _secret = bootstrap_local_user(make_sql_store(get_settings()))
    return user_id


@pytest.fixture()
def mock_vector_store(tmp_path):
    """Patch factory.make_vector_store so ingest/query never construct the
    real KURE embedding chain (OpenAI-compatible server + GGUF fallback) —
    confirmed by direct reproduction that the fallback loads a real local
    GGUF model. cli.py imports make_vector_store *inside* each command
    function, so patching the factory module attribute here is picked up by
    the next ``from opencrab.stores.factory import make_vector_store``."""
    store = build_vector_store("sqlite-vec", tmp_path, dim=32)
    with patch("opencrab.stores.factory.make_vector_store", return_value=store):
        yield store


def _parse_leading_json(output: str) -> dict:
    """Parse the first JSON object embedded in ``output``, ignoring any
    leading warning lines (e.g. the --apply/--dry-run reconciliation
    warnings) and trailing human-readable text (e.g. backfill-pack-id's
    dry-run hint line, printed directly after the JSON with no blank-line
    separator)."""
    brace = output.index("{")
    return json.JSONDecoder().raw_decode(output[brace:])[0]


def _write_pack_manifest(root: Path, pack_id: str, **fields) -> Path:
    stage = root / "packs" / pack_id / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    manifest = {"pack_id": pack_id, **fields}
    path = stage / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class TestInit:
    # init now also bootstraps a local user + token (#144), which touches the
    # SQL store at LOCAL_DATA_DIR -- these need cli_env (not just cwd
    # isolation) so they never reach the real default data dir.

    # --- Normal ---
    def test_fresh_dir_creates_env(self, cli_env, runner):
        with runner.isolated_filesystem():
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert Path(".env").exists()
            assert "Created" in result.output

    # --- Edge ---
    def test_existing_env_not_clobbered_without_force(self, cli_env, runner):
        with runner.isolated_filesystem():
            Path(".env").write_text("CUSTOM=1\n")
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert Path(".env").read_text() == "CUSTOM=1\n"
            assert "already exists" in result.output

    def test_force_overwrites_existing_env(self, cli_env, runner):
        with runner.isolated_filesystem():
            Path(".env").write_text("CUSTOM=1\n")
            result = runner.invoke(main, ["init", "--force"])
            assert result.exit_code == 0
            assert Path(".env").read_text() != "CUSTOM=1\n"
            assert "Created" in result.output


class TestInitBootstrap:
    """#144: 'opencrab init' bootstraps the local owner user + token once."""

    def test_bootstraps_local_user_and_prints_token_once(self, cli_env, runner):
        with runner.isolated_filesystem():
            result = runner.invoke(main, ["init"])
        assert result.exit_code == 0
        assert "Bootstrap token" in result.output
        assert "lc_" in result.output

        from opencrab.auth import list_users
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        users = list_users(sql)
        assert len(users) == 1
        assert users[0]["is_local"] is True

    def test_second_run_is_idempotent(self, cli_env, runner):
        with runner.isolated_filesystem():
            first = runner.invoke(main, ["init"])
            assert first.exit_code == 0
            second = runner.invoke(main, ["init", "--force"])
        assert second.exit_code == 0
        assert "already bootstrapped" in second.output
        assert "Bootstrap token" not in second.output

        from opencrab.auth import list_users
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        users = list_users(sql)
        assert len(users) == 1

    def test_init_fails_when_sql_store_unavailable(self, cli_env, runner):
        """3c: init must FAIL (non-zero) when the SQL store is unavailable,
        not silently skip the bootstrap and report success."""
        unavailable_sql = MagicMock()
        unavailable_sql.available = False

        with runner.isolated_filesystem():
            with patch(
                "opencrab.stores.factory.make_sql_store", return_value=unavailable_sql
            ):
                result = runner.invoke(main, ["init"])

        assert result.exit_code != 0
        assert "unavailable" in result.output.lower()

    def test_revoking_all_tokens_then_init_does_not_reissue(self, cli_env, runner):
        """Re-running init after revoking every token must not print a new
        token or create a second local user -- reissuing would silently
        undo an operator's deliberate revoke."""
        from opencrab.auth import list_tokens, list_users, revoke_token
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        with runner.isolated_filesystem():
            first = runner.invoke(main, ["init"])
            assert first.exit_code == 0

            sql = make_sql_store(get_settings())
            user_id = list_users(sql)[0]["user_id"]
            for t in list_tokens(sql, user_id):
                revoke_token(sql, t["token_id"])

            second = runner.invoke(main, ["init", "--force"])
            assert second.exit_code == 0
            assert "already bootstrapped" in second.output
            assert "Bootstrap token" not in second.output

            users = list_users(sql)
            assert len(users) == 1
            tokens_after = list_tokens(sql, user_id)
            assert len(tokens_after) == 1
            assert tokens_after[0]["revoked_at"] is not None

    def test_concurrent_init_race_lands_on_a_single_local_user(self, cli_env, runner):
        """3e: two concurrent inits both see "no local user" and both try
        to insert one. Simulated deterministically: seed a local user
        directly (the "winner"), then force this init's idempotency check
        to see None on its first read (as a real racer would) so it
        proceeds to insert and hits the real unique-index violation. cli.py
        must catch ONLY that violation and re-read (a fresh connection) to
        discover the winner's row -- not fail, not create a second one."""
        from opencrab.auth import bootstrap_local_user, get_local_user
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        with runner.isolated_filesystem():
            cfg = get_settings()
            sql = make_sql_store(cfg)
            winner_user_id, _ = bootstrap_local_user(sql)

            real_get_local_user = get_local_user
            calls = {"n": 0}

            def fake_get_local_user(s):
                calls["n"] += 1
                return None if calls["n"] == 1 else real_get_local_user(s)

            with patch("opencrab.auth.get_local_user", side_effect=fake_get_local_user):
                result = runner.invoke(main, ["init"])

        assert result.exit_code == 0
        assert "already bootstrapped" in result.output

        from opencrab.auth import list_users

        users = list_users(sql)
        assert len(users) == 1
        assert users[0]["user_id"] == winner_user_id


# ---------------------------------------------------------------------------
# user / token (#144)
# ---------------------------------------------------------------------------


class TestUserTokenCLI:
    # --- Normal ---
    def test_add_list_disable_user(self, cli_env, runner):
        add = runner.invoke(main, ["user", "add", "Alice"])
        assert add.exit_code == 0
        user_id = json.loads(add.output)["user_id"]

        listing = runner.invoke(main, ["user", "list"])
        assert listing.exit_code == 0
        assert user_id in listing.output

        disable = runner.invoke(main, ["user", "disable", user_id])
        assert disable.exit_code == 0
        assert "Disabled" in disable.output

    def test_issue_list_revoke_token(self, cli_env, runner):
        user_id = json.loads(runner.invoke(main, ["user", "add", "Bob"]).output)["user_id"]

        issued = runner.invoke(main, ["token", "issue", user_id, "--name", "cli-test"])
        assert issued.exit_code == 0
        assert "lc_" in issued.output

        listing = runner.invoke(main, ["token", "list", user_id])
        assert listing.exit_code == 0
        assert "cli-test" in listing.output
        assert "lc_" not in listing.output  # secret never listed

        from opencrab.auth import list_tokens
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        token_id = list_tokens(sql, user_id)[0]["token_id"]

        revoked = runner.invoke(main, ["token", "revoke", token_id])
        assert revoked.exit_code == 0
        assert "Revoked" in revoked.output

    def test_user_disable_enable_round_trip(self, cli_env, runner):
        user_id = json.loads(runner.invoke(main, ["user", "add", "Carol"]).output)["user_id"]

        disable = runner.invoke(main, ["user", "disable", user_id])
        assert disable.exit_code == 0

        enable = runner.invoke(main, ["user", "enable", user_id])
        assert enable.exit_code == 0
        assert "Enabled" in enable.output

        # Both surfaces, because they can drift: the store says disabled is
        # False again, and `user list` -- the only way an operator actually
        # sees it -- reports the same.
        from opencrab.auth import list_users
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        row = [u for u in list_users(sql) if u["user_id"] == user_id][0]
        assert row["disabled"] is False

        listing = runner.invoke(main, ["user", "list"])
        assert listing.exit_code == 0
        # `user list` renders a rich table (unlike `user add`, which emits
        # JSON), so assert on the row: this user is neither local nor
        # disabled, so its row must carry no "True" at all. Fails if either
        # flag column regresses.
        row_line = [ln for ln in listing.output.splitlines() if user_id in ln]
        assert len(row_line) == 1, listing.output
        assert "True" not in row_line[0], row_line[0]

    # --- Edge ---
    def test_disable_unknown_user_exits_nonzero(self, cli_env, runner):
        result = runner.invoke(main, ["user", "disable", "user_doesnotexist"])
        assert result.exit_code != 0
        assert "No such user" in result.output

    def test_enable_unknown_user_exits_nonzero(self, cli_env, runner):
        result = runner.invoke(main, ["user", "enable", "user_doesnotexist"])
        assert result.exit_code != 0
        assert "No such user" in result.output

    # --- Error: 3g CLI error surfacing for the new auth.py exceptions ---
    def test_token_issue_unknown_user_clear_error_no_traceback(self, cli_env, runner):
        result = runner.invoke(main, ["token", "issue", "user_doesnotexist"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "Could not issue token" in result.output

    def test_token_issue_disabled_user_clear_error_no_traceback(self, cli_env, runner):
        user_id = json.loads(runner.invoke(main, ["user", "add", "Dana"]).output)["user_id"]
        runner.invoke(main, ["user", "disable", user_id])

        result = runner.invoke(main, ["token", "issue", user_id])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "Could not issue token" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    # --- Normal ---
    def test_fresh_local_dir_reports_ok_for_all_stores(self, cli_env, runner):
        """Fresh LOCAL_DATA_DIR: graph/vector/docs/sql/billing are all local-
        SQLite backed and self-create on first use, so all five must report
        OK. billing_events lives in its own file (billing.db, issue #105) so
        it gets its own row alongside the other four."""
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "LOCAL MODE" in result.output
        assert "UNAVAILABLE" not in result.output
        assert "Billing" in result.output
        assert result.output.count("OK") == 5

    # --- Error ---
    def test_billing_store_unavailable_is_reported_not_swallowed(self, cli_env, runner):
        """issue #105 codex follow-up: billing.db can fail independently of
        the main SQL store now that it's a separate file (corrupt file, a
        permission problem specific to that one path). Before this fix,
        `status` never looked at the billing store at all, so it could
        report every configured store healthy while billing_events was
        completely dead. Pin that an unavailable billing store shows up."""
        from unittest.mock import MagicMock

        broken_billing_store = MagicMock()
        broken_billing_store.available = False

        with patch("opencrab.stores.factory.make_billing_sql_store", return_value=broken_billing_store):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "Billing" in result.output
        assert "UNAVAILABLE" in result.output

    def test_billing_table_creation_failure_is_reported(self, cli_env, runner):
        """Narrower than the connection-level case above: the billing store
        itself connects fine (available=True) but BillingHooks couldn't
        create billing_events (e.g. disk full, a permissions problem on
        CREATE TABLE specifically). This must be distinguishable from a
        healthy store, not silently reported as OK."""
        from unittest.mock import MagicMock

        store_that_connects_but_cant_create_tables = MagicMock()
        store_that_connects_but_cant_create_tables.available = True
        store_that_connects_but_cant_create_tables._engine.begin.side_effect = RuntimeError("disk full")

        with patch(
            "opencrab.stores.factory.make_billing_sql_store",
            return_value=store_that_connects_but_cant_create_tables,
        ):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        # Rich wraps the status cell across lines at narrow widths and
        # re-draws box-border characters on the continuation line, so check
        # each word rather than one contiguous phrase.
        assert "Billing" in result.output
        assert "UNAVAILABLE" in result.output
        assert "table" in result.output
        assert "creation failed" in result.output


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


class TestIngest:
    # --- Normal ---
    def test_ingest_directory_writes_vector_and_reports_count(
        self, bootstrapped, cli_env, runner, mock_vector_store
    ):
        (cli_env / "a.txt").write_text("hello world")
        (cli_env / "b.md").write_text("second doc")
        (cli_env / "skip.bin").write_text("ignored extension")

        result = runner.invoke(main, ["ingest", str(cli_env), "-e", ".txt,.md"])

        assert result.exit_code == 0
        assert "Ingested 2/2 files." in result.output
        assert mock_vector_store.count() == 2

    # --- Error ---
    def test_ingest_missing_path_exits_nonzero_no_traceback(self, cli_env, runner):
        result = runner.invoke(main, ["ingest", str(cli_env / "does-not-exist")])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output

    # --- Edge ---
    def test_ingest_single_file_path_is_accepted(
        self, bootstrapped, cli_env, runner, mock_vector_store
    ):
        """A single file (not a directory) is a plausible PATH argument
        (click.Path(exists=True) allows file_okay+dir_okay by default) and
        must be ingested directly rather than crash trying to iterdir() a
        file — reproduced as NotADirectoryError before the fix."""
        f = cli_env / "solo.txt"
        f.write_text("single file content")

        result = runner.invoke(main, ["ingest", str(f)])

        assert result.exit_code == 0
        assert result.exception is None
        assert "Ingested 1/1 files." in result.output
        assert mock_vector_store.count() == 1

    def test_ingest_pack_id_inferred_from_path(
        self, bootstrapped, cli_env, runner, mock_vector_store
    ):
        pack_dir = cli_env / "packs" / "demo-pack" / "stage"
        pack_dir.mkdir(parents=True)
        (pack_dir / "doc.txt").write_text("packed content")

        result = runner.invoke(main, ["ingest", str(pack_dir)])

        assert result.exit_code == 0
        assert "pack=demo-pack" in result.output


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


class TestQuery:
    # --- Normal ---
    def test_human_output_no_results_on_empty_store(
        self, cli_env, runner, mock_vector_store
    ):
        result = runner.invoke(main, ["query", "anything at all"])
        assert result.exit_code == 0
        assert "No results found." in result.output

    def test_human_output_finds_ingested_result(
        self, bootstrapped, cli_env, runner, mock_vector_store
    ):
        (cli_env / "doc.txt").write_text("the quick brown fox jumps")
        assert runner.invoke(main, ["ingest", str(cli_env / "doc.txt")]).exit_code == 0

        result = runner.invoke(main, ["query", "the quick brown fox jumps"])
        assert result.exit_code == 0
        assert "Found 1 result(s)" in result.output

    # --- Edge: three output formats / legacy shape contract ---
    def test_legacy_json_output_is_a_bare_list(
        self, cli_env, runner, mock_vector_store
    ):
        """Contract per code comment (cli.py ~496): --json-output alone must
        stay a bare JSON list (legacy shape), never wrapped in an envelope."""
        result = runner.invoke(main, ["query", "zzz no match", "--json-output"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed == []

    def test_json_envelope_shape(self, cli_env, runner, mock_vector_store):
        result = runner.invoke(main, ["query", "zzz no match", "--json-envelope"])
        assert result.exit_code == 0
        envelope = json.loads(result.output)
        assert set(envelope.keys()) == {
            "question",
            "spaces_filter",
            "pack_filter",
            "selected_packs",
            "total",
            "results",
        }
        assert envelope["total"] == 0

    def test_json_envelope_takes_priority_over_json_output(
        self, cli_env, runner, mock_vector_store
    ):
        """When both flags are given, --json-envelope wins (envelope dict,
        not a bare list) per the ``if json_output and not json_envelope``
        guard — documenting the precedence as intentional, not a bug."""
        result = runner.invoke(
            main, ["query", "zzz", "--json-output", "--json-envelope"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)
        assert "results" in parsed


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


class TestManifest:
    # --- Normal ---
    def test_human_output_renders_panel(self, runner):
        result = runner.invoke(main, ["manifest"])
        assert result.exit_code == 0
        assert "MetaOntology OS Grammar" in result.output

    def test_json_output_has_grammar_keys(self, runner):
        result = runner.invoke(main, ["manifest", "--json-output"])
        assert result.exit_code == 0
        grammar = json.loads(result.output)
        assert {"spaces", "meta_edges", "impact_categories", "rebac"} <= grammar.keys()


# ---------------------------------------------------------------------------
# packs list / show
# ---------------------------------------------------------------------------


class TestPacksListShow:
    # --- Normal ---
    def test_list_shows_registered_packs(self, cli_env, runner):
        _write_pack_manifest(
            cli_env, "demo-pack", title="Demo Pack", version="1.0.0",
            counts={"nodes": 3, "edges": 2},
        )
        result = runner.invoke(main, ["packs", "list"])
        assert result.exit_code == 0
        assert "demo-pack" in result.output
        assert "Demo Pack" in result.output

    def test_show_prints_manifest_json(self, cli_env, runner):
        _write_pack_manifest(
            cli_env, "demo-pack", title="Demo Pack", version="1.0.0",
        )
        result = runner.invoke(main, ["packs", "show", "demo-pack"])
        assert result.exit_code == 0
        info = json.loads(result.output)
        assert info["pack_id"] == "demo-pack"
        assert info["title"] == "Demo Pack"

    # --- Edge ---
    def test_list_empty_registry_reports_none_found(self, cli_env, runner):
        result = runner.invoke(main, ["packs", "list"])
        assert result.exit_code == 0
        assert "No packs under" in result.output

    # --- Error ---
    def test_show_unknown_pack_id_exits_nonzero(self, cli_env, runner):
        result = runner.invoke(main, ["packs", "show", "nonexistent-pack"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# packs backfill-pack-id
# ---------------------------------------------------------------------------


def _seed_graph(db_path: Path) -> None:
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(db_path=str(db_path))
    # inferable from source_path
    store.upsert_node("Agent", "n-inferable", {"source_path": "/data/packs/pack-a/x.md"})
    # not inferable, no --assume-pack-id given -> skipped
    store.upsert_node("Agent", "n-unresolvable", {"note": "no path hint"})
    # already has a pack_id -> must be left untouched
    store.upsert_node("Agent", "n-already-set", {"pack_id": "existing-pack"})
    store.upsert_edge(
        "Agent", "n-inferable", "owns", "Project", "p-inferable",
        {"source_path": "/data/packs/pack-a/y.md"},
    )
    store.close()


class TestPacksBackfillPackId:
    # --- Normal ---
    def test_dry_run_default_infers_but_does_not_write(self, cli_env, runner):
        db_path = cli_env / "graph.db"
        _seed_graph(db_path)

        result = runner.invoke(main, ["packs", "backfill-pack-id"])
        assert result.exit_code == 0
        summary = _parse_leading_json(result.output)
        assert summary["dry_run"] is True
        assert summary["nodes_inferred"] == 1
        assert summary["nodes_skipped"] == 1  # n-unresolvable, no assume flag
        assert summary["edges_inferred"] == 1

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id='n-inferable'"
        ).fetchone()
        assert "pack_id" not in json.loads(row[0])

    def test_apply_persists_inferred_pack_id(self, cli_env, runner):
        db_path = cli_env / "graph.db"
        _seed_graph(db_path)

        result = runner.invoke(main, ["packs", "backfill-pack-id", "--apply"])
        assert result.exit_code == 0
        summary = json.loads(result.output.strip())
        assert summary["dry_run"] is False

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id='n-inferable'"
        ).fetchone()
        assert json.loads(row[0])["pack_id"] == "pack-a"
        already_set = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id='n-already-set'"
        ).fetchone()
        assert json.loads(already_set[0])["pack_id"] == "existing-pack"

    # --- Error ---
    def test_missing_graph_db_exits_nonzero(self, cli_env, runner):
        result = runner.invoke(main, ["packs", "backfill-pack-id"])
        assert result.exit_code != 0
        assert "not found" in result.output

    # --- Edge: idempotency ---
    def test_apply_is_idempotent_on_second_run(self, cli_env, runner):
        db_path = cli_env / "graph.db"
        _seed_graph(db_path)
        runner.invoke(main, ["packs", "backfill-pack-id", "--apply"])

        result = runner.invoke(main, ["packs", "backfill-pack-id", "--apply"])
        summary = json.loads(result.output.strip())
        # Second run: everything that could be inferred already has a
        # pack_id, so nothing is (re-)inferred/assumed; only the
        # never-inferable node is skipped again.
        assert summary["nodes_inferred"] == 0
        assert summary["edges_inferred"] == 0
        assert summary["nodes_skipped"] == 1

    # --- Edge: assume-pack-id escape hatch ---
    def test_assume_pack_id_fills_unresolvable_entries(self, cli_env, runner):
        db_path = cli_env / "graph.db"
        _seed_graph(db_path)

        result = runner.invoke(
            main,
            ["packs", "backfill-pack-id", "--apply", "--assume-pack-id", "fallback-pack"],
        )
        summary = json.loads(result.output.strip())
        assert summary["nodes_assumed"] == 1
        assert summary["nodes_skipped"] == 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id='n-unresolvable'"
        ).fetchone()
        assert json.loads(row[0])["pack_id"] == "fallback-pack"

    # --- Edge: three-way dry-run/apply reconciliation (bug found + fixed) ---
    @pytest.mark.parametrize(
        "cli_args,expected_dry_run",
        [
            ([], True),  # neither flag -> default dry-run
            (["--apply"], False),  # --apply alone -> real run
            (["--dry-run"], True),  # --dry-run alone -> stays dry-run
            (["--no-dry-run"], True),  # --no-dry-run w/o --apply -> refuse, stay dry-run (safety net)
            (["--apply", "--dry-run"], True),  # contradictory -> honour --dry-run
            (["--apply", "--no-dry-run"], False),  # both explicit "do it" -> real run
        ],
    )
    def test_dry_run_apply_three_way_reconciliation(
        self, cli_env, runner, cli_args, expected_dry_run
    ):
        """Every (--apply, --dry-run/--no-dry-run) combination must resolve to
        an unambiguous dry_run value. ``--apply --no-dry-run`` — the most
        explicit "really do it" combination a user can type — used to fall
        through all three elif branches untouched and silently stay in
        dry-run with no properties written and no warning printed at all.
        Fixed by adding an explicit ``apply_changes and dry_run is False``
        branch in cli.py's packs_backfill_pack_id."""
        db_path = cli_env / "graph.db"
        _seed_graph(db_path)

        result = runner.invoke(main, ["packs", "backfill-pack-id", *cli_args])
        assert result.exit_code == 0
        summary = _parse_leading_json(result.output)
        assert summary["dry_run"] is expected_dry_run

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id='n-inferable'"
        ).fetchone()
        wrote_pack_id = "pack_id" in json.loads(row[0])
        assert wrote_pack_id is (not expected_dry_run)


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def _make_extraction_result(source_id: str):
    from opencrab.ontology.extractor import ExtractedEdge, ExtractedNode, ExtractionResult

    return ExtractionResult(
        source_id=source_id,
        nodes=[
            ExtractedNode(
                space="subject", node_type="Agent", node_id="alex_agent",
                properties={"name": "Alex"},
            ),
        ],
        edges=[
            ExtractedEdge(
                from_space="subject", from_id="alex_agent", relation="owns",
                to_space="resource", to_id="demo_project", properties={},
            ),
        ],
        errors=[],
    )


class TestExtract:
    # --- Normal ---
    def test_extract_writes_nodes_and_edges_and_reports_summary(
        self, bootstrapped, cli_env, runner
    ):
        (cli_env / "doc.md").write_text("Alex owns the demo project.")

        fake_extractor = MagicMock()
        fake_extractor.extract_from_file.side_effect = (
            lambda path: _make_extraction_result(str(path))
        )
        with patch(
            "opencrab.ontology.extractor.LLMExtractor", return_value=fake_extractor
        ):
            result = runner.invoke(
                main, ["extract", str(cli_env), "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        assert "nodes=1 edges=1 errors=0" in result.output
        assert "nodes=1 edges=1" in result.output.split("Done")[-1]

        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(db_path=str(cli_env / "graph.db"))
        assert graph.get_node("Agent", "alex_agent") is not None
        graph.close()

    def test_extract_dry_run_does_not_write(self, cli_env, runner):
        (cli_env / "doc.md").write_text("Alex owns the demo project.")

        fake_extractor = MagicMock()
        fake_extractor.extract_from_file.side_effect = (
            lambda path: _make_extraction_result(str(path))
        )
        with patch(
            "opencrab.ontology.extractor.LLMExtractor", return_value=fake_extractor
        ):
            result = runner.invoke(
                main,
                ["extract", str(cli_env), "--api-key", "test-key", "--dry-run"],
            )

        assert result.exit_code == 0
        assert "(dry-run)" in result.output

        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(db_path=str(cli_env / "graph.db"))
        assert graph.get_node("Agent", "alex_agent") is None
        graph.close()

    # --- Error ---
    def test_extract_no_api_key_clear_error_no_traceback(
        self, cli_env, runner, monkeypatch
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        (cli_env / "doc.md").write_text("content")

        result = runner.invoke(main, ["extract", str(cli_env)])

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert "ANTHROPIC_API_KEY" in result.output

    # --- Edge ---
    def test_extract_single_file_path_is_accepted(self, bootstrapped, cli_env, runner):
        f = cli_env / "solo.md"
        f.write_text("Alex owns the demo project.")

        fake_extractor = MagicMock()
        fake_extractor.extract_from_file.side_effect = (
            lambda path: _make_extraction_result(str(path))
        )
        with patch(
            "opencrab.ontology.extractor.LLMExtractor", return_value=fake_extractor
        ):
            result = runner.invoke(main, ["extract", str(f), "--api-key", "k"])

        assert result.exit_code == 0
        assert result.exception is None
        assert "nodes=1 edges=1" in result.output
