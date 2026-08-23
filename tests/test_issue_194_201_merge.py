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
from unittest.mock import MagicMock, patch

import pytest

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
    from opencrab.mcp.tools.pack import pack_ingest
    from opencrab.stores.sql_store import SQLStore
    from test_tools_handlers_direct import _base_ctx

    sql = SQLStore("sqlite:///:memory:")
    create_pack(sql, "test-user", "upstream")
    pack_id = create_pack(sql, "test-user", "mine", forked_from="upstream")

    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    ctx = _base_ctx(sql=sql, builder=builder)

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
        payload = json.loads(result.output[result.output.index("{"):].split("\n\n")[0])
        assert payload["apply"] is True
        assert payload["counts"]["created"] == 1

        from opencrab.cli import _optional_store

        graph = _optional_store(get_settings(), "graph")
        written = graph.get_node(ANCHOR_TYPE, anchor_node_id(pack_id))
        assert written is not None
        assert written["forked_from"] == "upstream"
        assert get_pack(sql, pack_id)["status"] == PACK_STATUS_READY
