"""Where #194's anchor auto-create meets #201's ``pack_fork``.

Both landed on ``OntologyBuilder.add_node`` and ``opencrab/pack/lifecycle.py``
independently, and merging them raised questions neither side had to answer
alone. This file pins the answers, so that reverting one of them fails here
rather than in production:

- ``_allow_ready_anchor`` must not accept a ``creating`` pack. #201 opened
  ``creating`` to a fork's bulk copy but required ``forked_from`` to keep that
  opening from becoming a general door; an anchor-shaped write that accepted
  ``creating`` with no equivalent requirement would reopen it.
- The two opt-ins are mutually exclusive. Their combination only became
  expressible when both features shared one function, and the dispatch order
  would let the stricter gate be shadowed silently.
- Rebuilding a forked pack's anchor must not launder its provenance into
  looking natively created.
- Repairing an anchor must never move the registry row's status, whatever
  ``repair_incomplete_packs`` would have decided about a fork.

Store doubles are defined locally rather than imported from another test
module: they honour the same slot contract the builder and ``probe_anchor``
probe, and keeping them here means #201's own test file can evolve its
private doubles without silently changing what these tests assert.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack.lifecycle import ensure_anchor, repair_missing_anchors
from opencrab.pack.ownership import (
    PACK_STATUS_READY,
    PackNotFoundError,
    anchor_node_id,
    begin_pack_creation,
    create_pack,
    get_pack,
)

ANCHOR_SPACE = "resource"
ANCHOR_TYPE = "Dataset"


# ---------------------------------------------------------------------------
# Store doubles -- the slots `OntologyBuilder.add_node` and `probe_anchor`
# actually touch, and nothing more.
# ---------------------------------------------------------------------------


class Graph:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.nodes: dict[tuple[str, str], dict] = {}
        self.upsert_calls = 0

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (_t, i), v in sorted(self.nodes.items()) if i == node_id]

    def lookup_node_type(self, node_id):
        for (t, i) in self.nodes:
            if i == node_id:
                return t
        return None

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.upsert_calls += 1
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)


class Docs:
    def __init__(self, *, available: bool = True):
        self.available = available

    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return None

    def upsert_node_doc(self, space, node_type, node_id, properties):  # noqa: ARG002
        return "doc-1"

    def log_event(self, *a, **kw):  # noqa: ARG002
        return "ev-1"


class Vec:
    def __init__(self, *, available: bool = True):
        self.available = available

    def get_by_id(self, doc_id):  # noqa: ARG002
        return None

    def upsert_texts(self, texts, ids, metadatas):  # noqa: ARG002
        return list(ids)


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


def _principal(sql, user_id: str) -> Principal:
    return Principal(user_id=user_id, is_local=False, disabled=False)


def _anchor_props(pack_id: str) -> dict:
    """The property set `pack_create` and `ensure_anchor` both write."""
    return {
        "pack_id": pack_id,
        "title": pack_id,
        "description": "",
        "created_by": "localcrab-mcp",
    }


# ---------------------------------------------------------------------------
# 1. `_allow_ready_anchor` opens `ready`, and only `ready`.
# ---------------------------------------------------------------------------


def test_allow_ready_anchor_does_not_open_a_creating_pack(sql, alice):
    """The merge's narrowest decision, and the one most likely to be undone by
    someone "restoring" the original tuple.

    #201's `authorize_fork_copy` opens `creating` only for rows a fork
    reserved. If this flag also accepted `creating`, the same window would be
    open for anchor-shaped writes with no such requirement -- including on a
    row `pack_create` still has in flight.
    """
    pack_id = begin_pack_creation(sql, alice, "still-creating")
    builder = OntologyBuilder(Graph(), Docs(), sql, vec=Vec())

    with principal_scope(_principal(sql, alice)):
        # Not `pytest.raises(Exception)`: the shape check on this same branch
        # also raises, so a loose assertion would pass without the gate.
        # `assert_writable` reports a status outside `allowed_statuses` as
        # "not found", never "forbidden" (#143 invariant 7).
        with pytest.raises(PackNotFoundError):
            builder.add_node(
                space=ANCHOR_SPACE,
                node_type=ANCHOR_TYPE,
                node_id=anchor_node_id(pack_id),
                properties=_anchor_props(pack_id),
                pack_id=pack_id,
                origin="server",
                pack_anchor=True,
                _allow_ready_anchor=True,
            )


def test_allow_ready_anchor_does_open_a_ready_pack(sql, alice):
    """Control for the test above: the flag is not simply inert."""
    pack_id = create_pack(sql, alice, "published")
    graph = Graph()
    builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

    with principal_scope(_principal(sql, alice)):
        receipt = builder.add_node(
            space=ANCHOR_SPACE,
            node_type=ANCHOR_TYPE,
            node_id=anchor_node_id(pack_id),
            properties=_anchor_props(pack_id),
            pack_id=pack_id,
            origin="server",
            pack_anchor=True,
            _allow_ready_anchor=True,
        )

    assert receipt["stores"]["graph"] == "ok"
    assert (ANCHOR_TYPE, anchor_node_id(pack_id)) in graph.nodes


# ---------------------------------------------------------------------------
# 6. The two opt-ins are mutually exclusive.
# ---------------------------------------------------------------------------


class TestAnchorAndForkCopyAreExclusive:
    """Guard for a combination that only exists because both features merged.

    The dispatch lets `pack_anchor` shadow `fork_copy`, and the gate being
    shadowed is the stricter one -- so the failure mode is silent widening,
    not a visible error.
    """

    def test_combination_is_refused(self, sql, alice):
        pack_id = begin_pack_creation(sql, alice, "fork-dst", forked_from="src")
        builder = OntologyBuilder(Graph(), Docs(), sql, vec=Vec())

        with principal_scope(_principal(sql, alice)):
            # `match=` matters: this branch's OTHER ValueError (anchor shape)
            # would otherwise satisfy an unqualified raises().
            with pytest.raises(ValueError, match="mutually exclusive"):
                builder.add_node(
                    space=ANCHOR_SPACE,
                    node_type=ANCHOR_TYPE,
                    node_id=anchor_node_id(pack_id),
                    properties=_anchor_props(pack_id),
                    pack_id=pack_id,
                    origin="server",
                    pack_anchor=True,
                    fork_copy=True,
                )

    def test_refusal_precedes_authorization(self, sql, alice):
        """A nonsensical request must not learn the pack's authorization
        state first -- same ordering rule the anchor shape check follows."""
        builder = OntologyBuilder(Graph(), Docs(), sql, vec=Vec())

        with principal_scope(_principal(sql, alice)):
            with pytest.raises(ValueError, match="mutually exclusive"):
                builder.add_node(
                    space=ANCHOR_SPACE,
                    node_type=ANCHOR_TYPE,
                    node_id=anchor_node_id("no-such-pack"),
                    properties=_anchor_props("no-such-pack"),
                    pack_id="no-such-pack",
                    origin="server",
                    pack_anchor=True,
                    fork_copy=True,
                )


# ---------------------------------------------------------------------------
# 2. An in-flight fork is not repair's business. (characterization)
# ---------------------------------------------------------------------------


def test_ensure_anchor_leaves_an_in_flight_fork_alone(sql, alice):
    """Characterization, not a change: `ensure_anchor` already refuses every
    non-`ready` row, and `forked_from` plays no part in that decision.

    It is pinned here anyway because the merge puts a second lifecycle pass
    next to #201's, and the reverse mutation that would break it (widening the
    status gate to include `creating`) is an easy "fix" for someone who notices
    that a dead fork's anchor is never rebuilt.
    """
    pack_id = begin_pack_creation(sql, alice, "fork-in-flight", forked_from="src")
    graph = Graph()
    builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

    result = ensure_anchor(sql, builder, graph, Docs(), Vec(), pack_id, apply=True)

    assert result["action"] == "skipped"
    assert "not 'ready'" in result["reason"]
    assert graph.upsert_calls == 0


# ---------------------------------------------------------------------------
# 3. Rebuilding a forked pack's anchor keeps its provenance.
# ---------------------------------------------------------------------------


class TestRebuiltAnchorProvenance:
    def test_forked_pack_keeps_its_fork_markers(self, sql, alice):
        """`pack_fork` stamps `forked_from` and its own `created_by` onto the
        anchor it writes. A repair that dropped them would quietly rewrite a
        forked pack's history into a natively created one."""
        create_pack(sql, alice, "upstream")
        pack_id = create_pack(sql, alice, "downstream", forked_from="upstream")
        graph = Graph()
        builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        result = ensure_anchor(sql, builder, graph, Docs(), Vec(), pack_id, apply=True)

        assert result["action"] == "created"
        written = graph.nodes[(ANCHOR_TYPE, anchor_node_id(pack_id))]
        assert written["forked_from"] == "upstream"
        assert written["created_by"] == "localcrab-mcp:pack_fork"

    def test_ordinary_pack_gets_no_fork_markers(self, sql, alice):
        """Control: the fork shape is conditional, not applied to everything."""
        pack_id = create_pack(sql, alice, "home-grown")
        graph = Graph()
        builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        assert ensure_anchor(
            sql, builder, graph, Docs(), Vec(), pack_id, apply=True
        )["action"] == "created"

        written = graph.nodes[(ANCHOR_TYPE, anchor_node_id(pack_id))]
        assert written["created_by"] == "localcrab-mcp"
        assert "forked_from" not in written

    def test_repair_never_moves_the_registry_status(self, sql, alice):
        """The invariant that keeps this pass clear of #201's lifecycle rules.

        #201 forbids promoting a forked row on anchor evidence, because a
        half-copied fork's anchor looks exactly like a complete one's. Repair
        stays compatible with that by never transitioning anything at all --
        it only rebuilds a node for a row that is already `ready`.
        """
        create_pack(sql, alice, "up")
        pack_id = create_pack(sql, alice, "down", forked_from="up")
        graph = Graph()
        builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY
        ensure_anchor(sql, builder, graph, Docs(), Vec(), pack_id, apply=True)
        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# 5 / 5-b. The batch pass, which is what the CLI actually runs.
