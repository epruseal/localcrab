"""pack_create's two-phase registry lifecycle (#170, design v4 §3.5).

pack_create now reserves its registry row status='creating' BEFORE writing
any content, and every failure past the first content-writer call
(``builder.add_node``) ends in a ``partial`` demotion -- never a delete. The
only delete left anywhere in this function is the anchor-identity-conflict
branch, which runs BEFORE ``builder.add_node`` is ever called (see
``opencrab/pack/ownership.py``'s ``delete_pack_row`` docstring and
``opencrab/pack/lifecycle.py``'s module docstring for the "why" this file
pins the "what" of).

Conventions follow tests/test_packs_registry.py's MCP-tool-level section: a
real in-memory SQLite ``SQLStore`` wired into ``ctx["sql"]`` (
``mark_pack_ready``/``mark_pack_partial``/``delete_pack_row`` all do
``result.rowcount > 0`` internally, which a bare ``MagicMock`` cannot
satisfy -- it raises ``TypeError`` on the comparison), and
``opencrab.mcp.tools._get_context`` patched at the PACKAGE level (see
``opencrab/mcp/tools/pack.py``'s module docstring for why a submodule-level
patch would not be observed). Principals are literal ``Principal(user_id=...)``
values, same as ``test_packs_registry.py``'s ``TestPackCreateCollision``/
``TestPackPublish`` sections -- SQLite's FK enforcement is off by default, so
``packs.owner_id`` does not need a matching real ``users`` row for these
tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.pack.ownership import (
    PACK_STATUS_PARTIAL,
    PACK_STATUS_READY,
    begin_pack_creation,
    delete_pack_row,
    get_pack,
)
from opencrab.pack.read_scope import assert_registry_covers_graph


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


def _base_ctx(sql, **overrides):
    """Same shape as tests/test_packs_registry.py's ``_base_ctx`` -- see
    there for why each of these defaults is what it is."""
    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    ctx = {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": sql,
        "builder": builder,
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    ctx["neo4j"].get_node.return_value = None
    ctx["neo4j"].get_node_by_id.return_value = None
    ctx["neo4j"].get_nodes_by_id.return_value = []
    ctx["neo4j"].get_edge.return_value = None
    ctx["neo4j"].lookup_node_type.return_value = "Entity"
    ctx["mongo"].get_node_doc.return_value = None
    ctx["mongo"].get_source.return_value = None
    ctx["chroma"].get_by_id.return_value = None
    ctx.update(overrides)
    return ctx


def _create(ctx, **kwargs):
    from opencrab.mcp.tools import pack_create

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
            return pack_create(**kwargs)


def _steal_owner(sql_, pack_id, owner_id):
    """A ``mark_pack_ready`` stand-in simulating another process changing
    the row's owner between ``begin_pack_creation`` and the finalize check --
    then reporting False, same as the real function would for a row it no
    longer matches."""
    from sqlalchemy import text

    with sql_._engine.begin() as conn:
        conn.execute(
            text("UPDATE packs SET owner_id = :o WHERE pack_id = :p"),
            {"o": "mallory", "p": pack_id},
        )
    return False


def _demote_underneath(sql_, pack_id, owner_id):
    """A ``mark_pack_ready`` stand-in simulating a concurrent repair-pass
    demotion racing in first."""
    from sqlalchemy import text

    with sql_._engine.begin() as conn:
        conn.execute(
            text("UPDATE packs SET status = 'partial' WHERE pack_id = :p"),
            {"p": pack_id},
        )
    return False


def _promote_underneath(sql_, pack_id, owner_id):
    """A ``mark_pack_ready`` stand-in simulating a concurrent repair-pass
    (or operator ``--promote``) already promoting the row first."""
    from sqlalchemy import text

    with sql_._engine.begin() as conn:
        conn.execute(
            text("UPDATE packs SET status = 'ready' WHERE pack_id = :p"),
            {"p": pack_id},
        )
    return False


def _vanish_row(sql_, pack_id, owner_id):
    """A ``mark_pack_ready`` stand-in simulating an operator's manual
    ``DELETE FROM packs`` racing in first -- the only way ``get_pack`` can
    return ``None`` here, since no branch of ``pack_create`` itself deletes
    a row past the anchor write (see this module's docstring)."""
    delete_pack_row(sql_, pack_id, owner_id)
    return False


# ---------------------------------------------------------------------------
# 1. normal path
# ---------------------------------------------------------------------------


class TestNormalPath:
    def test_normal_creates_ready_pack_and_ingests(self, sql):
        ctx = _base_ctx(sql)
        result = _create(ctx, title="My Pack", pack_id="my-pack")

        assert "error" not in result
        assert result["pack_id"] == "my-pack"
        assert result["status"] == "ok"
        row = get_pack(sql, "my-pack")
        assert row is not None
        assert row["status"] == PACK_STATUS_READY
        assert row["owner_id"] == "alice"
        ctx["builder"].add_node.assert_called_once()
        _, call_kwargs = ctx["builder"].add_node.call_args
        assert call_kwargs["pack_anchor"] is True


# ---------------------------------------------------------------------------
# 2 & 3. anchor identity conflict -- the ONLY delete branch (§3.0), and its
# delete-fails-so-demote-instead fallback
# ---------------------------------------------------------------------------


class TestIdentityConflictDeletion:
    def test_conflict_before_writer_deletes_the_row_and_never_calls_add_node(self, sql):
        ctx = _base_ctx(sql)
        # the anchor slot is already claimed by a DIFFERENT pack_id
        ctx["neo4j"].get_node.return_value = {"pack_id": "someone-elses-pack"}

        result = _create(ctx, title="My Pack", pack_id="taken-anchor")

        assert "error" in result
        assert get_pack(sql, "taken-anchor") is None, (
            "the identity-conflict branch is the ONLY delete point (§3.0) -- "
            "the row must be gone, not merely demoted"
        )
        ctx["builder"].add_node.assert_not_called()

    def test_conflict_when_delete_fails_demotes_to_partial_instead(self, sql):
        ctx = _base_ctx(sql)
        ctx["neo4j"].get_node.return_value = {"pack_id": "someone-elses-pack"}

        with patch("opencrab.pack.ownership.delete_pack_row", return_value=False):
            result = _create(ctx, title="My Pack", pack_id="taken-anchor-2")

        assert "error" in result
        row = get_pack(sql, "taken-anchor-2")
        assert row is not None, "a failed delete must never silently vanish the row"
        assert row["status"] == PACK_STATUS_PARTIAL
        ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 4. all four anchor_verdict branches, with distinguishable messages, plus
# the startup non-conflict check
# ---------------------------------------------------------------------------


class TestAnchorVerdictBranches:
    def test_ambiguous_commit_that_actually_landed_joins_ready_path(self, sql):
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {
            "stores": {"graph": "error: connection dropped after commit"}
        }
        # reprobe finds it under THIS pack_id -- the commit actually landed
        ctx["neo4j"].get_node.return_value = {"pack_id": "verdict-graph"}

        result = _create(ctx, title="P", pack_id="verdict-graph")

        assert "error" not in result
        row = get_pack(sql, "verdict-graph")
        assert row["status"] == PACK_STATUS_READY

    def test_optional_only_verdict_demotes_with_distinguishing_message(self, sql):
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {
            "stores": {"graph": "error: disk I/O", "docs": "ok"}
        }
        ctx["neo4j"].get_node.return_value = None  # graph: absent
        ctx["mongo"].get_node_doc.return_value = {
            "properties": {"pack_id": "verdict-optional"}
        }  # docs: present

        result = _create(ctx, title="P", pack_id="verdict-optional")

        assert "error" in result
        assert "optional store" in result["error"]
        row = get_pack(sql, "verdict-optional")
        assert row is not None
        assert row["status"] == PACK_STATUS_PARTIAL

    def test_absent_verdict_demotes_without_deleting(self, sql):
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: disk I/O"}}
        # graph/docs/vector all default to absent already

        result = _create(ctx, title="P", pack_id="verdict-absent")

        assert "error" in result
        assert "no store at all" in result["error"]
        row = get_pack(sql, "verdict-absent")
        assert row is not None, (
            "even ANCHOR_ABSENT never deletes (§3.0) -- a slow remote commit "
            "landing after this check would otherwise orphan the graph"
        )
        assert row["status"] == PACK_STATUS_PARTIAL

    def test_unverifiable_verdict_demotes_without_deleting(self, sql):
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: disk I/O"}}
        calls = {"n": 0}

        def _flaky_get_node(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # pre-write identity probe: no conflict
            raise RuntimeError("graph read boom")  # post-write reprobe: cannot verify

        ctx["neo4j"].get_node.side_effect = _flaky_get_node

        result = _create(ctx, title="P", pack_id="verdict-unverifiable")

        assert "error" in result
        assert "could not be verified" in result["error"]
        row = get_pack(sql, "verdict-unverifiable")
        assert row is not None
        assert row["status"] == PACK_STATUS_PARTIAL

    def test_optional_absent_unverifiable_messages_are_all_distinguishable(self, sql):
        """Design v4 §3.5 requires the three non-graph verdicts to read
        differently to an operator even though they all take the SAME
        registry action (mark_pack_partial, never delete)."""
        ctx_optional = _base_ctx(sql)
        ctx_optional["builder"].add_node.return_value = {
            "stores": {"graph": "error: x", "docs": "ok"}
        }
        ctx_optional["mongo"].get_node_doc.return_value = {
            "properties": {"pack_id": "distinct-optional"}
        }
        msg_optional = _create(ctx_optional, title="P", pack_id="distinct-optional")["error"]

        ctx_absent = _base_ctx(sql)
        ctx_absent["builder"].add_node.return_value = {"stores": {"graph": "error: x"}}
        msg_absent = _create(ctx_absent, title="P", pack_id="distinct-absent")["error"]

        ctx_unverifiable = _base_ctx(sql)
        ctx_unverifiable["builder"].add_node.return_value = {"stores": {"graph": "error: x"}}
        calls = {"n": 0}

        def _flaky(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            raise RuntimeError("boom")

        ctx_unverifiable["neo4j"].get_node.side_effect = _flaky
        msg_unverifiable = _create(ctx_unverifiable, title="P", pack_id="distinct-unverifiable")[
            "error"
        ]

        messages = {msg_optional, msg_absent, msg_unverifiable}
        assert len(messages) == 3, (
            f"the three verdicts must produce distinguishable messages, got: {messages}"
        )


class TestStartupNonConflict:
    def test_partial_and_creating_rows_still_cover_graph_pack_ids(self, sql):
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: disk I/O"}}
        _create(ctx, title="P", pack_id="startup-partial")
        row = get_pack(sql, "startup-partial")
        assert row["status"] == PACK_STATUS_PARTIAL

        # A row a crashed request left in 'creating' with no write attempted
        # at all.
        begin_pack_creation(sql, "alice", "startup-creating", title="c")

        graph_stub = MagicMock()
        graph_stub.available = True
        graph_stub.list_pack_ids.return_value = {"startup-partial", "startup-creating"}

        # Must NOT raise -- read_scope's registry-covers-graph check is
        # status-agnostic (SELECT pack_id FROM packs, no WHERE status), so
        # partial/creating rows already "cover" their own graph pack_ids on
        # the next boot.
        assert_registry_covers_graph(sql, graph_stub)


# ---------------------------------------------------------------------------
# 5. mark_pack_ready returning False: all five reconciliation sub-cases
# ---------------------------------------------------------------------------


class TestMarkReadyFalseReconciliation:
    def test_row_vanished_and_anchor_confirmed_reregisters_but_does_not_ingest(self, sql):
        ctx = _base_ctx(sql)
        # matches -- pre-write probe sees no conflict, post-vanish reprobe
        # confirms the graph anchor is really there
        ctx["neo4j"].get_node.return_value = {"pack_id": "vanished-confirmed"}

        with (
            patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_vanish_row),
            patch("opencrab.mcp.tools.pack._ingest_into_pack") as ingest_mock,
        ):
            result = _create(ctx, title="P", pack_id="vanished-confirmed")

        ingest_mock.assert_not_called()
        assert "error" in result
        assert "re-registered as ready" in result["error"]
        row = get_pack(sql, "vanished-confirmed")
        assert row is not None
        assert row["status"] == PACK_STATUS_READY
        assert row["owner_id"] == "alice"

    def test_row_vanished_and_anchor_not_confirmed_does_not_reregister(self, sql):
        ctx = _base_ctx(sql)
        # default get_node -> None: reprobe finds nothing under this pack_id

        with (
            patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_vanish_row),
            patch("opencrab.mcp.tools.pack._ingest_into_pack") as ingest_mock,
        ):
            result = _create(ctx, title="P", pack_id="vanished-unconfirmed")

        ingest_mock.assert_not_called()
        assert "error" in result
        assert "NOT re-registered" in result["error"]
        assert get_pack(sql, "vanished-unconfirmed") is None

    def test_owner_changed_refuses_without_touching_the_row(self, sql):
        ctx = _base_ctx(sql)

        with (
            patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_steal_owner),
            patch("opencrab.mcp.tools.pack._ingest_into_pack") as ingest_mock,
        ):
            result = _create(ctx, title="P", pack_id="owner-changed")

        ingest_mock.assert_not_called()
        assert "error" in result
        row = get_pack(sql, "owner-changed")
        assert row["owner_id"] == "mallory", "pack_create must not reassign or demote it"

    def test_concurrent_demotion_confirms_partial_and_skips_ingest(self, sql):
        ctx = _base_ctx(sql)

        with (
            patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_demote_underneath),
            patch("opencrab.mcp.tools.pack._ingest_into_pack") as ingest_mock,
        ):
            result = _create(ctx, title="P", pack_id="raced-partial")

        ingest_mock.assert_not_called()
        assert "error" in result
        row = get_pack(sql, "raced-partial")
        assert row["status"] == PACK_STATUS_PARTIAL

    def test_concurrent_promotion_joins_success_path_and_ingests(self, sql):
        ctx = _base_ctx(sql)

        with (
            patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_promote_underneath),
            patch(
                "opencrab.mcp.tools.pack._ingest_into_pack",
                return_value={"status": "ok"},
            ) as ingest_mock,
        ):
            result = _create(ctx, title="P", pack_id="raced-ready")

        ingest_mock.assert_called_once()
        assert "error" not in result
        row = get_pack(sql, "raced-ready")
        assert row["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# 6 & 7. the no-deletion-after-writer invariant, across every post-writer
# failure branch this function has
# ---------------------------------------------------------------------------


class TestNoDeletionAfterWriterInvariant:
    def test_every_post_writer_failure_branch_leaves_the_row_in_place(self, sql):
        """#170 design v4 §3.0: once builder.add_node has been called, no
        branch in pack_create may delete the registry row. This walks every
        failure branch that runs after that call and confirms none of them
        does -- each leaves the row present (status 'partial' or 'ready'
        depending on the branch, owner possibly reassigned by an outside
        actor), never gone. (The one case where the row IS legitimately
        gone -- an outside DELETE racing in -- is exercised separately in
        TestMarkReadyFalseReconciliation's "vanished" tests, which assert
        pack_create's OWN control flow does not cause it.)
        """
        # ANCHOR_OPTIONAL_ONLY
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: x", "docs": "ok"}}
        ctx["mongo"].get_node_doc.return_value = {"properties": {"pack_id": "inv-optional"}}
        _create(ctx, title="P", pack_id="inv-optional")
        assert get_pack(sql, "inv-optional") is not None

        # ANCHOR_ABSENT
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: x"}}
        _create(ctx, title="P", pack_id="inv-absent")
        assert get_pack(sql, "inv-absent") is not None

        # ANCHOR_UNVERIFIABLE
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.return_value = {"stores": {"graph": "error: x"}}
        calls = {"n": 0}

        def _flaky(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            raise RuntimeError("boom")

        ctx["neo4j"].get_node.side_effect = _flaky
        _create(ctx, title="P", pack_id="inv-unverifiable")
        assert get_pack(sql, "inv-unverifiable") is not None

        # add_node raises outright (not just a per-store "error: ..." report)
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.side_effect = RuntimeError("graph down mid-write")
        _create(ctx, title="P", pack_id="inv-raised")
        assert get_pack(sql, "inv-raised") is not None

        # mark_pack_ready False + owner changed underneath
        ctx = _base_ctx(sql)
        with patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_steal_owner):
            _create(ctx, title="P", pack_id="inv-owner-changed")
        assert get_pack(sql, "inv-owner-changed") is not None

        # mark_pack_ready False + demoted underneath
        ctx = _base_ctx(sql)
        with patch("opencrab.pack.ownership.mark_pack_ready", side_effect=_demote_underneath):
            _create(ctx, title="P", pack_id="inv-demoted")
        assert get_pack(sql, "inv-demoted") is not None


class TestFailureResponseNamesTheRetainedPack:
    """A failure that keeps the registry row has to say WHICH row (#170 PR
    review, P2).

    The caller cannot work it out. ``create_pack``'s slug negotiation
    silently appends a random suffix when the requested id is taken (#143
    invariant 7 -- an "already exists" error would confirm someone else holds
    that exact slug), so the id that got reserved, demoted, and left behind
    may be nothing the caller ever named. Without it in the response there is
    no way to reach that row again: not to promote it with
    ``packs repair-registry --promote``, not to ingest into it after the
    stores recover, not even to recognise it in a later listing.

    ``registry_status`` comes back as read, not as intended -- the demotion
    these branches attempt can itself match no row."""

    def test_suffixed_slug_is_returned_when_the_anchor_write_fails(self, sql):
        """The case the id actually matters in: the requested slug was taken,
        so what got retained is a suffixed id the caller never supplied."""
        squatter = begin_pack_creation(sql, "someone-else", "coffee")
        assert squatter == "coffee"  # the plain slug is now occupied

        ctx = _base_ctx(sql)
        ctx["builder"].add_node.side_effect = RuntimeError("graph down")
        result = _create(ctx, title="Coffee", pack_id="coffee")

        assigned = result["pack_id"]
        assert assigned != "coffee", "the taken slug cannot have been reused"
        assert assigned.startswith("coffee-")
        assert result["registry_status"] == PACK_STATUS_PARTIAL

        # The id is usable: it resolves to the retained row, and that row is
        # ours rather than the squatter's.
        row = get_pack(sql, assigned)
        assert row is not None
        assert row["owner_id"] == "alice"
        assert row["status"] == PACK_STATUS_PARTIAL
        # ...and the squatter's pack was left exactly as it was.
        assert get_pack(sql, "coffee")["owner_id"] == "someone-else"

    def test_status_is_read_back_not_assumed(self, sql):
        """When the demotion itself fails to apply, the response must report
        what the row IS, not what the branch tried to make it."""
        ctx = _base_ctx(sql)
        ctx["builder"].add_node.side_effect = RuntimeError("graph down")

        import opencrab.pack.ownership as ownership_mod

        with patch.object(
            ownership_mod, "mark_pack_partial", side_effect=RuntimeError("update lost")
        ):
            result = _create(ctx, title="Stuck", pack_id="stuck-pack")

        assert result["pack_id"] == "stuck-pack"
        # The demotion never landed, so the row is still 'creating' -- and
        # that, not 'partial', is what the caller is told.
        assert result["registry_status"] == "creating"
        assert get_pack(sql, "stuck-pack")["status"] == "creating"

    def test_a_foreign_row_yields_its_id_but_not_its_status(self, sql):
        """#143 invariant 7: the id is the caller's own, but another owner's
        lifecycle state is not theirs to see."""
        ctx = _base_ctx(sql)

        import opencrab.pack.ownership as ownership_mod

        with patch.object(
            ownership_mod,
            "mark_pack_ready",
            side_effect=lambda s, p, o: _steal_owner(s, p, o),
        ):
            result = _create(ctx, title="Taken", pack_id="taken-pack")

        assert result["pack_id"] == "taken-pack"
        assert result["registry_status"] is None
        assert get_pack(sql, "taken-pack")["owner_id"] == "mallory"

    def test_a_row_that_became_foreign_during_the_read_back_stays_silent(self, sql):
        """The leak the first version of this response shape had.

        The window is narrow and specific: ``pack_create`` finds its own row
        GONE, and by the time the response reads the registry back to report a
        status, another subject has claimed the slug. The status read there
        belongs to a stranger -- and a ``creating`` one is invisible to every
        read path, so reporting it would confirm exactly what slug negotiation
        exists to hide, that the slug is taken (#143 invariant 7).

        The squat is timed inside the anchor re-probe, which the row-missing
        branch runs between its own ``get_pack`` and the response. That is the
        real window, not a stand-in for it: ``get_pack`` is unscoped by design,
        so the ownership check has to live where the value enters the response.
        """

        def _vanish(sql_, pack_id, owner_id):
            """``mark_pack_ready`` stand-in: the caller's row disappears."""
            delete_pack_row(sql_, pack_id, owner_id)
            return False

        def _squat_then_report_absent(graph, docs, vector, pack_id):
            """``probe_anchor`` stand-in: another subject takes the slug during
            the probe, and the anchor is reported absent so the branch does not
            attempt a re-registration (that path is covered elsewhere)."""
            begin_pack_creation(sql, "mallory", pack_id)
            return {"graph": "absent", "docs": "absent", "vector": "absent"}

        ctx = _base_ctx(sql)

        import opencrab.pack.lifecycle as lifecycle_mod
        import opencrab.pack.ownership as ownership_mod

        with (
            patch.object(ownership_mod, "mark_pack_ready", side_effect=_vanish),
            patch.object(lifecycle_mod, "probe_anchor", side_effect=_squat_then_report_absent),
        ):
            result = _create(ctx, title="Vanish", pack_id="vanish-pack")

        assert result["pack_id"] == "vanish-pack"
        assert result["registry_status"] is None, "a foreign row's status must not be reported"
        # The row really is there and really is mallory's -- so the None above
        # is a withheld value, not an absent row.
        squatted = get_pack(sql, "vanish-pack")
        assert squatted is not None
        assert squatted["owner_id"] == "mallory"
        assert squatted["status"] == "creating"

    def test_a_failed_reregistration_never_says_the_slug_is_taken(self, sql, caplog):
        """The re-registration message may not report occupancy.

        Two things can stop the PK-safe re-insert: another row already holds
        the slug, or the insert raised. Naming the first in the response would
        report that the slug is taken -- the one fact slug negotiation is
        built to keep out of responses (#143 invariant 7) -- and saying it for
        the second would assert a cause nobody checked. The response therefore
        says only that it did not land; the two are separated in the log,
        which the caller never sees."""
        import logging

        def _vanish(sql_, pack_id, owner_id):
            delete_pack_row(sql_, pack_id, owner_id)
            return False

        def _squat_then_confirm_anchor(graph, docs, vector, pack_id):
            """The slug is taken during the window AND the anchor is
            confirmed, so the branch attempts the re-insert and loses."""
            begin_pack_creation(sql, "mallory", pack_id)
            return {"graph": "present", "docs": "absent", "vector": "absent"}

        ctx = _base_ctx(sql)

        import opencrab.pack.lifecycle as lifecycle_mod
        import opencrab.pack.ownership as ownership_mod

        with (
            caplog.at_level(logging.WARNING),
            patch.object(ownership_mod, "mark_pack_ready", side_effect=_vanish),
            patch.object(lifecycle_mod, "probe_anchor", side_effect=_squat_then_confirm_anchor),
        ):
            result = _create(ctx, title="Contested", pack_id="contested-pack")

        error = result["error"]
        assert "did not land" in error
        for leak in ("held by another", "already", "taken", "occupied", "mallory"):
            assert leak not in error, f"response discloses occupancy via {leak!r}"
        assert result["registry_status"] is None  # mallory's row, withheld
        # The operator still gets the real cause, server-side.
        assert any("already " in r.message or "already" in str(r.args) for r in caplog.records)
