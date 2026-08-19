"""``opencrab.pack.lifecycle.repair_incomplete_packs`` + the
``packs repair-registry`` CLI (#170, execution 5b of #143, design v4 §3.6).

This is the offline repair pass over ``packs`` rows that never reached
``ready``. The single invariant this whole file exists to pin (design v4
§3.0, §5 test 9(k)): **no branch of this function, under any argument
combination, ever deletes a registry row.** Every scenario below ends in
either ``ready``, ``partial``, or the row's status unchanged -- never gone.

Fixture style follows ``tests/test_packs_lifecycle_registry.py``: a real
in-memory SQLite ``SQLStore``, ``create_user`` for owner ids, and small
in-process store doubles for graph/docs/vector honouring the same slot
contract ``probe_anchor`` calls (``graph.get_node``, ``docs.get_node_doc``,
``vector.get_by_id``, each guarded by an ``available`` attribute).

``updated_at`` staleness is engineered directly with a raw ``UPDATE``
(``_set_updated_at``/``_stale``) rather than by sleeping in real time --
this file needs SECONDS-old and YEARS-old rows deterministically, not
whatever wall-clock drift a sleep would buy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as _sql_text

import opencrab.pack.ownership as ownership_mod
from opencrab.auth import create_user
from opencrab.pack.lifecycle import repair_incomplete_packs
from opencrab.pack.ownership import (
    PACK_STATUS_CREATING,
    PACK_STATUS_PARTIAL,
    PACK_STATUS_READY,
    anchor_node_id,
    begin_pack_creation,
    get_pack,
    mark_pack_partial,
    mark_pack_ready,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


def _row_count(sql) -> int:
    with sql._engine.connect() as conn:
        return conn.execute(_sql_text("SELECT count(*) FROM packs")).scalar_one()


def _set_updated_at(sql, pack_id: str, value) -> None:
    with sql._engine.begin() as conn:
        conn.execute(
            _sql_text("UPDATE packs SET updated_at = :v WHERE pack_id = :pid"),
            {"v": value, "pid": pack_id},
        )


def _stale(sql, pack_id: str, seconds: int = 7200) -> None:
    """Back-date ``pack_id``'s ``updated_at`` well past any threshold used
    below, in SQLite's own naive ``datetime('now')`` text shape."""
    dt = datetime.now(UTC) - timedelta(seconds=seconds)
    _set_updated_at(sql, pack_id, dt.strftime("%Y-%m-%d %H:%M:%S"))


class FakeGraph:
    """Minimal ``get_node`` double. ``present_ids``/``raise_ids`` are node
    ids (i.e. ``anchor_node_id(pack_id)`` results) -- present ones answer
    with ``{"pack_id": <the pack that owns that anchor id>}`` (inverting the
    ``dataset:{pack_id}`` convention), everything else answers ``None``
    (absent), and anything in ``raise_ids`` raises (probe-exception path)."""

    def __init__(self, *, available: bool = True, present_ids=(), raise_ids=()):
        self.available = available
        self._present = set(present_ids)
        self._raise = set(raise_ids)
        self.calls: list[tuple[str, str]] = []

    def get_node(self, node_type, node_id):
        self.calls.append((node_type, node_id))
        if node_id in self._raise:
            raise RuntimeError("simulated graph store failure")
        if node_id in self._present:
            assert node_id.startswith("dataset:")
            return {"pack_id": node_id[len("dataset:") :]}
        return None


class FakeDocs:
    available = True

    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return None


class FakeVector:
    available = True

    def get_by_id(self, node_id):  # noqa: ARG002
        return None


def _row_for(result: dict, pack_id: str) -> dict:
    return next(r for r in result["rows"] if r["pack_id"] == pack_id)


# ---------------------------------------------------------------------------
# 1. _parse_updated_at (direct, both timestamp shapes) -- underpins 9(f).
# ---------------------------------------------------------------------------