# ---------------------------------------------------------------------------


class TestRepairMissingAnchorsApply:
    """`--apply` had no coverage at all -- only the dry-run path was tested."""

    def test_repairs_every_owner_under_that_owner_s_authority(self, sql):
        """The candidate query has no owner filter, and that is deliberate: an
        offline operator pass that only fixed one user's packs would be half a
        repair on a multi-user deployment. Authorization is not skipped, it
        moves inside `ensure_anchor`, which authorizes as each pack's OWNER.

        `create_user` also creates a default pack, and those are `ready` with
        no anchor by design (see `TestAnchorlessReadyIsNormal`). They are
        legitimate candidates, so the expected count includes them -- passing
        `pack_ids` to make the number smaller would delete the very property
        this test exists to hold.
        """
        alice = create_user(sql, "Alice")
        bob = create_user(sql, "Bob")
        create_pack(sql, alice, "up")
        a_pack = create_pack(sql, alice, "alice-fork", forked_from="up")
        b_pack = create_pack(sql, bob, "bob-pack")

        graph = Graph()
        builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())
        summary = repair_missing_anchors(
            sql, graph, Docs(), Vec(), builder, apply=True
        )

        # alice: default + "up" + "alice-fork"; bob: default + "bob-pack".
        assert summary["counts"]["checked"] == 5
        assert summary["counts"]["created"] == 5
        assert summary["counts"]["failed"] == 0

        assert graph.nodes[(ANCHOR_TYPE, anchor_node_id(a_pack))]["forked_from"] == "up"
        assert "forked_from" not in graph.nodes[(ANCHOR_TYPE, anchor_node_id(b_pack))]
        for pid in (a_pack, b_pack):
            assert get_pack(sql, pid)["status"] == PACK_STATUS_READY

    def test_second_run_is_a_no_op(self, sql, alice):
        """Characterization: `ensure_anchor` documents itself as idempotent.

        Already true before this merge -- pinned because the batch pass is now
        reachable from the CLI with `--apply`, so a regression would rewrite
        every anchor on every operator run.
        """
        create_pack(sql, alice, "solo")
        graph = Graph()
        builder = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        first = repair_missing_anchors(sql, graph, Docs(), Vec(), builder, apply=True)
        writes_after_first = graph.upsert_calls
        second = repair_missing_anchors(sql, graph, Docs(), Vec(), builder, apply=True)

        assert first["counts"]["created"] == 2  # default pack + "solo"
        assert second["counts"]["created"] == 0
        assert second["counts"]["already_present"] == 2
        assert graph.upsert_calls == writes_after_first

    def test_graph_alone_decides_success(self, sql, alice):
        """Characterization of the criterion #201's fork write does NOT share.

        `_fork_leg_ok("anchor")` demands all four legs, because fork's
        preflight already proved they were all up. Repair works on whatever
        deployment it finds, so requiring the same would make a pack
        unrepairable for as long as the vector store is down -- strictly worse
        than rebuilding the leg that is the system of record.
        """
        create_pack(sql, alice, "up")
        pack_id = create_pack(sql, alice, "fork-of-up", forked_from="up")
        graph = Graph()
        docs, vec = Docs(available=False), Vec(available=False)
        builder = OntologyBuilder(graph, docs, sql, vec=vec)

        result = ensure_anchor(sql, builder, graph, docs, vec, pack_id, apply=True)

        assert result["action"] == "created"
        assert result["stores"]["graph"] == "ok"
        assert result["stores"]["docs"] == "unavailable"
        # Success must come from the graph leg ITSELF, not from the re-probe
        # rescue below it. Without this the assertion above is satisfied
        # either way: raise the bar to fork's four legs and the write "fails",
        # but the re-probe then finds the node the write just made and reports
        # `created` regardless -- so the test would pass while the criterion
        # it claims to pin is gone. The re-probe branch adds this key.
        assert "reprobe" not in result
        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY
        assert graph.nodes[(ANCHOR_TYPE, anchor_node_id(pack_id))]["forked_from"] == "up"


