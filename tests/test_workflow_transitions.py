"""
Stage 5 (B3) unified-behavior contract tests -- EXPECTED RED until the
E-agent adds a VALID_TRANSITIONS map to opencrab.execution.workflow and
enforces it in WorkflowEngine.advance(). Do not xfail these: today
``advance()`` accepts ANY status -> status move (including e.g.
completed -> pending) as long as the target is one of VALID_STATUSES; a
genuine failure here is the proof.

Target transition graph (derived from opencrab/execution/workflow.py's
existing VALID_STATUSES = {pending, running, approved, rejected, completed,
failed} -- no other status vocabulary exists in this codebase, so no state
was invented):

    pending   -> running, approved, rejected
    approved  -> running, rejected
    running   -> completed, failed
    completed -> (terminal)
    failed    -> (terminal)
    rejected  -> (terminal)

Rationale: `create_run` always starts a run at 'pending'. 'approved' /
'rejected' exist as first-class statuses (not only as `approvals.py`'s
separate decision table) so a run can pass through an optional approval
gate before executing (pending -> approved -> running) or be rejected
before ever running (pending -> rejected). Once a run starts executing
(running) it can only resolve to completed or failed. All three of
completed/failed/rejected are terminal: this workflow has no "reopen" or
"retry" verb today, so no edge leaves them.

Same-status policy: advancing a run to its OWN current status is REJECTED
(not a no-op). "Transition" implies an actual state change; a caller that
wants idempotent retries should catch and ignore the error rather than have
it silently swallowed.
"""

from __future__ import annotations

import os

import pytest

from opencrab.execution.workflow import VALID_STATUSES, WorkflowEngine
from opencrab.stores.sql_store import SQLStore

