"""``pack_fork``'s interaction with the registry repair pass (#201 §4-F).

`pack_fork` reserves its destination pack as `creating` and writes the graph
anchor FIRST, well before content copying starts (design v7 §5). Two
existing lifecycle behaviours are unsound against that ordering because both
gate on the anchor probe alone, and for a fork the anchor probing PRESENT is
evidence of nothing but the FIRST write having happened:

- `repair_incomplete_packs`'s stale-`creating` auto-promote branch would
  flip an in-flight or dead fork's copy straight to `ready`, exposing an
  incomplete pack to user queries.
- `promote_partial_pack` (and `repair_incomplete_packs`'s own `--promote`
  planning branch) would let an operator manually promote a `partial` fork
  remnant for the same reason -- the anchor is there either way.

This file pins the three fixes (design v7 §4-F fix 1/2/3): a forked
`creating` row demotes on age regardless of the probe and is never
auto-promoted (while still showing up in the report -- the guard changes
which branch runs, it does not `continue` past the row); a forked `partial`
row is refused by both `promote_partial_pack` directly and by
`repair_incomplete_packs`'s `--promote` planning branch (in BOTH `apply`
modes, so the dry-run's plan and what `--apply` would actually do never
diverge); and a `pack_create`-style row (no `forked_from`) is unaffected by
any of this -- the ordinary promote/demote paths still apply to it exactly
as `tests/test_pack_repair_registry.py` already pins.

Fixture style follows ``tests/test_packs_lifecycle_registry.py`` (in-memory
SQLite ``SQLStore``, ``create_user`` for owner ids) and
``tests/test_pack_create_lifecycle.py``'s import shape. The `FakeGraph`
double and `_stale`/`_row_for` helpers mirror
``tests/test_pack_repair_registry.py`` (same probe slot contract:
`available` + `get_node(node_type, node_id)`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import create_user
from opencrab.pack.lifecycle import repair_incomplete_packs
from opencrab.pack.ownership import (
    FORKED_PARTIAL_PROMOTE_REFUSAL,
    PACK_STATUS_PARTIAL,
    PACK_STATUS_READY,
    anchor_node_id,
    begin_pack_creation,
    get_pack,
    mark_pack_partial,
    promote_partial_pack,
)


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


def _set_updated_at(sql, pack_id: str, value) -> None:
    with sql._engine.begin() as conn:
        conn.execute(
            _sql_text("UPDATE packs SET updated_at = :v WHERE pack_id = :pid"),
            {"v": value, "pid": pack_id},
        )


def _stale(sql, pack_id: str, seconds: int = 7200) -> None:
    """Back-date ``pack_id``'s ``updated_at`` well past any threshold used
    below, in SQLite's own naive ``datetime('now')`` text shape -- same
    helper as ``tests/test_pack_repair_registry.py``'s."""
    dt = datetime.now(UTC) - timedelta(seconds=seconds)
    _set_updated_at(sql, pack_id, dt.strftime("%Y-%m-%d %H:%M:%S"))


def _row_for(result: dict, pack_id: str) -> dict:
    return next(r for r in result["rows"] if r["pack_id"] == pack_id)


class FakeGraph:
    """Minimal ``get_node`` double -- same slot contract as
    ``tests/test_pack_repair_registry.py``'s ``FakeGraph``. ``present_ids``
    are node ids (``anchor_node_id(pack_id)`` results) that answer PRESENT;
    everything else answers absent (``None``)."""

    available = True

    def __init__(self, *, present_ids=()):
        self._present = set(present_ids)

    def get_node(self, node_type, node_id):  # noqa: ARG002
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


# ---------------------------------------------------------------------------
# Fix 1: a stale, forked `creating` row demotes regardless of the anchor
# probe, never auto-promotes, and still appears in the report.
# ---------------------------------------------------------------------------


