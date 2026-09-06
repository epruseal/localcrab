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

Follow-up (same issue, dual adversarial verification round 1): removing
``ValueError`` wholesale from the tuple above also silently swallowed this
codebase's OWN input/property validation failures
(``opencrab/common/graph_identity.py``'s ``prepare_node``/
``normalize_node_properties``/``normalize_edge_properties``, and
``opencrab/stores/neo4j_store.py``'s ``_label``/``_clean_node_properties``),
because some of those validation calls happen INSIDE the Neo4j write
transaction closure (``update_node``'s incident-edge refresh loop,
``upsert_edge``'s stored-property re-validation), not only before it. That
silently reclassified a caller-data-shape rejection (previously surfaced
with its own safe message via ``opencrab/mcp/tools/graph.py``'s
``except ValueError as exc: return {"error": str(exc), "valid": False}``
and ``opencrab/pack/load.py``'s three ``except ValueError as ve: skip += 1``
sites) as an opaque ``"error: ValueError"`` per-store failure instead.

The fix adds ``GraphPropertyValidationError(ValueError)`` -- a marker
subclass every raise site in ``graph_identity.py`` (plus the two targeted
sites in ``neo4j_store.py`` reachable from this try block) now uses -- back
into the re-raise tuple. A driver-native bare ``ValueError`` (not an
instance of this subclass) still falls through to the generic path proven
by the two tests above; a ``GraphPropertyValidationError`` now re-raises
with its own safe, fixed message intact instead of being absorbed.
"""

from __future__ import annotations

import pytest

from opencrab.common.graph_identity import (
    GraphPropertyValidationError,
    normalize_edge_properties,
    normalize_node_properties,
)
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


class _GraphPropertyValidationGraphStub:
    """Graph store stub whose write calls raise
    ``GraphPropertyValidationError`` with the SAME message the real
    validation helpers use (``opencrab/common/graph_identity.py``'s
    ``normalize_node_properties``/``normalize_edge_properties``, or
    ``opencrab/stores/neo4j_store.py``'s ``_clean_node_properties``) --
    simulating the incident-edge-refresh / stored-property re-validation
    call that happens INSIDE the Neo4j write transaction closure, which is
    what this issue's dual adversarial verification round 1 found is
    reachable from ``add_node``/``add_edge``'s try block.
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
        raise GraphPropertyValidationError("reserved graph property")

    def upsert_edge(self, *args, **kwargs):  # noqa: ARG002
        raise GraphPropertyValidationError("reserved graph property")


def _make_property_validation_builder() -> tuple[OntologyBuilder, str]:
    from unittest.mock import MagicMock

    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "test-user")
    pack_id = create_pack(sql, "test-user", "issue168-graphpropvalidation-test-pack")
    mongo = MagicMock(available=False)
    builder = OntologyBuilder(neo4j=_GraphPropertyValidationGraphStub(), mongo=mongo, sql=sql)
    return builder, pack_id


class _GraphPropertyValidationEdgeOnlyGraphStub(_GraphPropertyValidationGraphStub):
    """Same as ``_GraphPropertyValidationGraphStub``, except ``upsert_node``
    succeeds -- so an ``add_edge`` test can set up its two endpoint nodes
    first, then trigger the ``GraphPropertyValidationError`` only on the
    edge write itself.
    """

    def upsert_node(self, **kwargs):
        return dict(kwargs.get("properties") or {})


def _make_property_validation_edge_builder() -> tuple[OntologyBuilder, str]:
    from unittest.mock import MagicMock

    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "test-user")
    pack_id = create_pack(sql, "test-user", "issue168-graphpropvalidation-edge-test-pack")
    mongo = MagicMock(available=False)
    builder = OntologyBuilder(neo4j=_GraphPropertyValidationEdgeOnlyGraphStub(), mongo=mongo, sql=sql)
    return builder, pack_id


@pytest.mark.usefixtures("bind_test_principal")
class TestGraphPropertyValidationErrorIsReraisedNotAbsorbed:
    """Counterexample this issue's dual adversarial verification round 1
    raised against the first version of this fix: a ``ValueError`` this
    codebase's own property/identity validation raises INSIDE the Neo4j
    write try block (not the driver's own bare ``ValueError``) must keep
    stopping the whole write and keep its own safe message -- not get
    silently absorbed into ``_safe_store_status(exc) == "error: ValueError"``
    the way the two tests above prove a genuine driver decode error should
    be.
    """

    def test_add_node_property_validation_error_reraises_with_safe_message(self):
        builder, pack_id = _make_property_validation_builder()

        # Must raise -- unlike the driver's own bare ValueError (see the two
        # tests above), a GraphPropertyValidationError is this codebase's
        # own input-shape rejection and must stop the whole write, the same
        # way NodeIdentityConflict/EdgeIdentityConflict/
        # GraphSchemaMigrationRequired/GraphWriteUnavailable already do.
        with pytest.raises(GraphPropertyValidationError) as excinfo:
            builder.add_node(
                "subject", "User", "u-issue168-propval",
                properties={"name": "Issue168 Prop Validation User", "email": "u@example.com", "role": "admin"},
                pack_id=pack_id,
            )

        # The exact safe, fixed-string message must survive unconverted --
        # not be replaced by a type-name-only receipt.
        assert str(excinfo.value) == "reserved graph property"

    def test_add_edge_property_validation_error_reraises_with_safe_message(self):
        builder, pack_id = _make_property_validation_edge_builder()
        builder.add_node(
            "subject", "User", "u-issue168-propval-from",
            properties={"name": "Issue168 From", "email": "from@example.com", "role": "admin"},
            pack_id=pack_id,
        )
        builder.add_node(
            "resource", "Dataset", "d-issue168-propval-to",
            properties={"name": "Issue168 Dataset"},
            pack_id=pack_id,
        )

        with pytest.raises(GraphPropertyValidationError) as excinfo:
            builder.add_edge(
                "subject", "u-issue168-propval-from", "owns", "resource", "d-issue168-propval-to",
                pack_id=pack_id,
            )

        assert str(excinfo.value) == "reserved graph property"


class TestGraphIdentityLoneSurrogateIsGraphPropertyValidationError:
    """Dual adversarial verification round 1 (design v2) counterexample:
    ``_validate_json()``'s ``.encode("utf-8")`` calls used to run unwrapped,
    so a lone UTF-16 surrogate in a property value or object key raised a
    raw built-in ``UnicodeEncodeError`` -- a ``ValueError`` subclass, but
    NOT a ``GraphPropertyValidationError`` instance, so it would have
    bypassed the re-raise tuple above and fallen back to the opaque
    ``"error: ValueError"`` path despite being this codebase's own grammar
    check, same as every other raise in this module. This proves the fix
    (wrapping those two calls) closes that gap, and that the safe message
    never repeats the offending surrogate code point.
    """

    def test_lone_surrogate_property_value_raises_graph_property_validation_error(self):
        with pytest.raises(GraphPropertyValidationError) as excinfo:
            normalize_node_properties("n1", {"name": "bad\udcff surrogate"})

        assert str(excinfo.value) == "graph properties must not contain lone surrogate code points"
        assert "\udcff" not in str(excinfo.value)

    def test_lone_surrogate_property_key_raises_graph_property_validation_error(self):
        with pytest.raises(GraphPropertyValidationError) as excinfo:
            normalize_node_properties("n1", {"bad\udcff key": "value"})

        assert str(excinfo.value) == "graph properties object keys must not contain lone surrogate code points"
        assert "\udcff" not in str(excinfo.value)

    def test_reserved_edge_property_raises_graph_property_validation_error(self):
        with pytest.raises(GraphPropertyValidationError) as excinfo:
            normalize_edge_properties("a", "owns", "b", {"edge_key": "forged"})

        assert str(excinfo.value) == "reserved graph property"