# The target graph this test file pins. workflow.py is expected to grow a
# same-named VALID_TRANSITIONS constant during the E3 leg (see
# test_valid_transitions_constant_matches_expected below) -- until then this
# local copy is what every test in this file checks WorkflowEngine.advance
# against.
EXPECTED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "approved", "rejected"}),
    "approved": frozenset({"running", "rejected"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "rejected": frozenset(),
}

assert set(EXPECTED_TRANSITIONS) == VALID_STATUSES, (
    "EXPECTED_TRANSITIONS must cover exactly VALID_STATUSES -- update this "
    "test file if the status vocabulary changes"
)

LEGAL_EDGES = [
    (src, dst) for src, dsts in EXPECTED_TRANSITIONS.items() for dst in dsts
]
ILLEGAL_EDGES = [
    (src, dst)
    for src in VALID_STATUSES
    for dst in VALID_STATUSES
    if dst not in EXPECTED_TRANSITIONS[src]
]


@pytest.fixture
def sql_store(tmp_path):
    db_path = tmp_path / "workflow.db"
    store = SQLStore(f"sqlite:///{db_path}")
    assert store.available
    yield store


@pytest.fixture
def engine(sql_store):
    return WorkflowEngine(sql_store)


def _advance_to(engine: WorkflowEngine, run_id: str, target: str) -> None:
    """Walk EXPECTED_TRANSITIONS from 'pending' to *target* via legal edges
    (BFS), advancing the real run through each intermediate status."""
    if target == "pending":
        return
    from collections import deque

    parent: dict[str, str | None] = {"pending": None}
    queue = deque(["pending"])
    while queue:
        node = queue.popleft()
        if node == target:
            break
        for nxt in EXPECTED_TRANSITIONS[node]:
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    assert target in parent, f"{target!r} is unreachable from 'pending' in EXPECTED_TRANSITIONS"

    path = [target]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    path.reverse()  # ['pending', ..., target]

    for status in path[1:]:
        engine.advance(run_id, status)


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestWorkflowTransitionsNormal:
    def test_legal_chain_pending_running_completed(self, engine):
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]

        engine.advance(run_id, "running")
        assert engine.get_run(run_id)["status"] == "running"

        engine.advance(run_id, "completed")
        assert engine.get_run(run_id)["status"] == "completed"

    @pytest.mark.parametrize("src,dst", LEGAL_EDGES)
    def test_every_legal_edge_is_accepted(self, engine, src, dst):
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        _advance_to(engine, run_id, src)
        assert engine.get_run(run_id)["status"] == src

        result = engine.advance(run_id, dst)

        assert result["status"] == dst
        assert engine.get_run(run_id)["status"] == dst


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestWorkflowTransitionsError:
    def test_completed_to_pending_raises(self, engine):
        """RED today: advance() has no transition rules, so this currently
        succeeds and silently reopens a finished run."""
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        _advance_to(engine, run_id, "completed")

        with pytest.raises(ValueError):
            engine.advance(run_id, "pending")

        assert engine.get_run(run_id)["status"] == "completed"

    @pytest.mark.parametrize("src,dst", ILLEGAL_EDGES)
    def test_every_illegal_edge_is_rejected(self, engine, src, dst):
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        _advance_to(engine, run_id, src)

        with pytest.raises(ValueError):
            engine.advance(run_id, dst)

        # rejected transition must not mutate state
        assert engine.get_run(run_id)["status"] == src

    def test_unknown_status_string_still_raises_invalid_status(self, engine):
        """Must not regress: an unrecognised status string is rejected
        before transition-legality is even considered."""
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        with pytest.raises(ValueError, match="Invalid status"):
            engine.advance(run_id, "not_a_real_status")
        assert engine.get_run(run_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestWorkflowTransitionsEdge:
    @pytest.mark.parametrize("status", sorted(VALID_STATUSES))
    def test_same_status_transition_is_rejected(self, engine, status):
        """RED today: advance() allows a no-op re-advance to the current
        status for every status (test_execution_workflow.py's
        test_advance_accepts_every_valid_status pins the OLD behavior and
        will need updating alongside this). Same-status is policy-decided
        here as a rejection, not a no-op: 'advance' implies an actual
        change."""
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        _advance_to(engine, run_id, status)

        with pytest.raises(ValueError):
            engine.advance(run_id, status)

        assert engine.get_run(run_id)["status"] == status

    def test_valid_transitions_constant_matches_expected(self):
        """The E3 leg is expected to add a VALID_TRANSITIONS constant to
        opencrab.execution.workflow matching EXPECTED_TRANSITIONS above.
        RED today: the constant does not exist yet."""
        import opencrab.execution.workflow as workflow_module

        assert hasattr(workflow_module, "VALID_TRANSITIONS"), (
            "opencrab.execution.workflow must define VALID_TRANSITIONS"
        )
        actual = {k: frozenset(v) for k, v in workflow_module.VALID_TRANSITIONS.items()}
        assert actual == EXPECTED_TRANSITIONS


@pytest.fixture
def pg_engine_dsn():
    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL not set -- PG workflow contract test skipped")
    return dsn


class TestWorkflowTransitionsPg:
    """One PG smoke test for the same B3 contract -- WorkflowEngine's SQL is
    dialect-branched internally (see workflow.py's _is_sqlite checks), so a
    passing sqlite run does not guarantee PG parity."""

    def test_legal_chain_pending_running_completed(self, pg_engine_dsn, tmp_path):
        store = SQLStore(pg_engine_dsn)
        if not store.available:
            pytest.skip(f"PG test DB unreachable: {pg_engine_dsn!r}")
        engine = WorkflowEngine(store)
        created = engine.create_run("add_node", {})
        run_id = created["run_id"]
        try:
            engine.advance(run_id, "running")
            engine.advance(run_id, "completed")
            assert engine.get_run(run_id)["status"] == "completed"

            with pytest.raises(ValueError):
                engine.advance(run_id, "pending")
        finally:
            from sqlalchemy import text

            with store._engine.begin() as conn:
                conn.execute(text("DELETE FROM action_log WHERE run_id = :r"), {"r": run_id})
                conn.execute(text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": run_id})
