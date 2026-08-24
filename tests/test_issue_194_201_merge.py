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
    PACK_STATUS_PARTIAL,
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
        payload, end = json.JSONDecoder().raw_decode(result.output[brace:])
        assert payload["apply"] is True
        assert payload["counts"]["created"] == 1
        # `raw_decode` reads a prefix, so on its own it would ignore anything
        # printed after the JSON. An apply run prints nothing after it -- the
        # dry-run hint is the other branch.
        assert result.output[brace + end:].strip() == ""

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
        # JSON, same as its sibling CLI tests handle. Reading a prefix means
        # whatever follows goes unread, so say what is allowed to follow.
        brace = result.output.index("{")
        payload, end = json.JSONDecoder().raw_decode(result.output[brace:])
        assert payload["apply"] is False
        assert payload["counts"]["would_create"] == 1
        assert payload["counts"]["skipped"] == 0
        rest = result.output[brace + end:].strip()
        assert rest.startswith("Dry-run only."), rest


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


def test_a_moved_row_is_reported_whole_not_half(sql, alice, monkeypatch):
    """The report must not mix two rows.

    If the slug is re-reserved by someone else and the replacement is not
    `creating`, this pass bails at the first gate. That gate reports, so it
    has to report the row that is actually there -- owner and status both.
    Taking the status from the re-read while leaving the owner from before
    the lock produces a line describing a pack that never existed.
    """
    import contextlib

    import opencrab.locking as locking_mod
    import opencrab.pack.ownership as ownership_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs

    pid = begin_pack_creation(sql, alice, "handed-over")
    TestRepairRegistryLockWindows._stale(sql, pid)
    bob = create_user(sql, "Bob")
    real_lock = locking_mod.write_lock

    @contextlib.contextmanager
    def hand_the_slug_to_bob(*a, **kw):
        with sql._engine.begin() as conn:
            conn.execute(
                _sql_text("DELETE FROM packs WHERE pack_id = :p"), {"p": pid}
            )
        ownership_mod.begin_pack_creation(sql, bob, pid)
        ownership_mod.mark_pack_partial(sql, pid, bob)
        with real_lock(*a, **kw):
            yield

    monkeypatch.setattr(locking_mod, "write_lock", hand_the_slug_to_bob)

    result = repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
    )

    row = next(r for r in result["rows"] if r["pack_id"] == pid)
    assert row["action"] == "skipped (row moved before the lock)"
    assert row["status"] == "partial"
    assert row["owner_id"] == bob, "reported the deleted row's owner"
    # This verdict is itself a claim about time ("before the lock"), so the
    # row has to say when it was looked at. Without it the only timestamp is
    # the pass's opening stamp, which after a wait can predate the very row
    # it is describing.
    assert "checked_at" in row, "a time-based verdict with no time on it"