# ---------------------------------------------------------------------------
# 4. The same thing, through `pack_ingest` rather than the library call.
# ---------------------------------------------------------------------------


def test_pack_ingest_rebuilds_a_forked_anchor_with_its_provenance(bind_test_principal):
    """Covers the wiring, which the library-level tests above cannot.

    `_writable_ctx` is not reusable here: it forwards its overrides to the
    context but not to `create_pack`, so it cannot produce a row carrying
    `forked_from`. The pack is owned by `test-user` because that is who
    `bind_test_principal` binds, and `pack_ingest` authorizes before it gets
    anywhere near the anchor.
    """
    from test_tools_handlers_direct import _base_ctx

    from opencrab.mcp.tools.pack import pack_ingest
    from opencrab.stores.sql_store import SQLStore

    sql = SQLStore("sqlite:///:memory:")
    create_pack(sql, "test-user", "upstream")
    pack_id = create_pack(sql, "test-user", "mine", forked_from="upstream")

    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    ctx = _base_ctx(sql=sql, builder=builder)
    # The builder must carry the SAME store handles as the context (#224).
    # `ensure_anchor` now probes and runs the identity guard through the
    # builder's stores, because that is where `add_node` will write -- so a
    # builder holding bare mocks while the context holds configured ones is a
    # shape production never has, and it makes the probe read an unrelated
    # store. Mirroring them here keeps the double honest about that.
    builder._neo4j, builder._mongo, builder._vec = (
        ctx["neo4j"], ctx["mongo"], ctx["chroma"],
    )

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(
            pack_id, nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}]
        )

    assert result.get("status") == "ok"
    anchor_calls = [
        c for c in builder.add_node.call_args_list
        if c.kwargs.get("node_id") == anchor_node_id(pack_id)
    ]
    assert len(anchor_calls) == 1
    props = anchor_calls[0].kwargs["properties"]
    assert props["forked_from"] == "upstream"
    assert props["created_by"] == "localcrab-mcp:pack_fork"


