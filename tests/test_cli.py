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
    # --- Normal ---
    def test_fresh_dir_creates_env(self, runner):
        with runner.isolated_filesystem():
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert Path(".env").exists()
            assert "Created" in result.output

    # --- Edge ---
    def test_existing_env_not_clobbered_without_force(self, runner):
        with runner.isolated_filesystem():
            Path(".env").write_text("CUSTOM=1\n")
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert Path(".env").read_text() == "CUSTOM=1\n"
            assert "already exists" in result.output

    def test_force_overwrites_existing_env(self, runner):
        with runner.isolated_filesystem():
            Path(".env").write_text("CUSTOM=1\n")
            result = runner.invoke(main, ["init", "--force"])
            assert result.exit_code == 0
            assert Path(".env").read_text() != "CUSTOM=1\n"
            assert "Created" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    # --- Normal ---
    def test_fresh_local_dir_reports_ok_for_all_stores(self, cli_env, runner):
        """Fresh LOCAL_DATA_DIR: graph/vector/docs/sql are all local-SQLite
        backed and self-create on first use, so all four must report OK."""
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "LOCAL MODE" in result.output
        assert "UNAVAILABLE" not in result.output
        assert result.output.count("OK") == 4


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


class TestIngest:
    # --- Normal ---
    def test_ingest_directory_writes_vector_and_reports_count(
        self, cli_env, runner, mock_vector_store
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
        self, cli_env, runner, mock_vector_store
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
        self, cli_env, runner, mock_vector_store
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
        self, cli_env, runner, mock_vector_store
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
        self, cli_env, runner
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
    def test_extract_single_file_path_is_accepted(self, cli_env, runner):
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


# ---------------------------------------------------------------------------
# export-neo4j-pack / assemble-pack-v1 — opencrab.pack 소비 배선
# ---------------------------------------------------------------------------


class TestPackCommandWiring:
    """CLI 가 `opencrab.pack` 을 **올바른 인자로** 부르는지.

    이 두 커맨드가 `opencrab.pack` 의 유일한 프로덕션 소비자인데(실측: cli.py 2곳 외에는
    `scripts/export_pack_graph_from_neo4j.py` 뿐), 그 배선이 무검사였다.

    export 표면 자체(`from opencrab.pack import ...`)는 pack 테스트들이 덮는다. 여기서
    닫는 것은 **CLI 배선**이다 — 옵션이 어느 파라미터로 가는지, 기본값이 무엇인지.
    전체 스위트를 다 돌려도 이 줄들이 커버되지 않아 "다 통과했으니 안전하다"가
    거짓 안심이었다(2026-08-06 커버리지 실측: cli.py:710·736 미커버).
    """

    # --- Normal ---
    def test_export_neo4j_pack_passes_options_through(self, cli_env, runner):
        with patch("opencrab.pack.export_neo4j_opencrab_ingest") as fake:
            fake.return_value = {"nodes": 0, "edges": 0}
            result = runner.invoke(main, [
                "export-neo4j-pack", "-o", str(cli_env / "out.jsonl"),
                "--pack-id", "p1", "--node-limit", "7", "--edge-limit", "9",
            ])

        assert result.exit_code == 0, result.output
        _store, output = fake.call_args.args
        assert output == str(cli_env / "out.jsonl")
        assert fake.call_args.kwargs == {
            "pack_id": "p1", "node_limit": 7, "edge_limit": 9}

    def test_export_neo4j_pack_defaults(self, cli_env, runner):
        """기본값이 조용히 바뀌면 운영 산출물의 크기 상한이 달라진다."""
        with patch("opencrab.pack.export_neo4j_opencrab_ingest") as fake:
            fake.return_value = {}
            result = runner.invoke(
                main, ["export-neo4j-pack", "-o", str(cli_env / "o.jsonl")])

        assert result.exit_code == 0, result.output
        assert fake.call_args.kwargs == {
            "pack_id": None, "node_limit": 500_000, "edge_limit": 1_000_000}

    def test_assemble_pack_v1_passes_options_through(self, cli_env, runner):
        src = cli_env / "staging"
        src.mkdir()
        with patch("opencrab.pack.assemble_pack_v1") as fake:
            fake.return_value = {"pack_id": "p1"}
            result = runner.invoke(main, [
                "assemble-pack-v1", str(src),
                "-o", str(cli_env / "p.zip"), "--pack-id", "p1", "--title", "제목",
            ])

        assert result.exit_code == 0, result.output
        source_dir, output = fake.call_args.args
        assert (source_dir, output) == (str(src), str(cli_env / "p.zip"))
        assert fake.call_args.kwargs == {"pack_id": "p1", "title": "제목"}

    def test_assemble_pack_v1_title_defaults_to_none(self, cli_env, runner):
        src = cli_env / "staging2"
        src.mkdir()
        with patch("opencrab.pack.assemble_pack_v1") as fake:
            fake.return_value = {}
            result = runner.invoke(main, [
                "assemble-pack-v1", str(src), "-o", str(cli_env / "q.zip"),
                "--pack-id", "p2"])

        assert result.exit_code == 0, result.output
        assert fake.call_args.kwargs == {"pack_id": "p2", "title": None}

    # --- Error ---
    def test_assemble_pack_v1_rejects_missing_source_dir(self, cli_env, runner):
        result = runner.invoke(main, [
            "assemble-pack-v1", str(cli_env / "없는디렉터리"),
            "-o", str(cli_env / "x.zip"), "--pack-id", "p"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