class TestParseUpdatedAt:
    def test_sqlite_naive_shape(self):
        from opencrab.pack.lifecycle import _parse_updated_at

        dt = _parse_updated_at("2026-08-19 04:05:06")
        assert dt is not None
        assert dt.tzinfo is not None  # naive input is assumed UTC
        assert dt.utcoffset() == timedelta(0)

    def test_pg_offset_shape(self):
        from opencrab.pack.lifecycle import _parse_updated_at

        dt = _parse_updated_at("2026-08-19 04:05:06.123456+00:00")
        assert dt is not None
        assert dt.utcoffset() == timedelta(0)

    def test_none_and_garbage(self):
        from opencrab.pack.lifecycle import _parse_updated_at

        assert _parse_updated_at(None) is None
        assert _parse_updated_at("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# 9(a). stale creating + graph anchor present -> promote.
# ---------------------------------------------------------------------------


class TestPromoteStaleCreating:
    def test_dry_run_reports_promote_without_changing_status(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promote-a")
        _stale(sql, pid)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=False)

        row = _row_for(result, pid)
        assert row["action"] == "promote"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

    def test_apply_promotes_to_ready(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promote-b")
        _stale(sql, pid)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "promote"
        assert row["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# 9(b). stale creating + graph anchor positively absent -> demote.
# ---------------------------------------------------------------------------


class TestDemoteStaleCreating:
    def test_apply_demotes_to_partial(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "demote-a")
        _stale(sql, pid)
        graph = FakeGraph(present_ids=set())

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "demote"
        assert row["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# 9(c). graph unavailable, or the probe itself raises -> skipped (unverifiable).
# ---------------------------------------------------------------------------


class TestUnverifiableSkip:
    def test_graph_unavailable_skips_and_leaves_status_untouched(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "unverif-a")
        _stale(sql, pid)
        graph = FakeGraph(available=False)

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "skipped (unverifiable)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

    def test_probe_exception_skips_and_leaves_status_untouched(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "unverif-b")
        _stale(sql, pid)
        graph = FakeGraph(raise_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "skipped (unverifiable)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING


# ---------------------------------------------------------------------------
# 9(d). row within the age threshold -> no effect.
# ---------------------------------------------------------------------------


def test_row_within_age_threshold_is_left_alone(sql, alice):
    pid = begin_pack_creation(sql, alice, "fresh-a")  # updated_at is "now"
    graph = FakeGraph(present_ids={anchor_node_id(pid)})

    result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

    row = _row_for(result, pid)
    assert row["action"] == "skipped (too recent)"
    assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING


# ---------------------------------------------------------------------------
# 9(e). updated_at NULL / unparseable / future -> skipped (unknown age).
# ---------------------------------------------------------------------------


class TestUnknownAgeSkip:
    def test_null_updated_at(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "unk-a")
        _set_updated_at(sql, pid, None)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "skipped (unknown age)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

    def test_unparseable_updated_at(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "unk-b")
        _set_updated_at(sql, pid, "not-a-timestamp")
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "skipped (unknown age)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

    def test_future_updated_at(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "unk-c")
        future = datetime.now(UTC) + timedelta(days=1)
        _set_updated_at(sql, pid, future.strftime("%Y-%m-%d %H:%M:%S"))
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "skipped (unknown age)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING


# ---------------------------------------------------------------------------
# 9(f). Both updated_at wire shapes (SQLite naive, PG offset) parse and act.
# ---------------------------------------------------------------------------


class TestTimestampShapesEndToEnd:
    def test_sqlite_naive_shape_is_acted_on(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "shape-a")
        _set_updated_at(sql, pid, "2000-01-01 00:00:00")
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        assert _row_for(result, pid)["action"] == "promote"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY

    def test_pg_offset_shape_is_acted_on(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "shape-b")
        _set_updated_at(sql, pid, "2000-01-01 00:00:00.123456+00:00")
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        assert _row_for(result, pid)["action"] == "promote"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# 9(g). partial rows get NO automatic action, even with a positive probe.
# ---------------------------------------------------------------------------


def test_partial_row_gets_no_automatic_remediation(sql, alice):
    pid = begin_pack_creation(sql, alice, "partial-a")
    assert mark_pack_partial(sql, pid, alice) is True
    _stale(sql, pid)
    graph = FakeGraph(present_ids={anchor_node_id(pid)})  # anchor DOES exist

    result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

    row = _row_for(result, pid)
    assert row["action"] == "report only (no automatic remediation)"
    assert row["probes"]["graph"] == "present"
    assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# 9(h)/(i). --promote: rejects without a confirmed anchor; no-ops without
# --apply; irrespective of the age threshold.
# ---------------------------------------------------------------------------


class TestPromoteFlag:
    def test_rejects_when_anchor_probes_absent(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promoteflag-a")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids=set())

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid
        )

        assert result["promote_result"]["action"] == "rejected (graph anchor not confirmed present)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_rejects_when_anchor_is_unverifiable(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promoteflag-b")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(available=False)

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid
        )

        assert result["promote_result"]["action"] == "rejected (graph anchor not confirmed present)"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_rejects_a_pack_id_that_does_not_exist(self, sql, alice):
        graph = FakeGraph(present_ids=set())

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote="no-such-pack"
        )

        assert result["promote_result"]["action"] == "rejected (no such pack)"

    def test_apply_false_makes_no_change(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promoteflag-c")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=False, promote=pid
        )

        assert result["promote_result"]["action"] == "promote"
        assert "applied" not in result["promote_result"]
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_apply_true_promotes_to_ready(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "promoteflag-d")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid
        )

        assert result["promote_result"]["action"] == "promote"
        assert result["promote_result"]["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY

    def test_ignores_the_age_threshold(self, sql, alice):
        """design v4 §3.6: "이 인자는 나이 임계와 무관하게 동작한다" -- a
        freshly-partial row, well inside a very large threshold, is still
        eligible for --promote."""
        pid = begin_pack_creation(sql, alice, "promoteflag-e")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(
            sql,
            graph,
            FakeDocs(),
            FakeVector(),
            apply=True,
            promote=pid,
            older_than_seconds=999_999,
        )

        assert result["promote_result"]["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# 9(j). apply=False leaves every row (not just --promote's target) unchanged.
# ---------------------------------------------------------------------------


def test_apply_false_changes_nothing_across_the_whole_pass(sql, alice):
    pid_a = begin_pack_creation(sql, alice, "noapply-a")
    _stale(sql, pid_a)
    pid_b = begin_pack_creation(sql, alice, "noapply-b")
    _stale(sql, pid_b)
    graph = FakeGraph(present_ids={anchor_node_id(pid_a)})  # a: promote, b: demote

    result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=False)

    assert get_pack(sql, pid_a)["status"] == PACK_STATUS_CREATING
    assert get_pack(sql, pid_b)["status"] == PACK_STATUS_CREATING
    actions = {r["pack_id"]: r["action"] for r in result["rows"]}
    assert actions[pid_a] == "promote"
    assert actions[pid_b] == "demote"


# ---------------------------------------------------------------------------
# 9(k). THE core invariant: no branch, in any scenario, ever deletes a row.
# ---------------------------------------------------------------------------


def test_no_scenario_ever_deletes_a_registry_row(sql, alice):
    pid_promote = begin_pack_creation(sql, alice, "inv-promote")
    _stale(sql, pid_promote)
    pid_demote = begin_pack_creation(sql, alice, "inv-demote")
    _stale(sql, pid_demote)
    pid_unverif = begin_pack_creation(sql, alice, "inv-unverif")
    _stale(sql, pid_unverif)
    pid_recent = begin_pack_creation(sql, alice, "inv-recent")  # left fresh
    pid_unknown_age = begin_pack_creation(sql, alice, "inv-unknown")
    _set_updated_at(sql, pid_unknown_age, None)
    pid_partial = begin_pack_creation(sql, alice, "inv-partial")
    assert mark_pack_partial(sql, pid_partial, alice) is True
    _stale(sql, pid_partial)
    pid_promote_flag = begin_pack_creation(sql, alice, "inv-promoteflag")
    assert mark_pack_partial(sql, pid_promote_flag, alice) is True
    pid_reject_flag = begin_pack_creation(sql, alice, "inv-rejectflag")
    assert mark_pack_partial(sql, pid_reject_flag, alice) is True

    all_pack_ids = [
        pid_promote,
        pid_demote,
        pid_unverif,
        pid_recent,
        pid_unknown_age,
        pid_partial,
        pid_promote_flag,
        pid_reject_flag,
    ]
    before = _row_count(sql)

    graph = FakeGraph(
        present_ids={anchor_node_id(pid_promote), anchor_node_id(pid_promote_flag)},
        raise_ids={anchor_node_id(pid_unverif)},
    )
    result = repair_incomplete_packs(
        sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid_promote_flag
    )

    after = _row_count(sql)
    assert after == before, "repair_incomplete_packs must never change the row count"
    for pid in all_pack_ids:
        assert get_pack(sql, pid) is not None, f"{pid} disappeared"

    # Spot-check a few outcomes landed the way this scenario set them up to,
    # so the row-count equality above isn't hiding "nothing happened at all".
    assert get_pack(sql, pid_promote)["status"] == PACK_STATUS_READY
    assert get_pack(sql, pid_demote)["status"] == PACK_STATUS_PARTIAL
    assert get_pack(sql, pid_unverif)["status"] == PACK_STATUS_CREATING
    assert get_pack(sql, pid_recent)["status"] == PACK_STATUS_CREATING
    assert get_pack(sql, pid_unknown_age)["status"] == PACK_STATUS_CREATING
    assert get_pack(sql, pid_partial)["status"] == PACK_STATUS_PARTIAL
    assert get_pack(sql, pid_promote_flag)["status"] == PACK_STATUS_READY
    assert result["promote_result"]["pack_id"] == pid_promote_flag


# ---------------------------------------------------------------------------
# 9b. Deterministic interleaving against a concurrent pack_create.
# ---------------------------------------------------------------------------


class TestDeterministicInterleaving:
    def test_late_concurrent_promote_beats_repairs_own_demote(self, sql, alice, monkeypatch):
        """Order: repair reads status='creating' and probes the anchor as
        absent (so it decides "demote"), but a concurrent pack_create's own
        ``mark_pack_ready`` commits BEFORE repair's ``mark_pack_partial``
        UPDATE runs -- simulated by making the patched
        ``mark_pack_partial`` perform that racing write first, then call
        through to the real one. ``mark_pack_partial``'s WHERE pins
        status='creating', so by the time repair's own UPDATE executes the
        row is already 'ready' and the UPDATE matches 0 rows: (i) the row
        never disappears, (ii) it stays owned by the same pack_id/owner
        (nobody else's write ever touches it), and (iii) the final state is
        whichever write actually landed first, not corrupted by the loser.
        """
        pid = begin_pack_creation(sql, alice, "race-a")
        _stale(sql, pid)

        real_mark_pack_partial = ownership_mod.mark_pack_partial

        def racing_mark_pack_partial(sql_, pid_, owner_id_):
            assert ownership_mod.mark_pack_ready(sql_, pid_, owner_id_) is True
            return real_mark_pack_partial(sql_, pid_, owner_id_)

        monkeypatch.setattr(ownership_mod, "mark_pack_partial", racing_mark_pack_partial)

        graph = FakeGraph(present_ids=set())  # repair's own probe reads "absent"
        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "demote"
        assert row["applied"] is False  # repair's own UPDATE matched 0 rows

        final = get_pack(sql, pid)
        assert final is not None  # (i) row never disappeared
        assert final["owner_id"] == alice  # (ii) slug/ownership never moved
        assert final["status"] == PACK_STATUS_READY  # (iii) winner's state stands

    def test_repairs_own_demote_beats_a_late_concurrent_promote(self, sql, alice):
        """Reverse order: repair's demote commits first. A "concurrent"
        pack_create success arriving afterward tries ``mark_pack_ready`` and
        loses -- its WHERE pins status='creating', which is no longer true.
        """
        pid = begin_pack_creation(sql, alice, "race-b")
        _stale(sql, pid)
        graph = FakeGraph(present_ids=set())

        repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

        assert mark_pack_ready(sql, pid, alice) is False  # matched 0 rows

        final = get_pack(sql, pid)
        assert final is not None  # (i)
        assert final["owner_id"] == alice  # (ii)
        assert final["status"] == PACK_STATUS_PARTIAL  # (iii) not silently flipped


# ---------------------------------------------------------------------------
# CLI: opencrab packs repair-registry (dry-run default, --apply required).
# ---------------------------------------------------------------------------


def _parse_leading_json(output: str) -> dict:
    brace = output.index("{")
    return json.JSONDecoder().raw_decode(output[brace:])[0]


class TestCLIRepairRegistry:
    @pytest.fixture()
    def cli_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STORAGE_MODE", "local")
        from opencrab.config import get_settings

        get_settings.cache_clear()
        yield tmp_path
        get_settings.cache_clear()

    @pytest.fixture()
    def runner(self):
        from click.testing import CliRunner

        return CliRunner()

    @pytest.fixture()
    def mock_vector_store(self, tmp_path):
        """Same rationale as tests/test_cli.py's fixture of the same name:
        never let a CLI test load the real KURE embedding chain."""
        from unittest.mock import patch

        from _vec_helpers import build_vector_store

        store = build_vector_store("sqlite-vec", tmp_path, dim=32)
        with patch("opencrab.stores.factory.make_vector_store", return_value=store):
            yield store

    def test_dry_run_by_default_prints_json_and_makes_no_change(
        self, cli_env, runner, mock_vector_store
    ):
        from opencrab.cli import main
        from opencrab.config import get_settings
        from opencrab.pack.ownership import begin_pack_creation, get_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        pid = begin_pack_creation(sql, "someone", "cli-untouched")

        result = runner.invoke(main, ["packs", "repair-registry", "--older-than", "0"])

        assert result.exit_code == 0, result.output
        payload = _parse_leading_json(result.output)
        assert payload["apply"] is False
        assert "Dry-run only" in result.output
        assert get_pack(sql, pid)["status"] == "creating"