class TestNothingPreLockLeaksIntoTheWindow:
    """Fifth and sixth of the same family, kept together on purpose.

    Everything this branch got wrong repeatedly had one shape: a value taken
    before the lock, used for a decision made inside it. The row was the
    obvious one. The clock and the running tally are the same mistake wearing
    different clothes.
    """

    def test_the_clock_is_read_inside_the_window_too(self, sql, alice, monkeypatch):
        """`now` is stamped before the candidate query.

        Wait long enough on the lock and a legitimately fresh row is dated
        after that stamp, so the re-read gate calls it "in the future" and
        reports an unknown age for a row whose age is perfectly knowable.
        """
        import contextlib
        import time

        import opencrab.locking as locking_mod
        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.lifecycle import repair_incomplete_packs

        pid = begin_pack_creation(sql, alice, "slow-wait")
        TestRepairRegistryLockWindows._stale(sql, pid)
        real_lock = locking_mod.write_lock

        @contextlib.contextmanager
        def dawdle_then_replace(*a, **kw):
            time.sleep(1.2)  # outlive the pre-lock clock stamp
            with sql._engine.begin() as conn:
                conn.execute(
                    _sql_text("DELETE FROM packs WHERE pack_id = :p"), {"p": pid}
                )
            ownership_mod.begin_pack_creation(sql, alice, pid)
            with real_lock(*a, **kw):
                yield

        monkeypatch.setattr(locking_mod, "write_lock", dawdle_then_replace)

        result = repair_incomplete_packs(
            sql, Graph(), Docs(), Vec(), older_than_seconds=3600, apply=True
        )

        row = next(r for r in result["rows"] if r["pack_id"] == pid)
        assert row["action"] == "skipped (too recent)", (
            "a fresh row was judged against a clock read before the lock"
        )
        # Deciding by the locked clock is only half of it: the operator has to
        # be able to reconstruct the decision. The run-level `checked_at` is
        # the pre-query stamp and here it predates the row's own
        # `updated_at` -- so the row has to carry the clock that judged it.
        assert "checked_at" in row, "no per-row clock to reconstruct the decision"
        assert row["checked_at"] > result["checked_at"], (
            "row clock should postdate the pass's starting stamp after a wait"
        )
        # and it must not predate the row it judged, which was the symptom
        assert row["checked_at"] >= str(get_pack(sql, pid)["updated_at"])[:10]

    def test_the_status_tally_follows_the_row_that_was_adopted(
        self, sql, alice, monkeypatch
    ):
        """The summary and the row detail have to agree.

        `counts` is bumped from the pre-lock scan. If the re-read adopts a
        different status, a tally left behind says `creating: 1` about a row
        the same report calls `partial`.
        """
        import contextlib

        import opencrab.locking as locking_mod
        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.lifecycle import repair_incomplete_packs

        pid = begin_pack_creation(sql, alice, "tally")
        TestRepairRegistryLockWindows._stale(sql, pid)
        real_lock = locking_mod.write_lock

        @contextlib.contextmanager
        def demote_it(*a, **kw):
            ownership_mod.mark_pack_partial(sql, pid, alice)
            with real_lock(*a, **kw):
                yield

        monkeypatch.setattr(locking_mod, "write_lock", demote_it)

        result = repair_incomplete_packs(
            sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
        )

        row = next(r for r in result["rows"] if r["pack_id"] == pid)
        assert row["status"] == "partial"
        assert result["counts"]["partial"] == 1
        assert result["counts"]["creating"] == 0


def test_sweep_data_dir_reaches_the_lock(sql, alice, rec):
    """The parameter exists so a caller whose stores are elsewhere can lock
    the right file. Accepting it and dropping it looks identical from the
    signature."""
    from opencrab.pack.lifecycle import repair_incomplete_packs

    TestRepairRegistryLockWindows._stale(sql, begin_pack_creation(sql, alice, "dd"))
    rec.reset()

    repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(),
        older_than_seconds=0, apply=True, data_dir="/tmp/o223-sweep",
    )

    assert rec.dirs and all(d == "/tmp/o223-sweep" for d in rec.dirs)

    # And the default path is the one the CLI actually takes, so pin it too:
    # passing `None` has to reach the lock as `None`, not as some substitute
    # directory chosen on the way down.
    TestRepairRegistryLockWindows._stale(sql, begin_pack_creation(sql, alice, "dd2"))
    rec.reset()

    repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
    )

    assert rec.dirs and all(d is None for d in rec.dirs)


def test_a_row_deleted_under_the_lock_is_not_reported_as_present(
    sql, alice, monkeypatch
):
    """When there is no current row, say so rather than showing the old one.

    Two reviewers read the same branch differently -- one called the retained
    `owner_id`/`status` stale, the other called them an honest description of
    the row that was examined. Both readings are available because the fields
    look like present facts either way. Marking them as scan-time settles it,
    and the status tally drops the row because that bucket counts what the
    registry holds.
    """
    import contextlib

    import opencrab.locking as locking_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs

    pid = begin_pack_creation(sql, alice, "vanishes")
    TestRepairRegistryLockWindows._stale(sql, pid)
    real_lock = locking_mod.write_lock

    @contextlib.contextmanager
    def delete_it(*a, **kw):
        with sql._engine.begin() as conn:
            conn.execute(_sql_text("DELETE FROM packs WHERE pack_id = :p"), {"p": pid})
        with real_lock(*a, **kw):
            yield

    monkeypatch.setattr(locking_mod, "write_lock", delete_it)

    result = repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(), older_than_seconds=0, apply=True
    )

    row = next(r for r in result["rows"] if r["pack_id"] == pid)
    assert row["row_gone"] is True
    assert "status" not in row and "owner_id" not in row
    assert row["scanned_status"] == "creating"
    assert row["scanned_owner_id"] == alice
    # examined, but no longer held by the registry
    assert result["counts"]["rows_examined"] == 1
    assert result["counts"]["creating"] == 0