class TestForkedCreatingDemotesRegardlessOfProbe:
    def test_aged_forked_creating_with_present_anchor_is_demoted_not_promoted(
        self, sql, alice
    ):
        """The dead-fork recovery case: `pack_fork` landed its anchor, then
        died before copying content. The anchor probes PRESENT -- exactly
        the condition that promotes an ordinary (non-forked) `pack_create`
        row -- but this row must be demoted instead, because for a fork the
        anchor is only the first write, not completion evidence."""
        pid = begin_pack_creation(sql, alice, "fork-dead", forked_from="src-pack")
        _stale(sql, pid)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "demote"
        assert row["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_aged_forked_creating_still_appears_in_the_report(self, sql, alice):
        """Reverse-mutation guard for the 'change WHICH branch runs, do not
        `continue` past the row' constraint: if the guard were implemented
        as a `continue`, the row would vanish from `result["rows"]` and
        `counts["rows_examined"]` would still count it while `results` did
        not reflect it -- this test dies under that mutant even though the
        status-transition test above would not distinguish the two
        implementations by itself."""
        pid = begin_pack_creation(sql, alice, "fork-dead-2", forked_from="src-pack")
        _stale(sql, pid)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        pack_ids_in_report = {r["pack_id"] for r in result["rows"]}
        assert pid in pack_ids_in_report
        assert result["counts"]["rows_examined"] >= 1
        assert result["counts"]["demoted"] >= 1

    def test_aged_plain_creating_without_forked_from_still_promotes(self, sql, alice):
        """Control / no-regression: a `pack_create`-style row (no
        `forked_from`) with a present anchor must still auto-promote exactly
        as before -- this guard is scoped to forked rows only, not a change
        to `pack_create`'s own recovery path."""
        pid = begin_pack_creation(sql, alice, "plain-create")
        _stale(sql, pid)
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(sql, graph, FakeDocs(), FakeVector(), apply=True)

        row = _row_for(result, pid)
        assert row["action"] == "promote"
        assert row["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# Fix 2: `promote_partial_pack` refuses a forked `partial` row outright.
# ---------------------------------------------------------------------------


class TestPromotePartialPackRefusesForkedRows:
    def test_refuses_a_forked_partial_row_and_names_the_reason(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "fork-partial", forked_from="src-pack")
        assert mark_pack_partial(sql, pid, alice) is True

        with pytest.raises(ValueError) as excinfo:
            promote_partial_pack(sql, pid, alice)

        assert "forked" in str(excinfo.value).lower()
        assert str(excinfo.value) == FORKED_PARTIAL_PROMOTE_REFUSAL
        # Refused, not silently no-op'd: the row must not have moved.
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_control_ordinary_partial_row_without_forked_from_still_promotes(
        self, sql, alice
    ):
        """CONTROL for fix 2: an ordinary `partial` row (no `forked_from`,
        e.g. a `pack_create` attempt that never landed) must still be
        promotable exactly as before -- the new guard must not over-reach
        onto non-forked rows."""
        pid = begin_pack_creation(sql, alice, "plain-partial")
        assert mark_pack_partial(sql, pid, alice) is True

        assert promote_partial_pack(sql, pid, alice) is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# Fix 3: the SAME refusal appears in `repair_incomplete_packs`'s `--promote`
# planning branch, in BOTH dry-run and apply mode -- the dry-run/apply
# invariant that module documents.
# ---------------------------------------------------------------------------


class TestRepairPromotePlanningRefusesForkedRows:
    def test_dry_run_does_not_plan_promote_for_a_forked_partial_row(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "fork-partial-dry", forked_from="src-pack")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=False, promote=pid
        )

        assert result["promote_result"]["action"] != "promote"
        assert result["promote_result"]["reason"] == FORKED_PARTIAL_PROMOTE_REFUSAL
        # Row must be untouched -- this is a dry run.
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_apply_mode_also_refuses_and_never_calls_promote_partial_pack(
        self, sql, alice
    ):
        """The dry-run/apply invariant this module documents ("the plan a
        dry-run prints has to be the operation an --apply would perform"):
        apply mode must reach the same rejection at planning time, not fall
        through to `promote_partial_pack` and get an `applied: false` (or a
        raised `ValueError` bubbling out of a plan that claimed "promote")."""
        pid = begin_pack_creation(sql, alice, "fork-partial-apply", forked_from="src-pack")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        result = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid
        )

        assert result["promote_result"]["action"] != "promote"
        assert result["promote_result"]["reason"] == FORKED_PARTIAL_PROMOTE_REFUSAL
        assert "applied" not in result["promote_result"]
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_control_ordinary_partial_row_still_plans_and_applies_promote(
        self, sql, alice
    ):
        """CONTROL for fix 3: an ordinary (non-forked) `partial` row must
        still plan and apply `promote` through the same code path -- the new
        `elif` must not shadow the existing anchor-probe rejection or the
        happy path for rows that were never forked."""
        pid = begin_pack_creation(sql, alice, "plain-partial-promote")
        assert mark_pack_partial(sql, pid, alice) is True
        graph = FakeGraph(present_ids={anchor_node_id(pid)})

        dry = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=False, promote=pid
        )
        assert dry["promote_result"]["action"] == "promote"
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

        wet = repair_incomplete_packs(
            sql, graph, FakeDocs(), FakeVector(), apply=True, promote=pid
        )
        assert wet["promote_result"]["action"] == "promote"
        assert wet["promote_result"]["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY
