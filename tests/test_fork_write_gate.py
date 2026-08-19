"""#201 `pack_fork`, execution unit 5d (design v7 §4-C-1/2/3, §6-3, §8 T19-T22).

Three additions to the write chokepoints, all defaulting to today's exact
behaviour:

1. ``write_gate.authorize_fork_copy`` -- the ONE widening of the write gate
   this design makes. Owner-only, same as ``authorize``, but the allowed
   status is ``('creating',)`` and the row must additionally carry
   ``forked_from`` -- otherwise a caller could write into ANY of their own
   ``creating`` packs, including one ``pack_create`` still has in flight.
2. ``fork_copy: bool = False`` on ``OntologyBuilder.add_node``,
   ``OntologyBuilder.add_edge``, and ``source_writer.write_source`` -- routes
   authorization through #1 instead of ``authorize`` when True. Everything
   else (identity guard, stamping, grammar validation, "graph unavailable =>
   write nothing") is unchanged.
3. ``write_vector: bool = True`` on ``add_node``/``write_source`` -- skips
   ONLY the vector leg when False and records the skip in the receipt.
   ``origin: Literal["client", "server"] = "client"`` on ``write_source`` --
   threads into the existing ``stamp(...)`` call, same reason ``add_node``
   already has it.

Reverse-mutation is the standard throughout (design v7 §8): each test is
written so that removing the guard it covers makes the test fail, not just
exercises the happy path.

Follows tests/test_builder_gate.py and tests/test_source_writer.py's
conventions: a REAL in-memory-backed SQLite ``SQLStore`` for the registry
(``authorize``/``authorize_fork_copy`` do real ``rowcount``-shaped work a bare
``MagicMock`` cannot satisfy), hand-written doubles for graph/docs/vector
honouring only the slot contract each guard actually probes, and
``principal_scope`` for the ambient caller identity ``current_principal()``
reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack.ownership import PackNotFoundError, begin_pack_creation
from opencrab.pack.source_writer import write_source
from opencrab.pack.write_gate import ClientIdentityFieldError, authorize_fork_copy

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)
BOB = Principal(user_id="user_bob", is_local=False, disabled=False)


@pytest.fixture
def sql(tmp_path):
    """Real registry, no packs pre-created -- each test reserves its own
    ``creating`` row via ``begin_pack_creation`` so it controls whether
    ``forked_from`` is set (the abuse-guard axis under test)."""
    from sqlalchemy import text as _t

    from opencrab.stores.sql_store import SQLStore

    store = SQLStore(f"sqlite:///{tmp_path}/o.db")
    with store._engine.begin() as conn:
        for p in (ALICE, BOB):
            conn.execute(
                _t(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES (:u, :n, 0)"
                ),
                {"u": p.user_id, "n": p.user_id},
            )
    return store


def _reserve(sql_, owner: Principal, pack_id: str, *, forked_from: str | None = None) -> str:
    """``begin_pack_creation`` with an explicit ``forked_from`` axis.

    ``forked_from=None`` reproduces exactly what ``pack_create`` reserves
    while its own anchor write is in flight -- the shape the abuse guard
    must refuse."""
    return begin_pack_creation(sql_, owner.user_id, pack_id, forked_from=forked_from)


# ---------------------------------------------------------------------------
# `write_gate.authorize_fork_copy` -- unit level, no writer involved
# ---------------------------------------------------------------------------


class TestAuthorizeForkCopy:
    def test_succeeds_on_a_creating_pack_reserved_by_fork(self, sql):
        pid = _reserve(sql, ALICE, "dst-a", forked_from="src-a")
        row = authorize_fork_copy(sql, ALICE, pid)
        assert row["status"] == "creating"
        assert row["forked_from"] == "src-a"

    def test_refuses_a_creating_pack_with_no_forked_from(self, sql):
        """The abuse guard (T19): a `pack_create`-in-flight row -- the ONLY
        other way a `creating` row exists -- carries no `forked_from` and
        must not be writable through this door."""
        pid = _reserve(sql, ALICE, "inflight-a", forked_from=None)
        with pytest.raises(ValueError, match="reserved by pack_fork"):
            authorize_fork_copy(sql, ALICE, pid)

    def test_still_owner_only(self, sql):
        """Reverse-mutation: deleting the owner check would let anyone widen
        into anyone else's fork-reserved pack."""
        pid = _reserve(sql, ALICE, "dst-b", forked_from="src-b")
        with pytest.raises(PackNotFoundError):
            authorize_fork_copy(sql, BOB, pid)

    def test_ready_pack_is_not_authorized_here_either(self, sql):
        """`authorize_fork_copy` widens to `creating` only -- a `ready` pack
        (even the caller's own, even with `forked_from` set from a completed
        fork) is not this function's business; `authorize` covers it."""
        from opencrab.pack.ownership import create_pack

        pid = create_pack(sql, ALICE.user_id, "ready-a", forked_from="src-a")
        with pytest.raises(PackNotFoundError):
            authorize_fork_copy(sql, ALICE, pid)


# ---------------------------------------------------------------------------
# `OntologyBuilder.add_node` / `add_edge` -- doubles honouring the slot
# contract each guard probes (mirrors tests/test_builder_gate.py)
# ---------------------------------------------------------------------------


class _Graph:
    available = True

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: list[dict] = []

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (_t, i), v in sorted(self.nodes.items()) if i == node_id]

    def get_edge(self, from_type, from_id, relation, to_type, to_id):  # noqa: ARG002
        return None

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)

    def lookup_node_type(self, node_id):
        for (t, i) in self.nodes:
            if i == node_id:
                return t
        return None

    def upsert_edge(self, from_type, from_id, relation, to_type, to_id, properties):  # noqa: ARG002
        self.edges.append(dict(properties))
        return True


class _Docs:
    available = True

    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return None

    def upsert_node_doc(self, space, node_type, node_id, properties):  # noqa: ARG002
        return "doc-1"

    def log_event(self, *a, **kw):  # noqa: ARG002
        return "ev-1"


class _Vec:
    available = True

    def __init__(self):
        self.upsert_calls: list[tuple[list[str], list[str]]] = []

    def get_by_id(self, doc_id):  # noqa: ARG002
        return None

    def upsert_texts(self, texts, ids, metadatas):  # noqa: ARG002
        self.upsert_calls.append((texts, ids))
        return list(ids)


@pytest.fixture
def builder(sql):
    return OntologyBuilder(_Graph(), _Docs(), sql, vec=_Vec())


class TestBuilderAddNodeForkCopy:
    def test_without_fork_copy_creating_pack_is_unwritable(self, builder, sql):
        """T19 (half 1): proves the widening is actually needed -- today's
        `authorize` alone refuses any write into a `creating` pack."""
        pid = _reserve(sql, ALICE, "dst-c", forked_from="src-c")
        with pytest.raises(PackNotFoundError), principal_scope(ALICE):
            builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
            )

    def test_fork_copy_true_succeeds_on_a_forked_creating_pack(self, builder, sql):
        """T19 (half 2)."""
        pid = _reserve(sql, ALICE, "dst-d", forked_from="src-d")
        with principal_scope(ALICE):
            out = builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True,
            )
        assert out["stores"]["graph"] == "ok"
        assert out["properties"]["pack_id"] == pid

    def test_fork_copy_true_is_refused_on_a_pack_create_in_flight_pack(self, builder, sql):
        """The abuse guard, exercised through the writer rather than the gate
        directly: `fork_copy=True` must not become a general "write into any
        creating pack" door."""
        pid = _reserve(sql, ALICE, "inflight-b", forked_from=None)
        with pytest.raises(ValueError, match="reserved by pack_fork"), principal_scope(ALICE):
            builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True,
            )
        assert builder._neo4j.nodes == {}, "refused, so nothing may have been written"

    def test_fork_copy_is_still_owner_only(self, builder, sql):
        pid = _reserve(sql, ALICE, "dst-e", forked_from="src-e")
        with pytest.raises(PackNotFoundError), principal_scope(BOB):
            builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True,
            )


class TestBuilderAddEdgeForkCopy:
    def test_without_fork_copy_creating_pack_is_unwritable(self, builder, sql):
        pid = _reserve(sql, ALICE, "dst-f", forked_from="src-f")
        with pytest.raises(PackNotFoundError), principal_scope(ALICE):
            builder.add_edge("resource", "a", "cites", "resource", "b", pack_id=pid)

    def test_fork_copy_true_succeeds_on_a_forked_creating_pack(self, builder, sql):
        pid = _reserve(sql, ALICE, "dst-g", forked_from="src-g")
        graph = builder._neo4j
        graph.nodes[("Document", "a")] = {"pack_id": pid, "space": "resource"}
        graph.nodes[("Document", "b")] = {"pack_id": pid, "space": "resource"}
        with principal_scope(ALICE):
            out = builder.add_edge(
                "resource", "a", "cites", "resource", "b", pack_id=pid,
                origin="server", fork_copy=True,
            )
        assert out["stores"]["graph"] == "ok"

    def test_fork_copy_true_is_refused_on_a_pack_create_in_flight_pack(self, builder, sql):
        pid = _reserve(sql, ALICE, "inflight-c", forked_from=None)
        with pytest.raises(ValueError, match="reserved by pack_fork"), principal_scope(ALICE):
            builder.add_edge(
                "resource", "a", "cites", "resource", "b", pack_id=pid,
                origin="server", fork_copy=True,
            )


# ---------------------------------------------------------------------------
# `write_vector=False` -- must keep authorization/identity-guard/stamping and
# only skip the store leg, correct receipt key per writer (T21, T22-adjacent)
# ---------------------------------------------------------------------------


class TestBuilderWriteVectorFalse:
    def test_still_authorizes(self, builder, sql):
        """A switch that skipped the gate would be a hole: prove BOB is still
        refused even with write_vector=False."""
        pid = _reserve(sql, ALICE, "dst-h", forked_from="src-h")
        with pytest.raises(PackNotFoundError), principal_scope(BOB):
            builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True, write_vector=False,
            )

    def test_still_runs_the_identity_guard(self, builder, sql):
        """A node_id already attributed to a different pack must still be
        refused with write_vector=False -- the vector leg being skipped must
        not shrink the guard that runs before any store write."""
        pid = _reserve(sql, ALICE, "dst-i", forked_from="src-i")
        builder._neo4j.nodes[("Document", "taken")] = {
            "pack_id": "someone-elses-pack", "space": "resource",
        }
        with pytest.raises(ValueError, match="already attributed"), principal_scope(ALICE):
            builder.add_node(
                "resource", "Document", "taken", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True, write_vector=False,
            )

    def test_still_stamps_and_skips_only_the_vector_leg(self, builder, sql):
        pid = _reserve(sql, ALICE, "dst-j", forked_from="src-j")
        vec = builder._vec
        with principal_scope(ALICE):
            out = builder.add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id=pid,
                origin="server", fork_copy=True, write_vector=False,
            )
        assert out["properties"]["pack_id"] == pid
        assert out["properties"]["owner_id"] == ALICE.user_id
        assert out["stores"]["graph"] == "ok"
        assert out["stores"]["vector"] == "skipped (raw copy)"
        assert vec.upsert_calls == [], "the vector leg must not have run at all"

    def test_write_vector_true_default_is_unchanged(self, builder, sql):
        """Regression: the default must keep embedding, exactly as before
        this parameter existed."""
        pid = _reserve(sql, ALICE, "dst-k", forked_from="src-k")
        vec = builder._vec
        with principal_scope(ALICE):
            out = builder.add_node(
                "resource", "Document", "n1", {"title": "some text"}, pack_id=pid,
                origin="server", fork_copy=True,
            )
        assert out["stores"]["vector"] == "ok"
        assert vec.upsert_calls, "the vector leg must have run"


# ---------------------------------------------------------------------------
# `source_writer.write_source` -- doubles mirror tests/test_source_writer.py
# ---------------------------------------------------------------------------


class _SourceDocs:
    def __init__(self, available=True, existing=None):
        self.available = available
        self._existing = existing
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def upsert_source(self, source_id, text, metadata):
        self.calls.append((source_id, text, dict(metadata)))
        return source_id

    def get_source(self, source_id):  # noqa: ARG002
        return self._existing


class _Hybrid:
    def __init__(self, status="ok (id=s1)"):
        self._status = status
        self.calls: list[dict[str, Any]] = []

    def ingest(self, text, source_id, metadata=None):
        self.calls.append({"text": text, "source_id": source_id, "metadata": metadata})
        return {"source_id": source_id, "stores": {"chromadb": self._status}}


class _SourceVec:
    available = True

    def __init__(self, row=None):
        self._row = row

    def get_by_id(self, doc_id):  # noqa: ARG002
        return self._row


def _write(sql_, *, pack_id, principal=ALICE, docs=None, hybrid=None, vector=None, **kw):
    with principal_scope(principal):
        return write_source(
            sql_, hybrid or _Hybrid(), docs or _SourceDocs(), vector or _SourceVec(),
            text="t", source_id="s1", pack_id=pack_id, **kw
        )


class TestSourceWriterForkCopy:
    def test_without_fork_copy_creating_pack_is_unwritable(self, sql):
        pid = _reserve(sql, ALICE, "dst-l", forked_from="src-l")
        with pytest.raises(PackNotFoundError):
            _write(sql, pack_id=pid)

    def test_fork_copy_true_succeeds_on_a_forked_creating_pack(self, sql):
        pid = _reserve(sql, ALICE, "dst-m", forked_from="src-m")
        docs = _SourceDocs()
        receipt = _write(sql, pack_id=pid, docs=docs, origin="server", fork_copy=True)
        assert receipt["stores"]["documents"].startswith("ok")
        assert docs.calls, "the source row must have been written"

    def test_fork_copy_true_is_refused_on_a_pack_create_in_flight_pack(self, sql):
        pid = _reserve(sql, ALICE, "inflight-d", forked_from=None)
        docs = _SourceDocs()
        with pytest.raises(ValueError, match="reserved by pack_fork"):
            _write(sql, pack_id=pid, docs=docs, origin="server", fork_copy=True)
        assert docs.calls == [], "refused, so nothing may have been written"

    def test_fork_copy_is_still_owner_only(self, sql):
        pid = _reserve(sql, ALICE, "dst-n", forked_from="src-n")
        with pytest.raises(PackNotFoundError):
            _write(sql, pack_id=pid, principal=BOB, origin="server", fork_copy=True)


class TestSourceWriterWriteVectorFalse:
    def test_still_authorizes(self, sql):
        pid = _reserve(sql, ALICE, "dst-o", forked_from="src-o")
        with pytest.raises(PackNotFoundError):
            _write(
                sql, pack_id=pid, principal=BOB,
                origin="server", fork_copy=True, write_vector=False,
            )

    def test_still_runs_the_identity_guard(self, sql):
        pid = _reserve(sql, ALICE, "dst-p", forked_from="src-p")
        docs = _SourceDocs(existing={"metadata": {"pack_id": "someone-elses"}})
        with pytest.raises(ValueError, match="already attributed"):
            _write(
                sql, pack_id=pid, docs=docs,
                origin="server", fork_copy=True, write_vector=False,
            )
        assert docs.calls == [], "the foreign row must not be overwritten"

    def test_still_stamps_and_skips_only_the_vector_leg(self, sql):
        pid = _reserve(sql, ALICE, "dst-q", forked_from="src-q")
        docs = _SourceDocs()
        hybrid = _Hybrid()
        receipt = _write(
            sql, pack_id=pid, docs=docs, hybrid=hybrid,
            origin="server", fork_copy=True, write_vector=False,
        )
        assert receipt["metadata"]["pack_id"] == pid
        assert receipt["metadata"]["user_id"] == ALICE.user_id
        assert receipt["stores"]["documents"].startswith("ok")
        assert receipt["stores"]["chromadb"] == "skipped (raw copy)"
        assert hybrid.calls == [], "the vector leg must not have run at all"

    def test_write_vector_true_default_is_unchanged(self, sql):
        pid = _reserve(sql, ALICE, "dst-r", forked_from="src-r")
        hybrid = _Hybrid()
        receipt = _write(
            sql, pack_id=pid, hybrid=hybrid, origin="server", fork_copy=True,
        )
        assert receipt["stores"]["chromadb"].startswith("ok")
        assert hybrid.calls, "the vector leg must have run"


# ---------------------------------------------------------------------------
# `origin="server"` on write_source (T20)
# ---------------------------------------------------------------------------


class TestSourceWriterOrigin:
    def test_default_origin_rejects_a_foreign_user_id(self, sql):
        """T20 (half 1): a copied source's metadata carries the ORIGINAL
        owner's user_id -- client-origin (the default, unchanged) must
        refuse it as forged identity, exactly as it does for any other
        caller-supplied identity mismatch."""
        pid = _reserve(sql, ALICE, "dst-s", forked_from="src-s")
        with pytest.raises(ClientIdentityFieldError):
            _write(
                sql, pack_id=pid, metadata={"user_id": "the-original-owner"},
                fork_copy=True,
            )

    def test_server_origin_accepts_and_overwrites_the_foreign_user_id(self, sql):
        """T20 (half 2)."""
        pid = _reserve(sql, ALICE, "dst-t", forked_from="src-t")
        receipt = _write(
            sql, pack_id=pid, metadata={"user_id": "the-original-owner"},
            origin="server", fork_copy=True,
        )
        assert receipt["metadata"]["user_id"] == ALICE.user_id


# ---------------------------------------------------------------------------
# T22 -- abuse-prevention grep contract (design v7 §4-C, "남용 방지"). The
# design's own wording is "호출자는 opencrab/pack/fork.py 하나여야 한다" -- the
# CALLER (module) must be exactly one -- not "exactly one call site": the
# orchestrator legitimately passes `write_vector=False` twice from inside
# that one file (once for the node leg §5-3-14, once for the source leg
# §5-3-16), and `fork_copy=True` at least three times (node/edge/source
# legs). Counting distinct FILES rather than raw occurrences is what makes
# "one caller" a stable, checkable contract instead of one this design's own
# orchestrator would immediately violate.
#
# `opencrab/pack/fork.py` (the orchestrator) has not landed yet in this
# unit, so the true file count is 0 today. "At most one" pins the ceiling
# now; the orchestrator commit tightens this to "exactly one" (asserting the
# single file IS opencrab/pack/fork.py) once that module exists.
# ---------------------------------------------------------------------------


def _caller_files(pattern: re.Pattern[str]) -> list[str]:
    root = Path(__file__).resolve().parent.parent / "opencrab"
    files: list[str] = []
    for path in sorted(root.rglob("*.py")):
        raw = path.read_text(encoding="utf-8")
        if pattern.search(raw):
            files.append(str(path.relative_to(root.parent)))
    return files


def test_fork_copy_true_has_at_most_one_caller_file_repo_wide():
    files = _caller_files(re.compile(r"fork_copy\s*=\s*True"))
    assert len(files) <= 1, (
        f"fork_copy=True must be used from at most one file (the pack_fork "
        f"orchestrator): {files}"
    )


def test_write_vector_false_has_at_most_one_caller_file_repo_wide():
    files = _caller_files(re.compile(r"write_vector\s*=\s*False"))
    assert len(files) <= 1, (
        f"write_vector=False must be used from at most one file (the "
        f"pack_fork orchestrator): {files}"
    )