# ---------------------------------------------------------------------------
# 7. `packs repair-anchors --apply` -- the operator entry point.
# ---------------------------------------------------------------------------


class TestCLIRepairAnchorsApply:
    """Only the argument validation of this command was covered; the path that
    actually writes was not, and it is the one path that exercises
    `ensure_anchor`'s synthesized-principal branch."""

    @pytest.fixture()
    def cli_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STORAGE_MODE", "local")
        from opencrab.config import get_settings

        get_settings.cache_clear()
        yield tmp_path
        get_settings.cache_clear()

    @pytest.fixture()
    def mock_vector_store(self, tmp_path):
        """Same rationale as the fixture of this name in tests/test_cli.py:
        never let a CLI test load the real embedding chain."""
        from _vec_helpers import build_vector_store

        store = build_vector_store("sqlite-vec", tmp_path, dim=32)
        with patch("opencrab.stores.factory.make_vector_store", return_value=store):
            yield store

    def test_apply_rebuilds_a_forked_anchor(self, cli_env, mock_vector_store):
        from click.testing import CliRunner

        from opencrab.cli import main
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        cfg = get_settings()
        sql = make_sql_store(cfg)
        owner = create_user(sql, "Operator")
        create_pack(sql, owner, "upstream")
        pack_id = create_pack(sql, owner, "forked", forked_from="upstream")

        result = CliRunner().invoke(
            main, ["packs", "repair-anchors", "--pack-id", pack_id, "--apply"]
        )

        assert result.exit_code == 0, result.output
        brace = result.output.index("{")
        payload = json.JSONDecoder().raw_decode(result.output[brace:])[0]
        assert payload["apply"] is True
        assert payload["counts"]["created"] == 1

        from opencrab.cli import _optional_store

        graph = _optional_store(get_settings(), "graph")
        written = graph.get_node(ANCHOR_TYPE, anchor_node_id(pack_id))
        assert written is not None
        assert written["forked_from"] == "upstream"
        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# #223 / #224: the repair commands join the write.lock map, and their plans
# stop promising writes that apply would refuse.
# ---------------------------------------------------------------------------


class _Recorder:
    """One timeline for lock span, registry read, probe, and write.

    Counting lock acquisitions is not enough to pin either issue. A lock taken
    and released before the probe satisfies a call count while leaving open
    the exact window it exists to close, so these tests record enter/exit as
    events next to the operations and assert containment.
    """

    def __init__(self):
        self.events: list[str] = []
        self.dirs: list = []

    def lock(self):
        import contextlib

        @contextlib.contextmanager
        def _cm(data_dir=None, **kw):  # noqa: ARG001
            self.dirs.append(data_dir)
            self.events.append("lock-enter")
            try:
                yield
            finally:
                self.events.append("lock-exit")

        return _cm

    def pairs(self) -> int:
        return self.events.count("lock-enter")

    def reset(self):
        """Drop setup noise so the timeline starts at the call under test.

        `create_pack` reads the registry too, so without this the first
        recorded event belongs to fixture setup rather than to the call whose
        lock window is being asserted.
        """
        self.events.clear()
        self.dirs.clear()


@pytest.fixture
def rec(monkeypatch):
    import opencrab.locking as locking_mod
    import opencrab.pack.lifecycle as lifecycle_mod
    import opencrab.pack.ownership as ownership_mod

    r = _Recorder()
    monkeypatch.setattr(locking_mod, "write_lock", r.lock())

    real_get_pack, real_probe = ownership_mod.get_pack, lifecycle_mod.probe_anchor

    def spy_get_pack(*a, **kw):
        r.events.append("get_pack")
        return real_get_pack(*a, **kw)

    def spy_probe(*a, **kw):
        r.events.append("probe")
        return real_probe(*a, **kw)

    monkeypatch.setattr(ownership_mod, "get_pack", spy_get_pack)
    monkeypatch.setattr(lifecycle_mod, "probe_anchor", spy_probe)
    return r


def _builder(graph, docs=None, vec=None):
    return OntologyBuilder(graph, docs or Docs(), None, vec=vec or Vec())


class TestEnsureAnchorLockWindow:
    def test_apply_wraps_registry_read_through_write(self, sql, alice, rec):
        """The window opens at the registry read, not at the probe.

        `repair_missing_anchors` selects candidates with an unlocked query and
        leaves the status re-check to `ensure_anchor`; if that re-check sat
        outside the lock, a pack demoted after the select could still be
        written to.
        """
        pack_id = create_pack(sql, alice, "locked")
        graph = Graph()
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())
        real_upsert = graph.upsert_node

        def spy(*a, **kw):
            rec.events.append("write")
            return real_upsert(*a, **kw)

        graph.upsert_node = spy
        rec.reset()

        assert ensure_anchor(
            sql, b, graph, Docs(), Vec(), pack_id, apply=True
        )["action"] == "created"

        assert rec.events[0] == "lock-enter"
        assert rec.events[-1] == "lock-exit"
        for stage in ("get_pack", "probe", "write"):
            assert 0 < rec.events.index(stage) < rec.events.index("lock-exit")

    def test_dry_run_takes_no_lock(self, sql, alice, rec):
        pack_id = create_pack(sql, alice, "planned")
        graph = Graph()
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())
        rec.reset()
        assert ensure_anchor(
            sql, b, graph, Docs(), Vec(), pack_id, apply=False
        )["action"] == "would_create"
        assert "lock-enter" not in rec.events

    def test_data_dir_reaches_the_lock_through_the_batch(self, sql, alice, rec):
        """Exposing the argument is not the same as forwarding it.

        `repair_missing_anchors` is the entry point the CLI uses, so an
        argument it accepts but drops would lock the configured directory
        while writing somewhere else -- and nothing about the signature
        would show it.
        """
        create_pack(sql, alice, "dirtest")
        graph = Graph()
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())
        rec.reset()
        repair_missing_anchors(
            sql, graph, Docs(), Vec(), b, apply=True, data_dir="/tmp/o223-probe"
        )
        assert rec.dirs and all(d == "/tmp/o223-probe" for d in rec.dirs)


class _ForeignGraph(Graph):
    """Graph whose anchor slot is held by a DIFFERENT pack.

    `_probe_one` reports that as absent (the value does not match this pack),
    so the probe alone says "go ahead" -- which is exactly why the plan used
    to promise a write the identity guard then refused.
    """

    def __init__(self, node_id, owner_pack):
        super().__init__()
        self.nodes[(ANCHOR_TYPE, node_id)] = {"pack_id": owner_pack}


class _MuteGraph(Graph):
    """Available, but cannot answer the type-agnostic axis.

    `_check_by_id_axis` fail-closes that to `unverifiable`, and `add_node`
    refuses on it just as hard as on `foreign`.
    """

    def __init__(self):
        super().__init__()
        del self.__dict__["nodes"]
        self.nodes = {}

    get_nodes_by_id = None  # type: ignore[assignment]


class TestPlanAndApplyAgree:
    """#224: the plan has to be a prediction, not a guess.

    Both paths run the same predicate now, so a slot the guard will refuse is
    reported the same way whether or not `--apply` was passed.
    """

    def _run(self, sql, owner, graph, pack_id):
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())
        dry = ensure_anchor(sql, b, graph, Docs(), Vec(), pack_id, apply=False)
        wet = ensure_anchor(sql, b, graph, Docs(), Vec(), pack_id, apply=True)
        return dry, wet

    def test_foreign_slot_is_blocked_in_both_modes(self, sql, alice):
        pack_id = create_pack(sql, alice, "wants-slot")
        graph = _ForeignGraph(anchor_node_id(pack_id), "someone-else")
        dry, wet = self._run(sql, alice, graph, pack_id)

        assert dry["action"] == wet["action"] == "blocked"
        assert dry["reason"] == wet["reason"] == "foreign"

    def test_unverifiable_slot_is_blocked_in_both_modes(self, sql, alice):
        """The reason an operator must NOT read as permanent.

        Blocking here is what keeps the plan honest -- apply refuses on
        `unverifiable` too -- but a retry can legitimately change it, which is
        why the reason travels with the verdict.
        """
        pack_id = create_pack(sql, alice, "cannot-ask")
        dry, wet = self._run(sql, alice, _MuteGraph(), pack_id)

        assert dry["action"] == wet["action"] == "blocked"
        assert dry["reason"] == wet["reason"] == "unverifiable"

    def test_blocked_never_calls_add_node(self, sql, alice):
        """Not merely "nothing was written".

        Before this change apply DID call `add_node` and let it raise, so a
        no-write assertion passed either way. What the fix buys is not making
        the call at all.
        """
        pack_id = create_pack(sql, alice, "no-attempt")
        graph = _ForeignGraph(anchor_node_id(pack_id), "other")
        b = MagicMock()
        b._neo4j, b._mongo, b._vec = graph, Docs(), Vec()

        result = ensure_anchor(sql, b, graph, Docs(), Vec(), pack_id, apply=True)

        assert result["action"] == "blocked"
        b.add_node.assert_not_called()
        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY

    def test_batch_counts_blocked_separately_from_skipped(self, sql, alice):
        """`skipped` means this pass did not look; `blocked` means it looked
        and the slot is unavailable. Folding them hides the one that needs
        an operator."""
        pack_id = create_pack(sql, alice, "counted")
        graph = _ForeignGraph(anchor_node_id(pack_id), "other")
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        summary = repair_missing_anchors(
            sql, graph, Docs(), Vec(), b, apply=True, pack_ids=[pack_id]
        )

        assert summary["counts"]["blocked"] == 1
        assert summary["counts"]["skipped"] == 0
        assert summary["counts"]["failed"] == 0


