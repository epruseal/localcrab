"""opencrab.pack.ownership's registry lifecycle (#170, design v4).

Pins the ``packs.status`` state machine (``creating`` -> ``ready``,
``creating`` -> ``partial``, ``partial`` -> ``ready``) added on top of
#146/#148's ownership registry: incomplete rows (``creating``/``partial``)
must be invisible on every read path AND unwritable through every writer,
including to their own owner (#143 invariant 7 -- a private/incomplete row's
existence must never be observable to anyone but a positive owner+status
match). ``OntologyBuilder.add_node``'s ``pack_anchor`` opt-out is the single,
narrowly-shaped exception: writing a ``creating`` pack's own graph anchor
node, and nothing else.

Not to be confused with ``tests/test_packs_registry.py`` (the pre-#170
ownership/visibility tests, still valid and still run) or
``tests/test_builder_gate.py`` (the pre-#170 authorization/stamping tests
for the builder). This file only covers what #170 adds: the status column,
its four transition functions, the ready-only query/write filters, and the
anchor opt-out.

Fixture style follows ``tests/test_packs_registry.py``: a real in-memory
SQLite ``SQLStore`` (no LOCAL_DATA_DIR/scratch dir needed), ``create_user``
for owner ids, hand-built ``Principal``s for callers. Builder tests use
in-process store doubles honouring the same slot contract as
``tests/test_builder_gate.py``'s ``_Graph``/``_Docs``/``_Vec`` (a real graph
store isn't needed here -- these tests are about the STATUS gate in front of
the write, not the identity-slot guard behind it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack.ownership import (
    PACK_STATUS_CREATING,
    PACK_STATUS_PARTIAL,
    PACK_STATUS_READY,
    PackNotFoundError,
    anchor_node_id,
    assert_writable,
    begin_pack_creation,
    create_pack,
    delete_pack_row,
    ensure_default_pack,
    get_pack,
    list_incomplete_packs,
    list_packs_for,
    mark_pack_partial,
    mark_pack_ready,
    promote_partial_pack,
    readable_pack_ids,
    set_visibility,
)
from opencrab.pack.read_scope import read_scope
from opencrab.pack.write_gate import authorize as gate_authorize


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


@pytest.fixture
def bob(sql):
    return create_user(sql, "Bob")


def _p(user_id: str) -> Principal:
    return Principal(user_id=user_id, is_local=False, disabled=False)


def _force_visibility(sql, pack_id: str, visibility: str) -> None:
    """Flip visibility with a raw UPDATE, bypassing ``set_visibility`` --
    which itself calls ``assert_writable`` and would refuse to touch a
    ``creating``/``partial`` row. Some tests need a public INCOMPLETE row to
    pin the "existence must not leak even if public" behaviour."""
    with sql._engine.begin() as conn:
        conn.execute(
            _sql_text("UPDATE packs SET visibility = :vis WHERE pack_id = :pid"),
            {"vis": visibility, "pid": pack_id},
        )


# ---------------------------------------------------------------------------
# Builder doubles (same slot contract as tests/test_builder_gate.py's, kept
# local and minimal -- these tests only exercise the STATUS gate in front of
# add_node, not the identity-slot guard behind it).
# ---------------------------------------------------------------------------


class _Graph:
    available = True

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (_t, i), v in sorted(self.nodes.items()) if i == node_id]

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)

    def lookup_node_type(self, node_id):
        for (t, i) in self.nodes:
            if i == node_id:
                return t
        return None


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

    def get_by_id(self, doc_id):  # noqa: ARG002
        return None

    def upsert_texts(self, texts, ids, metadatas):  # noqa: ARG002
        return list(ids)


@pytest.fixture
def builder(sql):
    return OntologyBuilder(_Graph(), _Docs(), sql, vec=_Vec())


# ---------------------------------------------------------------------------
# 1. Hidden path exhaustiveness -- creating/partial rows disappear from
#    every read path AND every writer, including to their own owner.
# ---------------------------------------------------------------------------


class TestHiddenFromEveryReadPath:
    def test_creating_pack_hidden_from_owner_on_every_path(self, sql, alice, bob):
        pid = begin_pack_creation(sql, alice, "wip-a")
        alice_p, bob_p = _p(alice), _p(bob)

        assert pid not in readable_pack_ids(sql, alice_p)
        assert pid not in readable_pack_ids(sql, bob_p)
        assert pid not in {r["pack_id"] for r in list_packs_for(sql, alice_p)}
        assert pid not in read_scope(sql, alice_p)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, alice_p, pid)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, bob_p, pid)

    def test_partial_pack_hidden_from_owner_on_every_path(self, sql, alice, bob):
        pid = begin_pack_creation(sql, alice, "wip-b")
        assert mark_pack_partial(sql, pid, alice) is True
        alice_p, bob_p = _p(alice), _p(bob)

        assert pid not in readable_pack_ids(sql, alice_p)
        assert pid not in readable_pack_ids(sql, bob_p)
        assert pid not in {r["pack_id"] for r in list_packs_for(sql, alice_p)}
        assert pid not in read_scope(sql, alice_p)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, alice_p, pid)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, bob_p, pid)

    def test_nonowner_gets_not_found_not_forbidden_even_when_public(self, sql, alice, bob):
        """#143 invariant 7 (존재 누출 금지): flipping an incomplete row to
        public-read must NOT turn its refusal into PackForbiddenError -- that
        would confirm to a non-owner that the slug is in use. It must stay
        PackNotFoundError, indistinguishable from "no such pack"."""
        pid = begin_pack_creation(sql, alice, "wip-c")
        _force_visibility(sql, pid, "public-read")
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, _p(bob), pid)
        assert pid not in readable_pack_ids(sql, _p(bob))


# ---------------------------------------------------------------------------
# 2. `ready` regression -- existing visibility rules still hold.
# ---------------------------------------------------------------------------


class TestReadyRegressionNone:
    def test_own_private_pack_is_visible(self, sql, alice):
        pid = create_pack(sql, alice, "priv")
        assert pid in readable_pack_ids(sql, _p(alice))

    def test_someone_elses_private_pack_is_not_visible(self, sql, alice, bob):
        pid = create_pack(sql, alice, "priv2")
        assert pid not in readable_pack_ids(sql, _p(bob))

    def test_someone_elses_public_pack_is_visible(self, sql, alice, bob):
        pid = create_pack(sql, alice, "pub")
        set_visibility(sql, _p(alice), pid, "public-read")
        assert pid in readable_pack_ids(sql, _p(bob))
        assert pid in {r["pack_id"] for r in list_packs_for(sql, _p(bob))}


# ---------------------------------------------------------------------------
# 3. Operator-precedence pin -- the parenthesised WHERE, not the unparenthesised
#    one that would let a public-but-incomplete pack leak through.
# ---------------------------------------------------------------------------


def test_someone_elses_public_creating_pack_is_not_visible(sql, alice, bob):
    """If the WHERE clause loses its parentheses (``status = 'ready' AND
    owner_id = :uid OR visibility != 'private'``), SQL's AND-before-OR
    precedence turns it into ``(status = 'ready' AND owner_id = :uid) OR
    (visibility != 'private')`` -- so a public row leaks through regardless
    of status. This test's whole point is to fail under that mutant: a
    creating pack, flipped to public-read, must still be invisible to a
    non-owner."""
    pid = begin_pack_creation(sql, alice, "creating-pub")
    _force_visibility(sql, pid, "public-read")
    assert pid not in readable_pack_ids(sql, _p(bob))
    assert pid not in {r["pack_id"] for r in list_packs_for(sql, _p(bob))}


# ---------------------------------------------------------------------------
# 4. Sudden-death reproduction -- begin_pack_creation alone, no follow-up call.
# ---------------------------------------------------------------------------


def test_begin_pack_creation_alone_appears_in_no_query(sql, alice):
    pid = begin_pack_creation(sql, alice, "sudden-death")
    alice_p = _p(alice)
    assert pid not in readable_pack_ids(sql, alice_p)
    assert pid not in {r["pack_id"] for r in list_packs_for(sql, alice_p)}
    assert pid not in read_scope(sql, alice_p)
    assert pid in {r["pack_id"] for r in list_incomplete_packs(sql)}


# ---------------------------------------------------------------------------
# 5. Anchor opt-out narrowing -- pack_anchor=True opens exactly one shape,
#    on exactly one status, and nothing else.
# ---------------------------------------------------------------------------


class TestAnchorOptOut:
    def test_wrong_space_raises_value_error(self, builder, sql, alice):
        pid = begin_pack_creation(sql, alice, "anchor-a")
        with pytest.raises(ValueError, match="space"), principal_scope(_p(alice)):
            builder.add_node(
                "subject", "Dataset", anchor_node_id(pid), {}, pack_id=pid, pack_anchor=True
            )

    def test_wrong_node_type_raises_value_error(self, builder, sql, alice):
        pid = begin_pack_creation(sql, alice, "anchor-b")
        with pytest.raises(ValueError, match="node_type"), principal_scope(_p(alice)):
            builder.add_node(
                "resource", "Document", anchor_node_id(pid), {}, pack_id=pid, pack_anchor=True
            )

    def test_wrong_node_id_raises_value_error(self, builder, sql, alice):
        pid = begin_pack_creation(sql, alice, "anchor-c")
        with pytest.raises(ValueError, match="anchor id"), principal_scope(_p(alice)):
            builder.add_node(
                "resource", "Dataset", "not-the-anchor-id", {}, pack_id=pid, pack_anchor=True
            )

    def test_correctly_shaped_anchor_is_refused_on_partial_pack(self, builder, sql, alice):
        pid = begin_pack_creation(sql, alice, "anchor-d")
        assert mark_pack_partial(sql, pid, alice) is True
        with pytest.raises(PackNotFoundError), principal_scope(_p(alice)):
            builder.add_node(
                "resource", "Dataset", anchor_node_id(pid), {}, pack_id=pid, pack_anchor=True
            )

    def test_correctly_shaped_anchor_is_refused_on_ready_pack(self, builder, sql, alice):
        pid = create_pack(sql, alice, "anchor-e")  # ready immediately, never went through creating
        with pytest.raises(PackNotFoundError), principal_scope(_p(alice)):
            builder.add_node(
                "resource", "Dataset", anchor_node_id(pid), {}, pack_id=pid, pack_anchor=True
            )

    def test_correctly_shaped_anchor_passes_on_creating_pack(self, builder, sql, alice):
        pid = begin_pack_creation(sql, alice, "anchor-f")
        with principal_scope(_p(alice)):
            out = builder.add_node(
                "resource", "Dataset", anchor_node_id(pid), {"title": "t"},
                pack_id=pid, pack_anchor=True,
            )
        assert out["stores"]["graph"] == "ok"

    def test_pack_anchor_false_on_creating_pack_is_refused(self, builder, sql, alice):
        """The default (pack_anchor=False) path stays ready-only -- a
        creating pack accepts writes ONLY through the pack_anchor=True door,
        never through an ordinary node write, even from its own owner."""
        pid = begin_pack_creation(sql, alice, "anchor-g")
        with pytest.raises(PackNotFoundError), principal_scope(_p(alice)):
            builder.add_node("resource", "Document", "some-node", {"title": "t"}, pack_id=pid)


# ---------------------------------------------------------------------------
# 6. State transition guards -- each WHERE pins its FROM state.
# ---------------------------------------------------------------------------


class TestStateTransitionGuards:
    def test_mark_pack_ready_does_not_promote_partial(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "trans-a")
        assert mark_pack_partial(sql, pid, alice) is True
        assert mark_pack_ready(sql, pid, alice) is False
        assert get_pack(sql, pid)["status"] == PACK_STATUS_PARTIAL

    def test_mark_pack_partial_does_not_demote_ready(self, sql, alice):
        pid = create_pack(sql, alice, "trans-b")
        assert mark_pack_partial(sql, pid, alice) is False
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY

    def test_promote_partial_pack_does_not_promote_creating(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "trans-c")
        assert promote_partial_pack(sql, pid, alice) is False
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

    def test_transitions_require_owner_match(self, sql, alice, bob):
        pid = begin_pack_creation(sql, alice, "trans-d")
        assert mark_pack_ready(sql, pid, bob) is False
        assert mark_pack_partial(sql, pid, bob) is False
        assert get_pack(sql, pid)["status"] == PACK_STATUS_CREATING

        pid2 = begin_pack_creation(sql, alice, "trans-e")
        assert mark_pack_partial(sql, pid2, alice) is True
        assert promote_partial_pack(sql, pid2, bob) is False
        assert get_pack(sql, pid2)["status"] == PACK_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# 7. delete_pack_row's only_status.
# ---------------------------------------------------------------------------


class TestDeletePackRowOnlyStatus:
    def test_only_status_creating_does_not_delete_ready_row(self, sql, alice):
        pid = create_pack(sql, alice, "del-a")
        assert delete_pack_row(sql, pid, alice, only_status=("creating",)) is False
        assert get_pack(sql, pid) is not None

    def test_only_status_creating_does_not_delete_partial_row(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "del-b")
        assert mark_pack_partial(sql, pid, alice) is True
        assert delete_pack_row(sql, pid, alice, only_status=("creating",)) is False
        assert get_pack(sql, pid) is not None

    def test_only_status_creating_deletes_a_creating_row(self, sql, alice):
        pid = begin_pack_creation(sql, alice, "del-c")
        assert delete_pack_row(sql, pid, alice, only_status=("creating",)) is True
        assert get_pack(sql, pid) is None

    def test_omitted_only_status_deletes_regardless_of_status(self, sql, alice):
        """Existing (pre-#170) behaviour: no only_status means status-blind
        delete, unchanged."""
        pid = create_pack(sql, alice, "del-d")
        assert delete_pack_row(sql, pid, alice) is True
        assert get_pack(sql, pid) is None


# ---------------------------------------------------------------------------
# 8. INSERT-exhaustiveness contract.
# ---------------------------------------------------------------------------


def test_every_insert_into_packs_names_status_explicitly():
    """The status column's DEFAULT 'ready' is deliberately fail-open-shaped
    (needed so the reverse migration -- an old source row with no status
    column -- resolves to the target's default). That safety net only works
    if the migration path is the ONLY place status is ever left implicit.
    Every production INSERT INTO packs must name status explicitly, or a
    future INSERT added without this contract test would silently register
    fresh rows as 'ready' no matter what the caller meant. Scans opencrab/
    only (test fixtures under tests/ deliberately simulate a pre-#170
    column-less schema and are exempt)."""
    root = Path(__file__).resolve().parent.parent / "opencrab"
    pattern = re.compile(r"INSERT\s+INTO\s+packs\s*\(([^)]*)\)", re.IGNORECASE)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        raw = path.read_text(encoding="utf-8")
        # Statements are commonly split across adjacent string literals
        # (`"INSERT INTO packs (a, b, "` \n `"c) "` \n `"VALUES (...)"`).
        # Stripping quotes/newlines and collapsing whitespace merges them
        # back into one logical statement before matching.
        collapsed = re.sub(r"""['"\n]""", " ", raw)
        collapsed = re.sub(r"\s+", " ", collapsed)
        for m in pattern.finditer(collapsed):
            cols = [c.strip() for c in m.group(1).split(",")]
            if "status" not in cols:
                offenders.append(f"{path.relative_to(root.parent)}: columns={cols}")
    assert offenders == [], (
        "INSERT INTO packs missing an explicit status column (fail-open -- "
        f"see ownership._insert_pack's docstring): {offenders}"
    )


# ---------------------------------------------------------------------------
# 9. Anchor-less `ready` is normal -- status == 'ready' does not imply an
#    anchor node exists anywhere (design v4 §3.2's two-branch definition).
# ---------------------------------------------------------------------------


class TestAnchorlessReadyIsNormal:
    def test_default_pack_is_ready_visible_and_writable_with_no_anchor(self, sql, alice):
        pid = ensure_default_pack(sql, alice)
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY
        assert pid in readable_pack_ids(sql, _p(alice))
        row = assert_writable(sql, _p(alice), pid)
        assert row["status"] == PACK_STATUS_READY
        # Never fed through builder.add_node(pack_anchor=True) -- status ==
        # 'ready' here means "visible and writable", not "anchor landed".

    def test_create_pack_row_is_ready_visible_and_writable_with_no_anchor(self, sql, alice):
        pid = create_pack(sql, alice, "no-anchor")
        assert get_pack(sql, pid)["status"] == PACK_STATUS_READY
        assert pid in readable_pack_ids(sql, _p(alice))
        row = assert_writable(sql, _p(alice), pid)
        assert row["status"] == PACK_STATUS_READY


# ---------------------------------------------------------------------------
# write_gate.authorize forwards allowed_statuses to assert_writable.
# ---------------------------------------------------------------------------


def test_write_gate_authorize_forwards_allowed_statuses(sql, alice):
    pid = begin_pack_creation(sql, alice, "gate-a")
    with pytest.raises(PackNotFoundError):
        gate_authorize(sql, _p(alice), pid)
    row = gate_authorize(sql, _p(alice), pid, allowed_statuses=(PACK_STATUS_CREATING,))
    assert row["status"] == PACK_STATUS_CREATING


class TestAmbiguousRegistryInsert:
    """An INSERT that raises means the outcome is UNKNOWN, not that no row
    exists (#170 PR review): a commit can succeed while its acknowledgement
    fails. Propagating blind would strand a committed row under an id nobody
    holds -- on a collision the id is a random suffix the caller never sees,
    and a `creating` row is absent from its owner's every listing, so nothing
    could reach it again."""

    def test_a_committed_row_is_recovered_from_a_raising_insert(self, sql, alice):
        """The row landed; only the answer was lost. That id must come back."""
        from unittest.mock import patch

        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.ownership import PACK_STATUS_CREATING, begin_pack_creation, get_pack

        real_insert = ownership_mod._insert_pack

        def _land_then_raise(sql_, pack_id, *args, **kwargs):
            real_insert(sql_, pack_id, *args, **kwargs)
            raise RuntimeError("connection dropped while acknowledging COMMIT")

        with patch.object(ownership_mod, "_insert_pack", side_effect=_land_then_raise):
            assigned = begin_pack_creation(sql, alice, "ambiguous")

        assert assigned == "ambiguous"
        row = get_pack(sql, assigned)
        assert row is not None
        assert row["owner_id"] == alice
        assert row["status"] == PACK_STATUS_CREATING

    def test_a_genuinely_failed_insert_still_raises(self, sql, alice):
        """Nothing landed, so the outcome is not 'unknown but fine' -- the
        caller must still see the failure rather than an id for no row."""
        from unittest.mock import patch

        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.ownership import begin_pack_creation, get_pack

        with patch.object(
            ownership_mod, "_insert_pack", side_effect=RuntimeError("registry is down")
        ):
            with pytest.raises(RuntimeError, match="registry is down"):
                begin_pack_creation(sql, alice, "never-landed")

        assert get_pack(sql, "never-landed") is None

    def test_a_foreign_row_under_that_id_is_not_claimed(self, sql, alice, bob):
        """The re-read must not hand back somebody else's row just because it
        occupies the id this call was trying for."""
        from unittest.mock import patch

        import opencrab.pack.ownership as ownership_mod
        from opencrab.pack.ownership import begin_pack_creation

        begin_pack_creation(sql, bob, "contested")

        with patch.object(
            ownership_mod, "_insert_pack", side_effect=RuntimeError("lost the answer")
        ):
            with pytest.raises(RuntimeError, match="lost the answer"):
                begin_pack_creation(sql, alice, "contested")