def test_no_path_returns_before_the_row_is_recorded():
    """Structural tripwire over the source of the sweep's row window.

    Status, stated plainly because two earlier versions of this docstring
    overclaimed: this is NOT a proof. It recognises shapes we have seen. The
    soundness comes from
    `test_every_row_the_lock_touched_is_judged_and_written_by_what_it_re_read`,
    which runs a pass and reads the result. Nine rounds of adversarial review
    produced 62 ways past some version of this file's gates; of those, this
    check is the only thing that catches exactly two -- renaming the window,
    and opening a third one. Everything else here is caught by the behaviour
    gate as well, and is kept because a named structural failure is faster to
    read than a fixture that stopped covering something.

    What it does NOT catch (measured, not guessed) -- all of these are killed
    by the behaviour gate instead:
      - writes to `entry` that are not `Assign`: `pop`, `del`, `update`,
        `AnnAssign`, rebinding `entry`, overwriting inside the `append` call;
      - laundering the pre-lock clock through an alias or a tuple assignment;
      - distorting the stamp's VALUE while keeping its shape;
      - deciding on one clock and reporting another;
      - deciding with a distorted threshold;
      - swapping which row a branch reads;
      - passing the pre-lock owner to a compare-and-set;
      - releasing the lock just before a transition.

    Scope: the SWEEP row window. `ensure_anchor` and the `--promote` window
    have no per-row stamp to guard.
    """
    import ast
    import inspect

    from opencrab.pack import lifecycle

    tree = ast.parse(inspect.getsource(lifecycle))
    defs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "repair_incomplete_packs"
    ]
    assert len(defs) == 1, (
        f"expected exactly one `repair_incomplete_packs` in the module, found "
        f"{len(defs)} at lines {[d.lineno for d in defs]} -- a later definition "
        f"shadows the one this check reads, so it would be checking dead code"
    )
    fn = defs[0]

    windows = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.With)
        and any(getattr(i.optional_vars, "id", "") == "_row_lock" for i in n.items)
    ]
    assert len(windows) == 1, (
        f"expected exactly one `_row_lock` window in the function, found "
        f"{len(windows)}; renaming or duplicating it leaves this check reading "
        f"the wrong block"
    )
    window = windows[0]

    locks = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "write_lock"
    ]
    assert len(locks) == 2, (
        f"expected exactly two `write_lock` call sites in the function -- the "
        f"sweep row window and the promote window -- found {len(locks)} at "
        f"lines {[c.lineno for c in locks]}. A third window is a lock nothing "
        f"here was asked about."
    )

    def stamps(node):
        return [
            n for n in ast.walk(node)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and getattr(t.value, "id", "") == "entry"
                    and getattr(getattr(t, "slice", None), "value", None) == "checked_at"
                    for t in n.targets)
        ]

    all_stamps = stamps(fn)
    assert len(all_stamps) == 1, (
        f"expected exactly one `entry['checked_at']` assignment in the "
        f"function, found {len(all_stamps)} at lines "
        f"{[a.lineno for a in all_stamps]}. A second one can overwrite the "
        f"first with a clock read somewhere else."
    )
    stamp = all_stamps[0]

    # The stamp has to be a DIRECT statement of the window's `if apply:` body.
    # Asking instead whether some exit precedes it was the shape three earlier
    # versions used, and it misses the case with no exit at all: nest the
    # stamp under any condition and the paths where that condition is false
    # simply skip it.
    apply_if = next(
        (st for st in window.body
         if isinstance(st, ast.If) and getattr(st.test, "id", "") == "apply"),
        None,
    )
    assert apply_if is not None, "the row window no longer opens with `if apply:`"
    assert not apply_if.orelse, (
        "`if apply:` grew an else branch -- re-derive this check before "
        "silencing it"
    )
    try:
        stamp_at = apply_if.body.index(stamp)
    except ValueError:
        raise AssertionError(
            f"the stamp at line {stamp.lineno} is not a direct statement of the "
            f"window's `if apply:` body. Nesting it under a condition means "
            f"some path through the window skips recording the row."
        ) from None

    for st in apply_if.body[:stamp_at]:
        bad = [
            n for n in ast.walk(st)
            if isinstance(n, (ast.Continue, ast.Return, ast.Break, ast.Raise, ast.Assert))
        ]
        assert not bad, (
            f"a path exits the row window at line(s) {[b.lineno for b in bad]} "
            f"before the row is stamped. Record when the row was read before "
            f"any branch can report on it."
        )

    names = {n.id for n in ast.walk(stamp.value) if isinstance(n, ast.Name)}
    assert "scan_started_at" not in names, (
        "the row is stamped with the pre-lock clock, which is the defect the "
        "stamp exists to prevent"
    )
    assert "now_locked" in names, (
        f"expected the in-window clock in the stamp, got {names}"
    )

    # ...and that name has to be a clock read here, not an alias for the
    # pre-lock one. Checking the stamp expression alone let
    # `now_locked = scan_started_at` through.
    binds = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "now_locked" for t in n.targets)
    ]
    assert binds, "`now_locked` is never assigned"
    for b in binds:
        bn = {n.id for n in ast.walk(b.value) if isinstance(n, ast.Name)}
        assert "scan_started_at" not in bn, (
            f"`now_locked` at line {b.lineno} derives from the pre-lock clock"
        )
        assert (isinstance(b.value, ast.Call)
                and getattr(b.value.func, "attr", "") == "now"), (
            f"`now_locked` at line {b.lineno} is not a fresh clock read"
        )