class TestBuilderAvailabilityIsDecidedBeforeThePlan:
    def test_no_builder_gives_the_same_answer_in_both_modes(self, sql, alice):
        """The other half of #224, and the reason the CLI now builds its
        builder for dry runs too.

        The graph is deliberately alive here: when it is down the probe
        already ends both modes at `skipped`, so that shape proves nothing.

        It is also deliberately one that cannot answer the by-id axis. That
        makes this test pull double duty: it pins the ORDER of the two checks.
        With the builder check first (as designed) the missing builder decides
        and both modes say `skipped`. Run the identity predicate first instead
        and the unanswerable axis fail-closes to `blocked`, so the assertion
        below catches the reordering. A double that could answer would make
        both orders agree and prove nothing.
        """
        pack_id = create_pack(sql, alice, "no-builder")
        graph = _MuteGraph()

        dry = ensure_anchor(sql, None, graph, Docs(), Vec(), pack_id, apply=False)
        wet = ensure_anchor(sql, None, graph, Docs(), Vec(), pack_id, apply=True)

        assert dry["action"] == wet["action"] == "skipped"

    def test_store_choice_follows_the_builder_s_graph_not_just_its_presence(
        self, sql, alice
    ):
        """A builder object whose `_neo4j` is None is not a usable builder.

        Selecting stores on `builder is not None` would probe through that
        None and fail-close to `skipped`, while the argument stores can answer
        perfectly well. Both checks have to use the same predicate.
        """
        pack_id = create_pack(sql, alice, "hollow-builder")
        graph = Graph()
        graph.nodes[(ANCHOR_TYPE, anchor_node_id(pack_id))] = {"pack_id": pack_id}
        hollow = MagicMock()
        hollow._neo4j = None

        result = ensure_anchor(
            sql, hollow, graph, Docs(), Vec(), pack_id, apply=False
        )

        assert result["action"] == "already_present"


class TestRepairRegistryLockWindows:
    """The sibling command joins the same map.

    Its registry writes are already compare-and-set, so this is map
    consistency rather than a bug fix -- but a command left outside the map
    is the next person's surprise, and the probe-to-transition span is still
    unserialized without it.
    """

    @staticmethod
    def _stale(sql, pack_id):
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text as _t

        dt = datetime.now(UTC) - timedelta(seconds=7200)
        with sql._engine.begin() as conn:
            conn.execute(
                _t("UPDATE packs SET updated_at = :v WHERE pack_id = :p"),
                {"v": dt.strftime("%Y-%m-%d %H:%M:%S"), "p": pack_id},
            )

    def test_one_window_per_row_not_one_around_the_sweep(self, sql, alice, rec):
        """Per row, deliberately.

        An operator pass over a large registry that held one exclusive lock
        for its whole duration would stall every writer -- worse than the gap
        it closes. The unit that must be atomic is one row's probe-to-write.
        """
        from opencrab.pack.lifecycle import repair_incomplete_packs

        for name in ("row-a", "row-b"):
            self._stale(sql, begin_pack_creation(sql, alice, name))
        rec.reset()

        repair_incomplete_packs(
            sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
        )

        assert rec.pairs() == 2

    def test_the_row_window_contains_probe_and_the_transition(
        self, sql, alice, rec, monkeypatch
    ):
        """Counting windows is not the same as covering the right code.

        An earlier draft of this change wrapped the wrong branch -- a counter
        bump a few lines up -- and the per-row COUNT test still passed,
        because one window per row was opened either way. Nothing was inside
        it. So assert containment of the two things that matter: the probe
        the decision reads, and the CAS transition it produces.
        """
        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.lifecycle import repair_incomplete_packs

        real_mark = ownership_mod.mark_pack_partial

        def spy(*a, **kw):
            rec.events.append("transition")
            return real_mark(*a, **kw)

        monkeypatch.setattr(ownership_mod, "mark_pack_partial", spy)
        self._stale(sql, begin_pack_creation(sql, alice, "contained"))
        rec.reset()

        repair_incomplete_packs(
            sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
        )

        enter = rec.events.index("lock-enter")
        leave = rec.events.index("lock-exit", enter)
        assert enter < rec.events.index("probe", enter) < leave
        assert enter < rec.events.index("transition", enter) < leave

    def test_dry_run_takes_no_lock_including_promote(self, sql, alice, rec):
        from opencrab.pack.lifecycle import repair_incomplete_packs
        from opencrab.pack.ownership import mark_pack_partial

        pid = begin_pack_creation(sql, alice, "planned-row")
        self._stale(sql, pid)
        part = begin_pack_creation(sql, alice, "planned-promote")
        assert mark_pack_partial(sql, part, alice) is True
        rec.reset()

        repair_incomplete_packs(
            sql, Graph(), Docs(), Vec(),
            older_than_seconds=0, apply=False, promote=part,
        )

        assert "lock-enter" not in rec.events

    def test_promote_window_starts_at_the_registry_read(self, sql, alice, rec):
        """Same rule as the anchor path: the block branches on the row it
        reads, so a re-read left outside the window decides nothing."""
        from opencrab.pack.lifecycle import repair_incomplete_packs
        from opencrab.pack.ownership import mark_pack_partial

        part = begin_pack_creation(sql, alice, "to-promote")
        assert mark_pack_partial(sql, part, alice) is True
        graph = Graph()
        graph.nodes[(ANCHOR_TYPE, anchor_node_id(part))] = {"pack_id": part}
        rec.reset()

        repair_incomplete_packs(
            sql, graph, Docs(), Vec(),
            older_than_seconds=0, apply=True, promote=part,
        )

        # Not absolute positions: the sweep runs first and probes the same
        # `partial` row on its report-only path, so there is legitimate
        # activity before the promote window opens. What matters is that the
        # registry read AND the probe the block branches on both fall inside
        # the window.
        enter = rec.events.index("lock-enter")
        leave = rec.events.index("lock-exit", enter)
        read = rec.events.index("get_pack", enter)
        assert enter < read < leave
        assert read < rec.events.index("probe", read) < leave


