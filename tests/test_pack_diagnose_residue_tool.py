"""``pack_diagnose_residue`` MCP tool (#407) -- authorization boundary and
end-to-end response shape.

Follows ``tests/test_packs_registry.py``'s established pattern: a real
in-memory ``SQLStore`` wired into ``ctx["sql"]`` (so the real
``readable_pack_ids`` SQL predicate runs), with the other four context
stores as ``MagicMock``s (patched via ``opencrab.mcp.tools._get_context``,
same patch point ``tests/test_packs_registry.py``/
``tests/test_tools_handlers_direct.py`` use).

The authorization-boundary tests assert BOTH the response shape AND zero
backend count calls -- the two evidence pieces #407's design requires for
"existence must not leak" (#143 invariant 7): a uniform response alone
would not catch a version that returns the right dict shape while still
leaking backend query timing/side effects.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.pack.ownership import (
    begin_pack_creation,
    create_pack,
    mark_pack_partial,
    set_visibility,
)
from tests._pack_fixtures import ensure_test_user

ALICE = Principal(user_id="alice", is_local=True, disabled=False)
BOB = Principal(user_id="bob", is_local=True, disabled=False)

NOT_CANDIDATE_RESPONSE_SHAPE = {"classification": "not_candidate", "axes": None}


class _FakeVec:
    """Minimal chroma-shaped double for ``_vec_backend``'s dispatch
    (``opencrab.pack.fork``): ``available=True`` plus a ``._collection``
    exposing ``.get(where=..., limit=..., include=...)``.

    A bare ``MagicMock()`` is NOT safe here: ``_vec_backend`` picks its
    backend kind off ``getattr(vec, "_conn", None)`` truthiness first, and
    an unconfigured ``MagicMock`` auto-vivifies ``._conn`` as a truthy
    child mock -- which ``_vec_backend`` would then misread as the "sql"
    backend kind, silently skipping the "chroma" branch (and this test's
    ``.get`` configuration) entirely.
    """

    def __init__(self, ids: list[str] | None = None):
        self.available = True
        self._collection = MagicMock()
        self._collection.get.return_value = {"ids": list(ids or [])}


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


def _base_ctx(sql, **overrides):
    ctx = {
        "neo4j": MagicMock(),
        "chroma": _FakeVec(),
        "mongo": MagicMock(),
        "sql": sql,
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    ctx.update(overrides)
    return ctx


def _call(sql, ctx, principal, pack_id):
    from opencrab.mcp.tools import dispatch_tool

    with patch("opencrab.mcp.tools._get_context") as mock_ctx:
        mock_ctx.return_value = ctx
        with principal_scope(principal):
            return dispatch_tool("pack_diagnose_residue", {"pack_id": pack_id})


def _assert_zero_backend_calls(ctx):
    ctx["neo4j"].count_exported_nodes_scoped.assert_not_called()
    ctx["neo4j"].count_exported_edges_scoped.assert_not_called()
    ctx["mongo"].count_sources_scoped.assert_not_called()
    ctx["mongo"].count_nodes_scoped.assert_not_called()
    ctx["chroma"]._collection.get.assert_not_called()


class TestAuthorizationBoundaryUniformResponse:
    """Every excluded pack state must produce the identical not_candidate
    response with zero backend count calls -- #143 invariant 7 applied to
    this new read-only tool."""

    def test_nonexistent_pack_id(self, sql):
        ensure_test_user(sql, "alice")
        ctx = _base_ctx(sql)
        result = _call(sql, ctx, ALICE, "does-not-exist")
        assert result["pack_id"] == "does-not-exist"
        assert result["classification"] == "not_candidate"
        assert result["axes"] is None
        _assert_zero_backend_calls(ctx)

    def test_other_owner_private_pack(self, sql):
        ensure_test_user(sql, "alice")
        ensure_test_user(sql, "bob")
        create_pack(sql, "bob", "bob-pack")
        ctx = _base_ctx(sql)
        result = _call(sql, ctx, ALICE, "bob-pack")
        assert result == {"pack_id": "bob-pack", **NOT_CANDIDATE_RESPONSE_SHAPE}
        _assert_zero_backend_calls(ctx)

    def test_other_owner_public_pack_is_readable_not_excluded(self, sql):
        """Sanity check on the boundary itself: a public pack owned by
        someone else is NOT in the excluded set -- readable_pack_ids
        includes it, so this must reach diagnose_pack (and therefore
        produce non-None axes), unlike the three excluded cases below."""
        ensure_test_user(sql, "alice")
        ensure_test_user(sql, "bob")
        create_pack(sql, "bob", "bob-public")
        set_visibility(sql, BOB, "bob-public", "public-read")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 0
        ctx["neo4j"].count_exported_edges_scoped.return_value = 0
        ctx["mongo"].count_sources_scoped.return_value = 0
        ctx["mongo"].count_nodes_scoped.return_value = 0
        ctx["chroma"] = _FakeVec([])
        result = _call(sql, ctx, ALICE, "bob-public")
        assert result["axes"] is not None
        assert result["classification"] == "confirmed_empty"

    def test_creating_pack(self, sql):
        ensure_test_user(sql, "alice")
        begin_pack_creation(sql, "alice", "alice-creating")
        ctx = _base_ctx(sql)
        result = _call(sql, ctx, ALICE, "alice-creating")
        assert result == {"pack_id": "alice-creating", **NOT_CANDIDATE_RESPONSE_SHAPE}
        _assert_zero_backend_calls(ctx)

    def test_partial_pack(self, sql):
        ensure_test_user(sql, "alice")
        begin_pack_creation(sql, "alice", "alice-partial")
        mark_pack_partial(sql, "alice-partial", "alice")
        ctx = _base_ctx(sql)
        result = _call(sql, ctx, ALICE, "alice-partial")
        assert result == {"pack_id": "alice-partial", **NOT_CANDIDATE_RESPONSE_SHAPE}
        _assert_zero_backend_calls(ctx)

    def test_owned_private_pack_is_not_excluded(self, sql):
        """The owner of a private pack IS in readable_pack_ids -- confirms
        the exclusion above is about visibility+ownership, not private
        status alone."""
        ensure_test_user(sql, "alice")
        create_pack(sql, "alice", "alice-private")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 0
        ctx["neo4j"].count_exported_edges_scoped.return_value = 0
        ctx["mongo"].count_sources_scoped.return_value = 0
        ctx["mongo"].count_nodes_scoped.return_value = 0
        ctx["chroma"] = _FakeVec([])
        result = _call(sql, ctx, ALICE, "alice-private")
        assert result["axes"] is not None


class TestEndToEndResponseShape:
    def test_graph_positive_is_not_candidate_with_axes_populated(self, sql):
        """not_candidate reached via the graph-known-positive rule (not the
        registry-not-ready rule) still returns axes, unlike the
        authorization-boundary not_candidate above -- classify()'s two
        not_candidate paths are not required to look alike beyond the
        classification label itself."""
        ensure_test_user(sql, "alice")
        create_pack(sql, "alice", "alice-pack")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 5
        ctx["neo4j"].count_exported_edges_scoped.return_value = 2
        ctx["mongo"].count_sources_scoped.return_value = 1
        ctx["mongo"].count_nodes_scoped.return_value = 1
        ctx["chroma"] = _FakeVec(["v1"])
        result = _call(sql, ctx, ALICE, "alice-pack")
        assert result["classification"] == "not_candidate"
        assert result["axes"]["graph_nodes"] == {"state": "known", "count": 5}

    def test_content_residue_shape(self, sql):
        ensure_test_user(sql, "alice")
        create_pack(sql, "alice", "alice-pack")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 0
        ctx["neo4j"].count_exported_edges_scoped.return_value = 0
        ctx["mongo"].count_sources_scoped.return_value = 3
        ctx["mongo"].count_nodes_scoped.return_value = 0
        ctx["chroma"] = _FakeVec([])
        result = _call(sql, ctx, ALICE, "alice-pack")
        assert result["classification"] == "content_residue"
        assert result["axes"]["doc_sources"] == {"state": "known", "count": 3}

    def test_incomplete_observation_shape_on_backend_exception(self, sql):
        ensure_test_user(sql, "alice")
        create_pack(sql, "alice", "alice-pack")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 0
        ctx["neo4j"].count_exported_edges_scoped.return_value = 0
        ctx["mongo"].count_sources_scoped.return_value = 0
        ctx["mongo"].count_nodes_scoped.side_effect = RuntimeError("mongo down")
        ctx["chroma"] = _FakeVec([])
        result = _call(sql, ctx, ALICE, "alice-pack")
        assert result["classification"] == "incomplete_observation"
        assert result["axes"]["doc_nodes"] == {"state": "unknown", "count": None}

    def test_live_vector_get_failure_returns_incomplete_observation(self, sql):
        """An initialized vector backend can fail after authorization.

        The real diagnostic counter reaches the Chroma-shaped collection's
        ``.get()`` call. The tool still returns a diagnostic result instead
        of propagating the backend exception.
        """
        ensure_test_user(sql, "alice")
        create_pack(sql, "alice", "alice-pack")
        ctx = _base_ctx(sql)
        ctx["neo4j"].count_exported_nodes_scoped.return_value = 0
        ctx["neo4j"].count_exported_edges_scoped.return_value = 0
        ctx["mongo"].count_sources_scoped.return_value = 0
        ctx["mongo"].count_nodes_scoped.return_value = 0
        ctx["chroma"] = _FakeVec([])
        ctx["chroma"]._collection.get.side_effect = RuntimeError("chroma connection lost")

        result = _call(sql, ctx, ALICE, "alice-pack")

        assert result["axes"]["vectors"] == {"state": "unknown", "count": None}
        assert result["classification"] == "incomplete_observation"
        ctx["chroma"]._collection.get.assert_called_once()