# ---------------------------------------------------------------------------
# The gate that carries the rule (#223/#224).
#
# Eight defects on `repair_incomplete_packs` had one shape: a value obtained
# before the lock, used inside it. Four of the eight were introduced by the
# fix for the previous one. Nine rounds of adversarial review produced 62 ways
# past the checks written for it, and the family moved outward one layer each
# time -- what the row REPORTED, then what the decision COMPUTED, then which
# BRANCH it took, then which ROW it read, then what the WRITE was given, then
# which of the three write sites, then WHEN the write happened.
#
# Asking the source what shape it has could not keep up: there is always
# another way to write the same defect. Asking a real pass what it DID can,
# because the fixture plants facts that only become true while the lock is
# held. Code that reads a pre-lock value takes a different branch, or hands
# the registry an owner it will not match. Values can be forged; branches and
# the registry's final state cannot.
# ---------------------------------------------------------------------------

# Large enough that a PROPORTIONAL distortion of the age threshold exceeds the
# fixture's 0.75s pinch and changes a verdict. An additive distortion smaller
# than the pinch still does not -- that is recorded as a limit rather than
# chased with a sub-second fixture whose result would depend on how fast the
# machine is.
_GATE_THRESHOLD = 600

_MOVED = "skipped (row moved before the lock)"
_UNKNOWN = "skipped (unknown age)"
_RECENT = "skipped (too recent)"
_UNVER = "skipped (unverifiable)"


class _GraphThatCanFail(Graph):
    """A graph store that is up, except for the one node named."""

    def __init__(self):
        super().__init__()
        self.raise_for: set[str] = set()

    def get_node(self, node_type, node_id):
        if node_id in self.raise_for:
            raise RuntimeError("probe cannot be answered for this one")
        return super().get_node(node_type, node_id)


def _write_cols(sql, pack_id, **cols):
    from sqlalchemy import text as _t

    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with sql._engine.begin() as conn:
        conn.execute(
            _t(f"UPDATE packs SET {sets} WHERE pack_id = :p"), {**cols, "p": pack_id}
        )