def _hold_lock_until(data_dir, ready_path, release_path):
    """Child-process body: take the real flock, then wait to be told to drop it."""
    import time

    from opencrab.locking import write_lock

    with write_lock(str(data_dir)):
        pathlib.Path(ready_path).write_text("held")
        for _ in range(600):
            if pathlib.Path(release_path).exists():
                break
            time.sleep(0.05)


class TestTheLockActuallyExcludes:
    """The gap #223 named: nothing proved the lock excludes anyone.

    The window tests above patch `write_lock` away, so they pin the span
    without ever taking a real flock. This one takes the real thing from a
    separate process. It has to be a process, not a thread -- `file_lock` is
    re-entrant within a thread and guarded by a per-path RLock, so a thread
    would either pass straight through or block somewhere that proves nothing
    about cross-process behaviour.
    """

    def test_apply_waits_for_a_concurrent_holder(self, tmp_path):
        import multiprocessing as mp
        import threading
        import time

        from opencrab.stores.sql_store import SQLStore

        # File-backed, not the in-memory fixture: the attempt runs on another
        # thread and every `:memory:` connection gets its own private database,
        # so the worker would not see this pack at all.
        sql = SQLStore(f"sqlite:///{tmp_path}/registry.db")
        alice = create_user(sql, "Alice")
        pack_id = create_pack(sql, alice, "contended")
        graph = Graph()
        b = OntologyBuilder(graph, Docs(), sql, vec=Vec())

        # Its own directory, not the suite-wide one: this test blocks a real
        # lock, and doing that on the shared file would stall other workers.
        lock_dir = tmp_path / "lockdir"
        lock_dir.mkdir()
        ready, release = tmp_path / "ready", tmp_path / "release"

        ctx = mp.get_context("fork")
        holder = ctx.Process(
            target=_hold_lock_until, args=(lock_dir, str(ready), str(release))
        )
        holder.start()
        try:
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.05)
            assert ready.exists(), "child never acquired the lock"

            done = threading.Event()

            def attempt():
                ensure_anchor(
                    sql, b, graph, Docs(), Vec(), pack_id,
                    apply=True, data_dir=str(lock_dir),
                )
                done.set()

            # daemon: if the assertion below fails we must not wedge the suite
            # on a thread parked in an untimed flock.
            t = threading.Thread(target=attempt, daemon=True)
            t.start()

            assert not done.wait(timeout=1.0), (
                "apply proceeded while another process held write.lock"
            )
            assert graph.upsert_calls == 0

            release.write_text("go")
            assert done.wait(timeout=10.0), "apply never resumed after release"
            assert graph.upsert_calls == 1
        finally:
            release.write_text("go")
            holder.join(timeout=10)
            if holder.is_alive():  # pragma: no cover - defensive
                holder.terminate()
                holder.join(timeout=5)


def test_store_unification_follows_the_builder_not_the_arguments(sql, alice):
    """`ensure_anchor` takes stores AND a builder, and they can differ.

    The write goes through the builder, so the plan has to look there too.
    Reading the argument stores instead is invisible in production -- the CLI
    passes the same objects to both -- which is exactly why it needs a test
    that hands them different ones. Here the builder's graph is the one
    holding a foreign claim on the slot; a plan that consulted the argument
    stores would see a clean slot and promise a write the guard refuses.
    """
    pack_id = create_pack(sql, alice, "split-view")
    clean = Graph()
    conflicted = _ForeignGraph(anchor_node_id(pack_id), "other-pack")
    builder = OntologyBuilder(conflicted, Docs(), sql, vec=Vec())

    result = ensure_anchor(
        sql, builder, clean, Docs(), Vec(), pack_id, apply=False
    )

    assert result["action"] == "blocked"
    assert result["reason"] == "foreign"


class TestCLIDryRunStillPlans:
    """The CLI half of the builder change (#224).

    Green on an unmodified tree by design -- it exists to catch the CLI
    reverting to building its builder only under `--apply`, which would turn
    every dry run into `skipped` and make the command useless for planning.
    """

    @pytest.fixture()
    def cli_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STORAGE_MODE", "local")
        from opencrab.config import get_settings

        get_settings.cache_clear()
        yield tmp_path
        get_settings.cache_clear()

    @pytest.fixture()
    def mock_vector_store(self, tmp_path):
        from _vec_helpers import build_vector_store

        store = build_vector_store("sqlite-vec", tmp_path, dim=32)
        with patch("opencrab.stores.factory.make_vector_store", return_value=store):
            yield store

    def test_dry_run_reports_a_plan_not_skipped(self, cli_env, mock_vector_store):
        from click.testing import CliRunner

        from opencrab.cli import main
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        owner = create_user(sql, "Operator")
        pack_id = create_pack(sql, owner, "planme")

        result = CliRunner().invoke(
            main, ["packs", "repair-anchors", "--pack-id", pack_id]
        )

        assert result.exit_code == 0, result.output
        # raw_decode, not loads: the command prints a trailing hint after the
        # JSON, same as its sibling CLI tests handle.
        brace = result.output.index("{")
        payload = json.JSONDecoder().raw_decode(result.output[brace:])[0]
        assert payload["apply"] is False
        assert payload["counts"]["would_create"] == 1
        assert payload["counts"]["skipped"] == 0


def test_sweep_decides_on_the_row_it_reads_inside_the_lock(sql, alice, monkeypatch):
    """The candidate list is gathered before the lock exists.

    So the row a sweep starts with can be out of date by the time it holds the
    window, and branching on it means probing and reporting against a state
    that has already moved. The CAS underneath would still refuse a bad
    transition -- this is not what makes the write safe -- but the window is
    supposed to cover the reading that picks the transition, not just the
    transition itself. Re-reading inside is also what makes this window follow
    the same rule as the other two, which both open at their registry read.
    """
    import opencrab.pack.ownership as ownership_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs

    pid = begin_pack_creation(sql, alice, "moved-row")
    TestRepairRegistryLockWindows._stale(sql, pid)

    real_write_lock = None
    import opencrab.locking as locking_mod
    real_write_lock = locking_mod.write_lock

    import contextlib

    @contextlib.contextmanager
    def promote_then_lock(*a, **kw):
        # Simulate a concurrent pack_create finishing in the gap between the
        # unlocked candidate query and this pass taking the window.
        ownership_mod.mark_pack_ready(sql, pid, alice)
        with real_write_lock(*a, **kw):
            yield

    monkeypatch.setattr(locking_mod, "write_lock", promote_then_lock)

    result = repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
    )

    row = next(r for r in result["rows"] if r["pack_id"] == pid)
    assert row["action"] == "skipped (row moved before the lock)"
    assert get_pack(sql, pid)["status"] == PACK_STATUS_READY


def test_a_slug_reused_under_the_lock_is_not_judged_by_the_old_row_s_age(
    sql, alice, monkeypatch
):
    """Still `creating` is not the same as still the same row.

    If the stale candidate goes away and the slug is re-reserved while this
    pass waits for the lock, the re-read finds a `creating` row again -- a
    brand-new one. Judging it with the previous row's timestamp would demote
    a pack somebody is still creating, which is the exact thing the age gate
    exists to prevent. So the gate has to run again on what was read.
    """
    import contextlib

    import opencrab.locking as locking_mod
    import opencrab.pack.ownership as ownership_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs

    pid = begin_pack_creation(sql, alice, "reused-slug")
    TestRepairRegistryLockWindows._stale(sql, pid)
    bob = create_user(sql, "Bob")
    real_lock = locking_mod.write_lock

    @contextlib.contextmanager
    def swap_the_row(*a, **kw):
        # Delete the aged row and re-reserve the same id, fresh.
        with sql._engine.begin() as conn:
            conn.execute(
                _sql_text("DELETE FROM packs WHERE pack_id = :p"), {"p": pid}
            )
        ownership_mod.begin_pack_creation(sql, bob, pid)
        with real_lock(*a, **kw):
            yield

    monkeypatch.setattr(locking_mod, "write_lock", swap_the_row)

    result = repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(), older_than_seconds=3600, apply=True
    )

    row = next(r for r in result["rows"] if r["pack_id"] == pid)
    assert row["action"] == "skipped (too recent)"
    assert "re-read under the lock" in row["reason"]
    assert get_pack(sql, pid)["status"] == "creating"
    # The report has to describe the row that is actually there. A
    # replacement can belong to someone else, and the gates above return
    # before the write, so the identity has to be adopted on the way in
    # rather than on the way out.
    assert row["owner_id"] == bob
