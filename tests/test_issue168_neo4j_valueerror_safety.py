"""
#168 (Fix G) regression: the installed Neo4j Python driver can raise a bare
``ValueError`` from its own PackStream wire-protocol decoder (confirmed by
direct read of the installed driver's
``neo4j/_codec/packstream/v1/__init__.py``) -- unrelated to any of this
codebase's own ``ValueError`` raises (mutual-exclusion, anchor-shape
mismatch, grammar validation), all of which happen BEFORE the Neo4j-write
try block in ``OntologyBuilder.add_node``/``add_edge`` is ever entered.

Before this fix, that try block's except-tuple included ``ValueError``
alongside ``NodeIdentityConflict``/``EdgeIdentityConflict``/
``GraphSchemaMigrationRequired``/``GraphWriteUnavailable`` and re-raised it
unconverted -- letting the driver's own decode-error text (which can carry
wire-protocol/marker detail) escape all the way to
``opencrab/mcp/tools/graph.py``'s handler-level
``except ValueError as exc: return {"error": str(exc), ...}``.

The fix removes ``ValueError`` from that tuple, so an unanticipated
``ValueError`` raised DURING the write call now falls through to the
existing ``except Exception as exc:`` branch and is recorded via
``_safe_store_status(exc)`` (the exception's type name only) instead of
being blindly re-raised.

This is the reverse-mutation check Fix G's "lossless" claim requires: revert
the tuple change (put ``ValueError`` back) and these two tests must go RED,
because the stub's ``ValueError`` would then propagate straight out of
``add_node``/``add_edge`` instead of landing in the returned receipt.
"""

from __future__ import annotations

import pytest

from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack.ownership import create_pack
from opencrab.stores.sql_store import SQLStore
from tests._pack_fixtures import ensure_test_user


class _DriverPackStreamErrorGraphStub:
    """Graph store stub whose write calls raise the SAME exception shape
    the installed Neo4j driver's PackStream decoder raises on a malformed
    or unrecognized wire marker: a bare ``ValueError``, not a
    ``neo4j.exceptions`` type. See this module's docstring for where that
    was confirmed in the installed driver's own source.

    The identity-guard probe methods (``get_node``/``get_nodes_by_id``/
    ``get_edge``/``lookup_node_type``) answer "this id is free" / "resolves
    to a fixed type", same as ``test_mongo_unit.py``'s ``_AvailableGraphStub``
    -- so the write call below is actually reached instead of being refused
    earlier by the identity guard.
    """

    available = True

    def get_node(self, node_type, node_id):  # noqa: ARG002
        return None

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return []

    def get_edge(self, *args):  # noqa: ARG002
        return None

    def lookup_node_type(self, node_id):  # noqa: ARG002
        return "StubType"

    def upsert_node(self, **kwargs):  # noqa: ARG002
        raise ValueError("Unknown PackStream marker 7F")

    def upsert_edge(self, *args, **kwargs):  # noqa: ARG002
        raise ValueError("Unknown PackStream marker 7F")


def _make_builder() -> tuple[OntologyBuilder, str]:
    from unittest.mock import MagicMock

    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "test-user")
    pack_id = create_pack(sql, "test-user", "issue168-valueerror-test-pack")
    mongo = MagicMock(available=False)
    builder = OntologyBuilder(neo4j=_DriverPackStreamErrorGraphStub(), mongo=mongo, sql=sql)
    return builder, pack_id


@pytest.mark.usefixtures("bind_test_principal")
class TestNeo4jDriverBareValueErrorIsNotReraisedOrLeaked:
    def test_add_node_neo4j_bare_valueerror_is_caught_not_reraised(self):
        builder, pack_id = _make_builder()

        # Must NOT raise -- add_node/add_edge never propagate a per-store
        # write failure to the caller (see builder.py's module docstring).
        # Before Fix G, ValueError sat in the re-raise tuple and this call
        # would raise ValueError("Unknown PackStream marker 7F") instead of
        # returning a dict.
        result = builder.add_node(
            "subject", "User", "u-issue168",
            properties={"name": "Issue168 Test User", "email": "u-issue168@example.com", "role": "admin"},
            pack_id=pack_id,
        )

        # The driver's own decode-error text must never reach the caller --
        # only the exception's type name, same shape as every other
        # per-store failure in this module (_safe_store_status).
        assert result["stores"]["graph"] == "error: ValueError"

    def test_add_edge_neo4j_bare_valueerror_is_caught_not_reraised(self):
        builder, pack_id = _make_builder()
        builder.add_node(
            "subject", "User", "u-issue168-from",
            properties={"name": "Issue168 From", "email": "from@example.com", "role": "admin"},
            pack_id=pack_id,
        )
        builder.add_node(
            "resource", "Dataset", "d-issue168-to",
            properties={"name": "Issue168 Dataset"},
            pack_id=pack_id,
        )

        result = builder.add_edge(
            "subject", "u-issue168-from", "owns", "resource", "d-issue168-to",
            pack_id=pack_id,
        )

        assert result["stores"]["graph"] == "error: ValueError"