def _ts(dt):
    """Sub-second precision, deliberately.

    A row written during the lock wait has to land strictly between the scan
    clock and the in-lock clock, and a second-truncated timestamp cannot
    express that on a pass this fast. `_parse_updated_at` goes through
    `fromisoformat`, which reads the fractional part.
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def test_every_row_the_lock_touched_is_judged_and_written_by_what_it_re_read(
    sql, alice, monkeypatch
):
    """One pass, every window path, and three ways to catch a stale read.

    Each row plants a fact that becomes true only while its lock is held.
    Reading the scan row instead of the re-read one therefore shows up as a
    DIFFERENT ACTION, not as a subtly wrong number -- and the compare-and-set
    that follows shows up as a row the registry never moved.

    Limits, measured rather than assumed:
      - additive distortion of the in-lock threshold smaller than the 0.75s
        pinch is invisible here. Its harm is bounded by that same 0.75s, and
        closing it would need a sub-second fixture that depends on machine
        speed. A proportional distortion is caught -- that is what the 600s
        threshold buys.
      - fields this fixture does not change during the lock wait are not
        covered. Four such (the anchor probe, the two status tallies, the
        `status` field, and the promote window's reads) are each caught by
        tests above; a new one needs the same check.
      - it does not look at `apply=False`, which takes no lock and re-reads
        nothing.
    """
    import re
    import time
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _t

    import opencrab.pack.lifecycle as lifecycle_mod
    import opencrab.pack.ownership as ownership_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs
    from opencrab.pack.ownership import anchor_node_id

    graph = _GraphThatCanFail()
    real_get_pack = ownership_mod.get_pack

    # Every clock the pass reads, in order. The stamp has to BE one of these
    # -- the one taken right after this row's in-lock re-read -- not merely a
    # value that sorts after it. A bound admits a later or skewed clock.
    clock_log: list[datetime] = []

    class _LoggedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            t = datetime.now(tz)
            clock_log.append(t)
            return t

    monkeypatch.setattr(lifecycle_mod, "datetime", _LoggedClock)

    def stale(pack_id):
        _write_cols(
            sql, pack_id,
            updated_at=_ts(datetime.now(UTC) - timedelta(seconds=7200)),
        )
        return pack_id

    def mk(name):
        return stale(begin_pack_creation(sql, alice, name))

    def with_anchor(pack_id):
        graph.nodes[(ANCHOR_TYPE, anchor_node_id(pack_id))] = {"pack_id": pack_id}
        return pack_id

    gone = mk("gone")
    moved = mk("moved")
    future = mk("future")
    garbage = mk("garbage")
    straddle = mk("straddle")
    nearfuture = mk("nearfuture")
    recent_inlock = mk("recentinlock")
    boundary = with_anchor(mk("boundary"))
    nearmiss = with_anchor(mk("nearmiss"))
    forked_late = with_anchor(mk("forkedlate"))
    bob = create_user(sql, "Bob")
    owner_promote = with_anchor(mk("ownermoved"))
    owner_absent = mk("ownerabsent")
    owner_fork = with_anchor(mk("ownerfork"))
    forked = mk("forked")
    _write_cols(sql, forked, forked_from="elsewhere")
    present = with_anchor(mk("present"))
    absent = mk("absent")
    unver = mk("unver")
    graph.raise_for.add(anchor_node_id(unver))

    # Controls: rows the window never sees.
    partial_row = stale(begin_pack_creation(sql, alice, "partialrow"))
    _write_cols(sql, partial_row, status=PACK_STATUS_PARTIAL)
    too_recent = begin_pack_creation(sql, alice, "toorecent")
    _write_cols(sql, too_recent, updated_at=_ts(datetime.now(UTC)))
    no_date = begin_pack_creation(sql, alice, "nodate")
    _write_cols(sql, no_date, updated_at="not-a-date")
    controls = {partial_row, too_recent, no_date}

    # What each row's own window must conclude. Compared as a mapping, not as
    # a set: two paths produce `demote` and two produce the same skip string,
    # so set equality would let a row take a sibling's branch unnoticed.
    expected = {
        gone: _MOVED,
        moved: _MOVED,
        future: _UNKNOWN,
        garbage: _UNKNOWN,
        # Written after the scan and before the in-lock read: the scan clock
        # calls it "dated in the future", the in-lock clock calls it new.
        straddle: _RECENT,
        # Dated a little ahead: only a gate whose horizon is the in-lock clock
        # calls this unjudgeable.
        nearfuture: _UNKNOWN,
        recent_inlock: _RECENT,
        # Just under the threshold when written, carried over it by the wait.
        # Which clock the age gate uses decides between two ACTIONS here.
        boundary: "promote",
        # Its twin without the wait. Together they pin the threshold from
        # both sides: raise it and `boundary` moves, lower it and this does.
        nearmiss: _RECENT,
        # Becomes a fork only under the lock. The fork guard has to read the
        # row it re-read, or it promotes a copy still being made.
        forked_late: "demote",
        # Change hands under the lock. Deciding on the re-read row is not
        # enough -- the transition has to be ATTEMPTED with the owner just
        # adopted, or the compare-and-set matches nothing. One row would only
        # cover the promote write; the registry pins the owner in all three.
        owner_promote: "promote",
        owner_absent: "demote",
        owner_fork: "demote",
        forked: "demote",
        present: "promote",
        absent: "demote",
        unver: _UNVER,
        partial_row: "report only (no automatic remediation)",
        too_recent: _RECENT,
        no_date: _UNKNOWN,
    }

    def delete(pack_id):
        with sql._engine.begin() as conn:
            conn.execute(
                _t("DELETE FROM packs WHERE pack_id = :p"), {"p": pack_id}
            )

    # Ages this pass must report for the rows it calls too recent, kept so the
    # printed number can be recomputed from the clock that judged them.
    written_at: dict[str, datetime] = {}

    def age_to(pack_id, seconds):
        t = datetime.now(UTC) - timedelta(seconds=seconds)
        written_at[pack_id] = t
        _write_cols(sql, pack_id, updated_at=_ts(t))

    mutations = {
        gone: lambda: delete(gone),
        moved: lambda: _write_cols(sql, moved, status=PACK_STATUS_PARTIAL),
        future: lambda: _write_cols(
            sql, future, updated_at=_ts(datetime.now(UTC) + timedelta(hours=1))
        ),
        garbage: lambda: _write_cols(sql, garbage, updated_at="not-a-date"),
        straddle: lambda: age_to(straddle, 0),
        nearfuture: lambda: _write_cols(
            sql, nearfuture,
            updated_at=_ts(datetime.now(UTC) + timedelta(seconds=20)),
        ),
        recent_inlock: lambda: age_to(recent_inlock, _GATE_THRESHOLD / 2),
        boundary: lambda: age_to(boundary, _GATE_THRESHOLD - 0.75),
        nearmiss: lambda: age_to(nearmiss, _GATE_THRESHOLD - 0.75),
        forked_late: lambda: _write_cols(sql, forked_late, forked_from="elsewhere"),
        owner_promote: lambda: _write_cols(sql, owner_promote, owner_id=bob),
        owner_absent: lambda: _write_cols(sql, owner_absent, owner_id=bob),
        owner_fork: lambda: _write_cols(
            sql, owner_fork, owner_id=bob, forked_from="elsewhere"
        ),
    }
    # Two waits, for two reasons. `boundary`'s carries it over the threshold
    # -- 0.8s against a 0.75s shortfall puts the crossing on the sleep floor
    # rather than on machine speed. `recentinlock`'s opens a gap between the
    # scan clock and the in-lock one wider than the reason string's rounding,
    # so an age computed from the wrong clock cannot hide in the tolerance.
    slow = {boundary: 0.8, recent_inlock: 1.5}

    judged_at = {}

    def spy_get_pack(sql_, pack_id, *a, **kw):
        run = mutations.pop(pack_id, None)
        if run is not None:
            run()
        time.sleep(slow.get(pack_id, 0.005))
        out = real_get_pack(sql_, pack_id, *a, **kw)
        # `now_locked` is the first clock read after this returns, so the
        # log's length right here names it exactly.
        judged_at[pack_id] = len(clock_log)
        return out

    monkeypatch.setattr(ownership_mod, "get_pack", spy_get_pack)

    result = repair_incomplete_packs(
        sql, graph, Docs(), Vec(), older_than_seconds=_GATE_THRESHOLD, apply=True
    )
    rows = {r["pack_id"]: r for r in result["rows"]}

    assert set(rows) == set(expected)
    assert {p: r.get("action") for p, r in rows.items()} == expected

    # The tally has to agree with the verdicts printed beside it.
    verdicts = list(expected.values())
    counts = result["counts"]
    assert counts["rows_examined"] == len(expected)
    assert counts["promoted"] == verdicts.count("promote")
    assert counts["demoted"] == verdicts.count("demote")
    assert counts["skipped"] == sum(v.startswith("skipped") for v in verdicts)

    # A verdict the pass did not carry out is not a verdict.
    with sql._engine.begin() as conn:
        final = dict(conn.execute(_t("SELECT pack_id, status FROM packs")).all())
    for pack_id, action in expected.items():
        if action not in ("promote", "demote"):
            continue
        assert rows[pack_id].get("applied") is True, (
            f"row {pack_id} reports {action} with "
            f"applied={rows[pack_id].get('applied')!r} -- the transition was "
            f"attempted with something the registry did not match"
        )
        want = PACK_STATUS_READY if action == "promote" else PACK_STATUS_PARTIAL
        assert final[pack_id] == want, (
            f"row {pack_id} reports {action} but the registry says "
            f"{final[pack_id]!r}"
        )

    for pack_id, row in rows.items():
        if pack_id in controls:
            assert "checked_at" not in row, (
                f"{row['action']} row never reached the lock but carries a stamp"
            )
            continue
        assert "checked_at" in row, (
            f"row {pack_id} ({row['action']}) reached the lock and carries no stamp"
        )
        i = judged_at[pack_id]
        assert i < len(clock_log), (
            f"row {pack_id} ({row['action']}) read no clock after its in-lock re-read"
        )
        judged = clock_log[i]
        assert row["checked_at"] == judged.isoformat(), (
            f"row {pack_id} ({row['action']}) reports {row['checked_at']} but "
            f"the clock read inside its window was {judged.isoformat()}"
        )

        if row["action"] == _RECENT:
            printed = float(re.search(r"age (-?\d+)s", row["reason"]).group(1))
            # A row called too recent must read as too recent. Reporting the
            # in-lock age beside a verdict reached on another clock produces
            # "age 601s < threshold 600s".
            assert printed < _GATE_THRESHOLD, (
                f"row {pack_id} is called too recent while reporting age "
                f"{printed}s against threshold {_GATE_THRESHOLD}s -- the verdict "
                f"and the number beside it came from different clocks"
            )
            if pack_id in written_at:
                from_judging_clock = (judged - written_at[pack_id]).total_seconds()
                assert abs(printed - from_judging_clock) <= 0.75, (
                    f"row {pack_id} reported age {printed}s but the clock inside "
                    f"its window puts it at {from_judging_clock:.2f}s -- the age "
                    f"gate ran off a different clock than the one that stamped it"
                )


def test_every_registry_transition_happens_under_a_lock(sql, alice, monkeypatch):
    """Containment for all three writes, not one.

    The first version of this spied `mark_pack_partial` only. That pins the
    demote branch and leaves the promote CAS and `--promote` free to move
    outside the window: releasing the lock one line before either of them
    passed the whole suite. Asserting that all three were REACHED is half the
    check -- covering one site and calling the axis closed is the mistake this
    test exists to make impossible.
    """
    import contextlib

    import opencrab.locking as locking_mod
    import opencrab.pack.ownership as ownership_mod
    from opencrab.pack.lifecycle import repair_incomplete_packs
    from opencrab.pack.ownership import anchor_node_id

    graph = Graph()
    events: list[str] = []
    real_lock = locking_mod.write_lock

    @contextlib.contextmanager
    def spy_lock(*a, **kw):
        events.append("lock-enter")
        with real_lock(*a, **kw):
            yield
        events.append("lock-exit")

    monkeypatch.setattr(locking_mod, "write_lock", spy_lock)
    for name in ("mark_pack_partial", "mark_pack_ready", "promote_partial_pack"):
        real = getattr(ownership_mod, name)

        def spy(*a, _real=real, _name=name, **kw):
            events.append(f"write:{_name}")
            return _real(*a, **kw)

        monkeypatch.setattr(ownership_mod, name, spy)

    to_demote = begin_pack_creation(sql, alice, "demote-me")
    TestRepairRegistryLockWindows._stale(sql, to_demote)
    to_promote = begin_pack_creation(sql, alice, "promote-me")
    TestRepairRegistryLockWindows._stale(sql, to_promote)
    graph.nodes[(ANCHOR_TYPE, anchor_node_id(to_promote))] = {"pack_id": to_promote}
    explicit = begin_pack_creation(sql, alice, "explicit")
    assert ownership_mod.mark_pack_partial(sql, explicit, alice) is True
    graph.nodes[(ANCHOR_TYPE, anchor_node_id(explicit))] = {"pack_id": explicit}
    events.clear()

    repair_incomplete_packs(
        sql, graph, Docs(), Vec(),
        older_than_seconds=0, apply=True, promote=explicit,
    )

    depth = 0
    reached = set()
    for event in events:
        if event == "lock-enter":
            depth += 1
        elif event == "lock-exit":
            depth -= 1
        else:
            reached.add(event)
            assert depth > 0, f"{event} ran with no lock held -- events: {events}"
    assert reached == {
        "write:mark_pack_partial",
        "write:mark_pack_ready",
        "write:promote_partial_pack",
    }, f"not every transition was exercised, so not every one was checked: {sorted(reached)}"


def test_the_window_has_no_verdict_the_fixture_never_reaches():
    """Count the verdicts the row window can produce, so adding one is loud.

    The gate above is only as complete as its fixture. This does not enforce
    that -- three measured ways past it: widening an existing branch's
    condition keeps the count, writing through an alias is not seen, and code
    outside the window is out of scope (the structural check counts the lock
    sites for that). It is a tripwire, and its job is to make someone look.
    """
    import ast
    import inspect

    from opencrab.pack import lifecycle

    tree = ast.parse(inspect.getsource(lifecycle))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "repair_incomplete_packs"
    )
    window = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.With)
        and any(getattr(i.optional_vars, "id", "") == "_row_lock" for i in n.items)
    )

    sites = [
        n for n in ast.walk(window)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Subscript)
                and getattr(t.value, "id", "") == "entry"
                and getattr(getattr(t, "slice", None), "value", None) == "action"
                for t in n.targets)
    ] + [
        n for n in ast.walk(window)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "update"
        and getattr(getattr(n.func, "value", None), "id", "") == "entry"
        and any(k.arg == "action" for k in n.keywords)
    ]
    assert len(sites) == 7, (
        f"the row window sets `entry['action']` in {len(sites)} places, not 7. "
        f"If a verdict was added, add a row to "
        f"`test_every_row_the_lock_touched_is_judged_and_written_by_what_it_re_read` "
        f"that reaches it, then update this count."
    )


def test_promote_says_why_when_there_is_no_such_pack(sql, alice):
    """Every other rejection carries a reason; this one used to carry none.

    An operator handed back only `rejected (no such pack)` cannot tell a
    mistyped slug from a row that went away while the pass waited for its
    lock -- and those call for opposite next steps.
    """
    from opencrab.pack.lifecycle import repair_incomplete_packs

    pid = begin_pack_creation(sql, alice, "exists")
    TestRepairRegistryLockWindows._stale(sql, pid)

    result = repair_incomplete_packs(
        sql, Graph(), Docs(), Vec(),
        older_than_seconds=0, apply=False, promote="no-such-slug",
    )

    assert result["promote_result"]["action"] == "rejected (no such pack)"
    assert "no-such-slug" in result["promote_result"]["reason"]


class TestSweepPlanMatchesApplyForForks:
    """#224, on the sweep's own fork branch.

    The `--promote` path has its own class for this. The sweep's fork guard
    did not, and a guard that consults `apply` there would let a dry run
    print `promote` for a row an `--apply` demotes -- the exact disagreement
    #224 is about, one branch over from where it was found.
    """

    @staticmethod
    def _forked_creating_with_anchor(sql, alice, graph, name):
        from opencrab.pack.ownership import anchor_node_id

        pid = begin_pack_creation(sql, alice, name)
        TestRepairRegistryLockWindows._stale(sql, pid)
        from sqlalchemy import text as _t

        with sql._engine.begin() as conn:
            conn.execute(
                _t("UPDATE packs SET forked_from = :f WHERE pack_id = :p"),
                {"f": "upstream", "p": pid},
            )
        graph.nodes[(ANCHOR_TYPE, anchor_node_id(pid))] = {"pack_id": pid}
        return pid

    def test_a_dry_run_plans_the_demote_an_apply_performs(self, sql, alice):
        from opencrab.pack.lifecycle import repair_incomplete_packs

        graph = Graph()
        pid = self._forked_creating_with_anchor(sql, alice, graph, "forkplan")

        planned = repair_incomplete_packs(
            sql, graph, Docs(), Vec(), older_than_seconds=0, apply=False
        )
        plan_row = next(r for r in planned["rows"] if r["pack_id"] == pid)

        applied = repair_incomplete_packs(
            sql, graph, Docs(), Vec(), older_than_seconds=0, apply=True
        )
        apply_row = next(r for r in applied["rows"] if r["pack_id"] == pid)

        # A present anchor would say `promote` for any other row. The fork
        # guard overrides that in BOTH modes or in neither.
        assert plan_row["action"] == "demote"
        assert apply_row["action"] == plan_row["action"]
        assert apply_row["applied"] is True
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL
