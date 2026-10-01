"""#434: the pack DELETE entries authorize ownership before any store access.

`delete_pack` and `incremental_finalize` used to run only `require_live_data`.
Any bound principal could delete any pack's nodes, docs, chunks, edges and
vectors by knowing the pack name. Both now run `require_live_data`, a bound
principal, then an owner check, before they touch a store, the journal or the
lock file.

The spies record EVERY attribute access on the stores (a `hasattr` probe counts)
and every journal and lock call. A refused call must leave all recorders empty.
Counting only delete calls would pass a gate that sits after the first read.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from opencrab.auth import Principal, principal_scope
from opencrab.pack import delete_journal
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import (
    PackForbiddenError,
    PackNotFoundError,
    begin_pack_creation,
    create_pack,
    mark_pack_partial,
)
from opencrab.pack.write_gate import authorize_delete
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from tests._pack_fixtures import ensure_test_user
from tests.test_pack_load import _node, _write_jsonl

OWNER = "owner-u"
OTHER = "other-u"


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, is_local=True, disabled=False)


class _Spy:
    """Delegating proxy that records every attribute access."""

    def __init__(self, inner, log: list, label: str):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name):
        self._log.append(f"{self._label}.{name}")
        return getattr(self._inner, name)


class _NoVec:
    available = False

    def delete(self, ids):
        raise AssertionError("vector delete reached")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    for u in (OWNER, OTHER):
        ensure_test_user(sql, u)
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    log: list[str] = []
    calls = {"journal": [], "lock": []}

    real_load, real_save = delete_journal.load_journal, delete_journal.save_journal
    monkeypatch.setattr(
        delete_journal, "load_journal",
        lambda *a, **k: (calls["journal"].append("load"), real_load(*a, **k))[1])
    monkeypatch.setattr(
        delete_journal, "save_journal",
        lambda *a, **k: (calls["journal"].append("save"), real_save(*a, **k))[1])
    real_lock = pack_load.file_lock

    def _lock(*a, **k):
        calls["lock"].append(a)
        return real_lock(*a, **k)

    monkeypatch.setattr(pack_load, "file_lock", _lock)
    yield sql, graph, docs, log, calls, tmp_path
    graph.close()
    docs.close()


def _seed(sql, graph, docs, tmp_path, pack, n=10):
    """Load `n` nodes into a ready pack as its owner."""
    from opencrab.ontology.builder import OntologyBuilder

    create_pack(sql, OWNER, pack)
    f = _write_jsonl(tmp_path / f"{pack}.jsonl", [_node(id=f"n{i}") for i in range(n)])
    with principal_scope(_principal(OWNER)):
        pack_load.load_nodes(pack, f, OntologyBuilder(graph, docs, sql), {})


def _set_visibility(sql, pack, vis):
    with sql._engine.begin() as conn:
        conn.execute(text("UPDATE packs SET visibility = :v WHERE pack_id = :p"),
                     {"v": vis, "p": pack})


def _spies(graph, docs, log):
    return _Spy(graph, log, "graph"), _Spy(docs, log, "docs"), _Spy(_NoVec(), log, "vec")


def _assert_untouched(log, calls):
    assert log == [], f"a store was accessed before the gate refused: {log}"
    assert calls["journal"] == [], f"journal touched before the gate: {calls['journal']}"
    assert calls["lock"] == [], f"lock taken before the gate: {calls['lock']}"


def _finalize(graph, docs, vec, pack, sql):
    live = {"nodes": {}, "chunks": {}, "edges": set(), "vec_ids": set(), "doc_node_spaces": {}}
    return pack_load.incremental_finalize(
        pack, graph, docs, vec, live, {"n0"}, set(), set(), True, 1, 0, sql=sql)


def _live_nodes(graph, docs, pack):
    return set(pack_load.live_pack_state(pack, graph, docs, _NoVec())["nodes"])


# ───────────────────────── delete_pack ─────────────────────────


class TestDeletePackGate:
    @pytest.mark.parametrize("vis", ["private", "public-read", "public-fork"])
    def test_non_owner_cannot_delete_ready_pack(self, env, vis):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        _set_visibility(sql, "pk", vis)
        before = _live_nodes(graph, docs, "pk")
        g, d, v = _spies(graph, docs, log)
        log.clear()
        expected = PackNotFoundError if vis == "private" else PackForbiddenError
        with principal_scope(_principal(OTHER)):
            with pytest.raises(expected):
                pack_load.delete_pack("pk", g, d, v, sql=sql)
        _assert_untouched(log, calls)
        assert _live_nodes(graph, docs, "pk") == before

    @pytest.mark.parametrize("status", ["creating", "partial"])
    @pytest.mark.parametrize("vis", ["private", "public-read", "public-fork"])
    def test_non_owner_cannot_delete_or_see_incomplete_pack(self, env, status, vis):
        sql, graph, docs, log, calls, tmp = env
        begin_pack_creation(sql, OWNER, "inc")
        if status == "partial":
            assert mark_pack_partial(sql, "inc", OWNER)
        _set_visibility(sql, "inc", vis)
        g, d, v = _spies(graph, docs, log)
        log.clear()
        with principal_scope(_principal(OTHER)):
            with pytest.raises(PackNotFoundError):
                pack_load.delete_pack("inc", g, d, v, sql=sql)
        _assert_untouched(log, calls)

    def test_missing_pack_row_is_refused(self, env):
        sql, graph, docs, log, calls, _ = env
        g, d, v = _spies(graph, docs, log)
        with principal_scope(_principal(OWNER)):
            with pytest.raises(PackNotFoundError):
                pack_load.delete_pack("ghost", g, d, v, sql=sql)
        _assert_untouched(log, calls)

    def test_unbound_principal_is_refused_without_touching_anything(self, env):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        g, d, v = _spies(graph, docs, log)
        log.clear()
        with pytest.raises(RuntimeError, match="principal_scope"):
            pack_load.delete_pack("pk", g, d, v, sql=sql)
        _assert_untouched(log, calls)

    def test_unavailable_registry_fails_closed(self, env):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")

        class _DownSql:
            available = False

        g, d, v = _spies(graph, docs, log)
        log.clear()
        with principal_scope(_principal(OWNER)):
            with pytest.raises(RuntimeError, match="registry unavailable"):
                pack_load.delete_pack("pk", g, d, v, sql=_DownSql())
        _assert_untouched(log, calls)

    def test_live_data_guard_runs_first(self, env, monkeypatch):
        sql, graph, docs, log, calls, _ = env
        monkeypatch.delenv("LOCAL_DATA_DIR")
        g, d, v = _spies(graph, docs, log)
        with pytest.raises(SystemExit):
            pack_load.delete_pack("pk", g, d, v, sql=sql)
        _assert_untouched(log, calls)

    def test_owner_still_deletes_ready_pack(self, env):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        assert len(_live_nodes(graph, docs, "pk")) == 10
        with principal_scope(_principal(OWNER)):
            node_del, _c, _v = pack_load.delete_pack("pk", graph, docs, _NoVec(), sql=sql)
        assert node_del >= 10
        assert _live_nodes(graph, docs, "pk") == set()

    @pytest.mark.parametrize("status", ["creating", "partial"])
    def test_owner_can_reclaim_incomplete_pack(self, env, status):
        """Recovery path: residue of a failed fork or ingest sits in a non-ready pack."""
        sql, graph, docs, log, calls, tmp = env
        begin_pack_creation(sql, OWNER, "inc")
        if status == "partial":
            assert mark_pack_partial(sql, "inc", OWNER)
        with principal_scope(_principal(OWNER)):
            assert pack_load.delete_pack("inc", graph, docs, _NoVec(), sql=sql)[0] == 0

    def test_ownership_change_while_waiting_for_the_lock_is_refused(self, env, monkeypatch):
        """The in-lock check must read the registry again after the lock is held."""
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        before = _live_nodes(graph, docs, "pk")
        real_lock = pack_load.file_lock

        def _lock_then_transfer(*a, **k):
            with sql._engine.begin() as conn:
                conn.execute(text("UPDATE packs SET owner_id = :o WHERE pack_id = 'pk'"),
                             {"o": OTHER})
            return real_lock(*a, **k)

        monkeypatch.setattr(pack_load, "file_lock", _lock_then_transfer)
        with principal_scope(_principal(OWNER)):
            with pytest.raises((PackNotFoundError, PackForbiddenError)):
                pack_load.delete_pack("pk", graph, docs, _NoVec(), sql=sql)
        assert _live_nodes(graph, docs, "pk") == before
        assert calls["journal"] == [], "journal read before the in-lock check"


# ───────────────────────── incremental_finalize ─────────────────────────


class TestIncrementalFinalizeGate:
    @pytest.mark.parametrize("vis", ["private", "public-read", "public-fork"])
    def test_non_owner_cannot_finalize_ready_pack(self, env, vis):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        _set_visibility(sql, "pk", vis)
        before = _live_nodes(graph, docs, "pk")
        g, d, v = _spies(graph, docs, log)
        log.clear()
        expected = PackNotFoundError if vis == "private" else PackForbiddenError
        with principal_scope(_principal(OTHER)):
            with pytest.raises(expected):
                _finalize(g, d, v, "pk", sql)
        _assert_untouched(log, calls)
        assert _live_nodes(graph, docs, "pk") == before

    @pytest.mark.parametrize("status", ["creating", "partial"])
    def test_incomplete_pack_is_refused_even_for_owner(self, env, status):
        """Finalize is a writer: the ready-only default stays (not widened)."""
        sql, graph, docs, log, calls, _ = env
        begin_pack_creation(sql, OWNER, "inc")
        if status == "partial":
            assert mark_pack_partial(sql, "inc", OWNER)
        g, d, v = _spies(graph, docs, log)
        with principal_scope(_principal(OWNER)):
            with pytest.raises(PackNotFoundError):
                _finalize(g, d, v, "inc", sql)
        _assert_untouched(log, calls)

    def test_unbound_principal_is_refused_without_touching_anything(self, env):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        g, d, v = _spies(graph, docs, log)
        log.clear()
        with pytest.raises(RuntimeError, match="principal_scope"):
            _finalize(g, d, v, "pk", sql)
        _assert_untouched(log, calls)

    def test_live_data_guard_runs_first(self, env, monkeypatch):
        sql, graph, docs, log, calls, _ = env
        monkeypatch.delenv("LOCAL_DATA_DIR")
        g, d, v = _spies(graph, docs, log)
        with pytest.raises(SystemExit):
            _finalize(g, d, v, "pk", sql)
        _assert_untouched(log, calls)

    def test_owner_still_finalizes(self, env):
        sql, graph, docs, log, calls, tmp = env
        _seed(sql, graph, docs, tmp, "pk")
        live = pack_load.live_pack_state("pk", graph, docs, _NoVec())
        with principal_scope(_principal(OWNER)):
            res = pack_load.incremental_finalize(
                "pk", graph, docs, _NoVec(), live, {"n0"}, set(), set(), True, 1, 0, sql=sql)
        assert res["node_del"] == 9
        assert _live_nodes(graph, docs, "pk") == {"n0"}


# ───────────────────────── authorize_delete ─────────────────────────


class TestAuthorizeDelete:
    def test_decision_uses_exactly_one_registry_read(self, env, monkeypatch):
        """A second read would reopen a window between the mask and the owner check."""
        sql, *_ = env
        begin_pack_creation(sql, OWNER, "inc")
        import opencrab.pack.ownership as own

        calls = []
        real = own.get_pack
        monkeypatch.setattr(own, "get_pack", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
        with pytest.raises(PackNotFoundError):
            authorize_delete(sql, _principal(OTHER), "inc")
        assert authorize_delete(sql, _principal(OWNER), "inc")["pack_id"] == "inc"
        assert len(calls) == 2, f"expected one read per call, saw {len(calls)}"

    def test_paths_for_non_owner_to_reach_delete_are_zero(self, env):
        """Every (status, visibility) for a non-owner raises; nothing returns a row."""
        sql, *_ = env
        combos = 0
        for status in ("ready", "creating", "partial"):
            for vis in ("private", "public-read", "public-fork"):
                pid = f"{status}-{vis}"
                if status == "ready":
                    create_pack(sql, OWNER, pid)
                else:
                    begin_pack_creation(sql, OWNER, pid)
                    if status == "partial":
                        mark_pack_partial(sql, pid, OWNER)
                _set_visibility(sql, pid, vis)
                combos += 1
                with pytest.raises((PackNotFoundError, PackForbiddenError)):
                    authorize_delete(sql, _principal(OTHER), pid)
                # owner control
                assert authorize_delete(sql, _principal(OWNER), pid)["pack_id"] == pid
        assert combos == 9
