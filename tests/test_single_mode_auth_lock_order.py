"""#671 B1~B3: 단일 모드의 인증 배선과 write.lock 순서 시험.

설계 4판 8절의 시험 목록이 정본이다. 함수 접두사는 test_t12_다. 합성 root는
tests/test_factory_single_file.py의 보조 함수를 재사용한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sqlite3
import subprocess
import sys

import httpx
import pytest

from tests.test_factory_single_file import (  # noqa: F401  (_isolate_single_registry는 autouse 고정물이다)
    _HOLD_LOCK_CODE,
    _authorized_and_pointed,
    _edit_single_db,
    _op_error,
    _open_entries,
    _single_env,
    _single_settings,
    _write_ledger_state,
)
from tests.test_factory_single_file import (
    _fresh_single_registry as _isolate_single_registry,  # noqa: F401
)


@pytest.fixture
def single_registry():
    """이 시험의 factory 등록부(경로별 공유 객체)다. autouse 고정물이 시험마다 새로 꽂는다."""
    from opencrab.stores import factory

    return factory._SINGLE_FILES


_WAIT = 20.0
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_SECRET = "t12-secret-token"
_NOT_AUTHORIZED = "single SQLite generation is not authorized"
_IN_UNIT = "single SQLite store construction cannot enter an active unit of work"
_SINGLE_NO_USER = "No local user exists in the single SQLite database"

# 인증 경로가 root에 만들면 안 되는 이름(BR1)과 만들 수 있는 새 이름.
FORBIDDEN_AUTH = frozenset({"opencrab.db", "bootstrap.lock", "write.lock", "billing.db.lock", ".env"})
ALLOWED_AUTH_NEW = frozenset({"single.db-wal", "single.db-shm"})


# ---------------------------------------------------------------------------
# 보조 함수
# ---------------------------------------------------------------------------


def _listing(root) -> set[str]:
    return set(os.listdir(root))


def _assert_new_names(before: set[str], after: set[str], allowed) -> None:
    extra = (after - before) - set(allowed)
    assert not extra, sorted(extra)


def _assert_auth_clean(root, before: set[str]) -> None:
    after = _listing(root)
    assert not (after & FORBIDDEN_AUTH), sorted(after & FORBIDDEN_AUTH)
    _assert_new_names(before, after, ALLOWED_AUTH_NEW)


def _add_token(root, user_id: str, secret: str = _SECRET) -> str:
    """게시 DB에 토큰 행을 직접 넣는다. 병합이 가져온 토큰과 같은 모양이다."""
    from opencrab.auth import hash_token

    _edit_single_db(
        root,
        "INSERT INTO api_tokens (token_id, user_id, token_hash, name) "
        f"VALUES ('t12-token', '{user_id}', '{hash_token(secret)}', 't12')",
    )
    return secret


def _user_count(root) -> int:
    conn = sqlite3.connect(root / "single.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def _file_hashes(root) -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(root)):
        path = root / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@contextlib.contextmanager
def _lifetime_lock_held(root):
    """다른 프로세스가 수명 잠금을 독점한다."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_CODE, str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        yield
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


@pytest.fixture
def http_env(monkeypatch):
    """HTTP 인증 작업 스레드와 경고 기록을 시험 하나에 가둔다."""
    from opencrab.mcp import http_app as http_mod

    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)
    monkeypatch.setattr(http_mod, "_UNAVAILABLE_LOGGED", {}, raising=False)
    try:
        yield http_mod
    finally:
        for pool in list(http_mod._POOLS.values()):
            stop = getattr(pool, "stop_for_tests", None)
            if stop is not None:
                stop()
            else:
                pool.shutdown(wait=True)


def _http_run(coro_fn, *, allow_query_token: bool = False):
    """새 앱에 대해 한 이벤트 루프 안에서 coro_fn(client)를 돌린다. lifespan은 돌지 않는다."""
    from opencrab.mcp.http_app import create_app

    async def main():
        app = create_app(allow_query_token=allow_query_token)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.wait_for(coro_fn(client), timeout=_WAIT * 2)

    return asyncio.run(main())


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _post_once(headers=None, params=None, *, allow_query_token: bool = False):
    async def go(client):
        return await client.post("/mcp", json=_TOOLS_LIST, headers=headers, params=params)

    return _http_run(go, allow_query_token=allow_query_token)


def _authorized_with_token(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _add_token(built.root, built.owner)
    _single_env(monkeypatch, built.root)
    return built


# ---------------------------------------------------------------------------
# 정상
# ---------------------------------------------------------------------------


def test_t12_n1_make_auth_sql_store_single(tmp_path, monkeypatch, single_registry):
    """T12-N1: 단일 모드 인증 저장소는 공유 파일 위에서 서고 닫으면 공유 객체가 닫힌다."""
    from opencrab.auth import get_local_user
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    sql = factory.make_auth_sql_store(settings)
    assert sql is not None and sql.available
    shared = single_registry[str(built.root / "single.db")]
    assert sql.shared_file is shared
    principal = get_local_user(sql)
    assert principal is not None and principal.user_id == built.owner
    sql.close()
    assert shared._closed


def test_t12_n2_require_local_principal_single(tmp_path, monkeypatch, single_registry):
    """T12-N2: 단일 모드에서 owner를 돌려주고 root에 금지 이름을 만들지 않는다."""
    from opencrab import auth

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    before = _listing(built.root)
    principal = auth.require_local_principal()
    assert principal.user_id == built.owner and not principal.disabled
    _assert_auth_clean(built.root, before)
    assert _open_entries(single_registry) == []


def test_t12_n3_maybe_bootstrap_on_empty_single(tmp_path, monkeypatch):
    """T12-N3: 단일 모드 bootstrap-on-empty는 opt-in이면 Principal이고 꺼져 있으면 None이다."""
    from opencrab import auth

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    before = _listing(built.root)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    principal = auth.maybe_bootstrap_on_empty()
    assert principal is not None and principal.user_id == built.owner
    _assert_auth_clean(built.root, before)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "")
    assert auth.maybe_bootstrap_on_empty() is None
    _assert_auth_clean(built.root, before)


def test_t12_n4_http_token_both_shapes(tmp_path, monkeypatch, http_env):
    """T12-N4: gateway와 authproxy 두 인스턴스 모양에서 토큰 200, 헤더 없음과 모르는 토큰 401."""
    built = _authorized_with_token(tmp_path, monkeypatch)
    before = _listing(built.root)
    assert _post_once(_bearer(_SECRET)).status_code == 200
    assert _post_once(None).status_code == 401
    assert _post_once(_bearer("t12-unknown")).status_code == 401
    assert _post_once(_bearer(_SECRET), allow_query_token=True).status_code == 200
    assert _post_once(None, {"token": _SECRET}, allow_query_token=True).status_code == 200
    assert _post_once(None, {"token": "t12-unknown"}, allow_query_token=True).status_code == 401
    # lifespan이 없는 HTTP 사례다. 인증 요청 경로는 write.lock을 만들지 않는다.
    after = _listing(built.root)
    assert "write.lock" not in after
    _assert_auth_clean(built.root, before)


# ---------------------------------------------------------------------------
# 오류
# ---------------------------------------------------------------------------

_E3_CASES = {
    "busy_lock": "store-busy",
    "ready_root": "store-unavailable",
    "missing_meta_table": "store-unavailable",
    "database_error": "store-unavailable",
    "busy_error": "store-busy",
}


@pytest.mark.parametrize("case", sorted(_E3_CASES))
def test_t12_e3_http_503_contract(tmp_path, monkeypatch, http_env, single_registry, case):
    """T12-E3: 열기 실패는 모두 503과 Retry-After 5다. 이유는 경고 기록의 키로 읽는다."""
    from opencrab import locking
    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_with_token(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "default_lock_wait_timeout", lambda: 2.0)
    hold = contextlib.nullcontext()
    if case == "busy_lock":
        hold = _lifetime_lock_held(built.root)
    elif case == "ready_root":
        _write_ledger_state(built.root, "ready")
    elif case == "missing_meta_table":
        _edit_single_db(built.root, "DROP TABLE single_file_metadata")
    elif case == "database_error":
        def boom(self, *args, **kwargs):
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr(SharedSqliteFile, "validate_binding", boom)
    else:
        def busy(self, *args, **kwargs):
            raise _op_error(sqlite3.SQLITE_BUSY, "database is locked")

        monkeypatch.setattr(SharedSqliteFile, "validate_binding", busy)
    with hold:
        resp = _post_once(_bearer(_SECRET))
    assert resp.status_code == 503, resp.status_code
    assert resp.headers["retry-after"] == "5"
    assert "www-authenticate" not in resp.headers
    assert set(http_env._UNAVAILABLE_LOGGED) == {_E3_CASES[case]}
    assert _open_entries(single_registry) == []


def test_t12_e4_classification():
    """T12-E4: SingleFileUnavailableError는 이유를 들고 있고 token_store_unavailable_reason이 그대로 돌려준다."""
    from opencrab.auth import token_store_unavailable_reason
    from opencrab.stores._single_file import SingleFileUnavailableError

    assert issubclass(SingleFileUnavailableError, RuntimeError)
    assert SingleFileUnavailableError("x").reason == "store-unavailable"
    assert token_store_unavailable_reason(SingleFileUnavailableError("x", reason="store-busy")) == "store-busy"
    assert token_store_unavailable_reason(SingleFileUnavailableError("x")) == "store-unavailable"
    # 대조군: 일반 RuntimeError와 B11의 root 거부는 분류하지 않는다.
    assert token_store_unavailable_reason(RuntimeError("single SQLite root requires SINGLE_SQLITE_FILE: x")) is None
    assert token_store_unavailable_reason(RuntimeError("anything")) is None


@pytest.mark.parametrize("case", ["busy", "no_such_table", "not_a_database", "real_missing_table"])
def test_t12_e4_direct_conversion(tmp_path, monkeypatch, single_registry, case):
    """T12-E4(직접 호출): make_auth_sql_store가 sqlite3 오류를 HTTP 없이 SingleFileUnavailableError로 바꾼다."""
    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile, SingleFileUnavailableError

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    injected = {
        "busy": (_op_error(sqlite3.SQLITE_BUSY, "database is locked"), "store-busy"),
        "no_such_table": (sqlite3.OperationalError("no such table: single_file_metadata"), "store-unavailable"),
        "not_a_database": (sqlite3.DatabaseError("file is not a database"), "store-unavailable"),
    }
    if case == "real_missing_table":
        # 실측: 결속 검증이 metadata 표를 못 찾으면 sqlite3.OperationalError가 그대로 나온다(E3 다1의 근거).
        _edit_single_db(built.root, "DROP TABLE single_file_metadata")
        raised, reason = None, "store-unavailable"
    else:
        raised, reason = injected[case]

        def boom(self, *args, **kwargs):
            raise raised

        monkeypatch.setattr(SharedSqliteFile, "validate_binding", boom)
    with pytest.raises(SingleFileUnavailableError) as info:
        factory.make_auth_sql_store(settings)
    assert info.value.reason == reason
    if raised is not None:
        assert info.value.__cause__ is raised
    else:
        assert type(info.value.__cause__) is sqlite3.OperationalError
        assert "no such table" in str(info.value.__cause__)
    assert _open_entries(single_registry) == []


def test_t12_e6_principal_states(tmp_path, monkeypatch, single_registry):
    """T12-E6: 비활성 사용자와 사용자 없음과 users 표 없음은 단일 모드 문구로 거부된다."""
    from opencrab import auth

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)

    _edit_single_db(built.root, "UPDATE users SET disabled=1")
    with pytest.raises(RuntimeError, match="is disabled"):
        auth.require_local_principal()
    assert _open_entries(single_registry) == []
    _edit_single_db(built.root, "UPDATE users SET disabled=0, is_local=0")
    for drop_table in (False, True):
        if drop_table:
            _edit_single_db(built.root, "DROP TABLE users")
        with pytest.raises(RuntimeError) as info:
            auth.require_local_principal()
        message = str(info.value)
        assert _SINGLE_NO_USER in message, message
        assert "opencrab init" not in message and "OPENCRAB_BOOTSTRAP_ON_EMPTY" not in message
        assert _open_entries(single_registry) == []


def test_t12_e7_auth_requests_make_no_new_files(tmp_path, monkeypatch, http_env):
    """T12-E7: lifespan을 마친 뒤의 인증 요청은 root에 새 파일을 만들지 않는다."""
    from fastapi.testclient import TestClient

    from opencrab.mcp.http_app import create_app

    built = _authorized_with_token(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        before = _listing(built.root)
        url = "/mcp"
        assert client.post(url, json=_TOOLS_LIST).status_code == 401
        assert client.post(url, json=_TOOLS_LIST, headers=_bearer("t12-unknown")).status_code == 401
        assert client.post(url, json=_TOOLS_LIST, headers=_bearer(_SECRET)).status_code == 200
        after = _listing(built.root)
    assert not ({"opencrab.db", "bootstrap.lock", ".env"} & after)
    _assert_new_names(before, after, ALLOWED_AUTH_NEW)


@pytest.mark.parametrize("case", ["a_ready_root", "b_lock_held", "c_no_user", "d_disabled_user", "e_active_user"])
def test_t12_e9_bootstrap_single_branch(tmp_path, monkeypatch, single_registry, case):
    """T12-E9: 단일 분기는 열기 오류를 삼키지 않고 사용자를 만들지 않는다."""
    from opencrab import auth, locking
    from opencrab.stores._single_file import SingleFileUnavailableError

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    # 기본 120초를 기다리지 않게 8초보다 짧게 둔다.
    monkeypatch.setattr(locking, "default_lock_wait_timeout", lambda: 2.0)
    users = _user_count(built.root)

    if case == "a_ready_root":
        _write_ledger_state(built.root, "ready")
        with pytest.raises(RuntimeError, match=_NOT_AUTHORIZED):
            auth.maybe_bootstrap_on_empty()
    elif case == "b_lock_held":
        with _lifetime_lock_held(built.root):
            with pytest.raises(SingleFileUnavailableError) as info:
                auth.maybe_bootstrap_on_empty()
        assert info.value.reason == "store-busy"
    elif case == "c_no_user":
        _edit_single_db(built.root, "UPDATE users SET is_local=0")
        assert auth.maybe_bootstrap_on_empty() is None
        assert _user_count(built.root) == users
    elif case == "d_disabled_user":
        _edit_single_db(built.root, "UPDATE users SET disabled=1")
        assert auth.maybe_bootstrap_on_empty() is None
        assert _user_count(built.root) == users
    else:
        principal = auth.maybe_bootstrap_on_empty()
        assert principal is not None and principal.user_id == built.owner
        assert _user_count(built.root) == users
    assert _open_entries(single_registry) == []


# ---------------------------------------------------------------------------
# 엣지
# ---------------------------------------------------------------------------


def test_t12_g3_non_single_mode_unchanged(tmp_path, monkeypatch, single_registry):
    """T12-G3: SINGLE_SQLITE_FILE이 없으면 make_auth_sql_store는 옛 규칙 그대로다."""
    from opencrab.config import Settings
    from opencrab.stores import factory
    from opencrab.stores.sql_store import SQLStore

    root = tmp_path / "plain"
    root.mkdir()
    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(root), SINGLE_SQLITE_FILE="")
    # hold_single_file은 비단일 모드에서 레지스트리와 root를 바꾸지 않는다.
    with factory.hold_single_file(settings):
        assert single_registry == {}
    assert os.listdir(root) == []
    assert factory.make_auth_sql_store(settings) is None
    assert os.listdir(root) == []
    SQLStore(f"sqlite:///{root}/opencrab.db").close()
    before = _listing(root)
    sql = factory.make_auth_sql_store(settings)
    try:
        assert sql is not None and sql.available
    finally:
        sql.close()
    assert _listing(root) == before
    assert single_registry == {}


def test_t12_g6_bootstrap_scope(tmp_path, monkeypatch):
    """T12-G6: 비단일 모드는 단일 root를 B11 문구로 거부하고 단일 모드는 같은 root에서 거부하지 않는다."""
    from opencrab import auth
    from opencrab.config import get_settings

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="single SQLite root requires SINGLE_SQLITE_FILE"):
        auth.maybe_bootstrap_on_empty()
    _single_env(monkeypatch, built.root)
    principal = auth.maybe_bootstrap_on_empty()
    assert principal is not None and principal.user_id == built.owner


def test_t12_g7_unavailable_store_is_closed(tmp_path, monkeypatch, http_env, single_registry):
    """T12-G7: 연결 실패로 비가용인 저장소는 닫히고 캐시되지 않으며 다음 요청은 성공한다."""
    from opencrab.stores.sql_store import SQLStore

    _authorized_with_token(tmp_path, monkeypatch)
    real_connect = SQLStore._connect

    async def go(client):
        with monkeypatch.context() as patch:
            patch.setattr(SQLStore, "_connect", lambda self: None)
            first = await client.post("/mcp", json=_TOOLS_LIST, headers=_bearer(_SECRET))
        owners_after_failure = _open_entries(single_registry)
        second = await client.post("/mcp", json=_TOOLS_LIST, headers=_bearer(_SECRET))
        return first, owners_after_failure, second

    first, owners, second = _http_run(go)
    assert SQLStore._connect is real_connect
    assert first.status_code == 503
    assert set(http_env._UNAVAILABLE_LOGGED) == {"store-unavailable"}
    assert owners == []
    assert second.status_code == 200


# ---------------------------------------------------------------------------
# 쓰기 경로와 write.lock 순서(D7~D10)
# ---------------------------------------------------------------------------

# 쓰기 경로가 root에 만들 수 있는 새 이름의 후보다. 실측이 후보 밖 이름을 보이면 설계를 개정한다.
ALLOWED_WRITE_NEW = frozenset(
    {"write.lock", "billing.db.lock", "billing.db-wal", "billing.db-shm", "single.db-wal", "single.db-shm"}
)
_FORBIDDEN_WRITE = frozenset({"opencrab.db", "bootstrap.lock", ".env"})


@pytest.fixture
def crabharness_importable(monkeypatch):
    """이 체크아웃의 crabharness 패키지를 import 경로 맨 앞에 두고, 끝나면 모듈 등록부를 되돌린다."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    removed = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "crabharness" or name.startswith("crabharness.")
    }
    monkeypatch.syspath_prepend(str(repo_root / "crabharness"))
    yield
    for name in list(sys.modules):
        if name == "crabharness" or name.startswith("crabharness."):
            del sys.modules[name]
    sys.modules.update(removed)


def _package_file(tmp_path, node_id: str = "t12-ds") -> str:
    import json

    package = {
        "package_id": "pkg-t12",
        "mission_id": "mis-t12",
        "run_id": "run-t12",
        "nodes": [{"space": "resource", "node_type": "Dataset", "node_id": node_id, "properties": {}}],
        "edges": [],
    }
    path = tmp_path / f"package-{node_id}.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    return str(path)


def _owner_scope(built):
    from opencrab.auth import Principal, principal_scope

    return principal_scope(Principal(user_id=built.owner, is_local=True, disabled=False))


@pytest.fixture
def fresh_tool_context(monkeypatch):
    """MCP 도구 컨텍스트를 비운 채 시작하고, 끝나면 만들어진 저장소를 닫는다."""
    from opencrab.mcp import tools as tools_pkg

    monkeypatch.setattr(tools_pkg, "_context", {})
    yield tools_pkg
    ctx = dict(tools_pkg._context)
    for key in ("neo4j", "chroma", "mongo", "sql"):
        close = getattr(ctx.get(key), "close", None)
        if callable(close):
            close()


def _dispatch_add_node(node_id: str):
    from opencrab.mcp.tools import dispatch_tool

    return dispatch_tool(
        "ontology_add_node",
        {"space": "resource", "node_type": "Dataset", "node_id": node_id, "properties": {}},
    )


def test_t12_n5_rest_token_on_single_root(tmp_path, monkeypatch, single_registry):
    """T12-N5: REST는 단일 root에서 컨텍스트를 만들고 토큰 있는 읽기 요청이 200, 토큰 없음이 401이다(K7)."""
    from fastapi.testclient import TestClient

    from apps.api import main as api

    built = _authorized_with_token(tmp_path, monkeypatch)
    with TestClient(api.app) as client:
        ok = client.get("/api/packs", headers=_bearer(_SECRET))
        anonymous = client.get("/api/packs")
        unknown = client.get("/api/packs", headers=_bearer("t12-unknown"))
    assert ok.status_code == 200, ok.text
    assert [pack["pack_id"] for pack in ok.json()["packs"]] == [built.pack_id]
    assert anonymous.status_code == 401
    assert unknown.status_code == 401


def test_t12_n6_first_write_call(tmp_path, monkeypatch, single_registry, fresh_tool_context):
    """T12-N6: 컨텍스트가 비어 있는 첫 호출이 쓰기 도구여도 수명 잠금 순서 위반 없이 쓰고 읽힌다."""
    from opencrab.mcp.tools import dispatch_tool

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    before = _listing(built.root)
    assert not fresh_tool_context._context
    with _owner_scope(built):
        result = _dispatch_add_node("t12-n6")
        assert result["node_id"] == "t12-n6", result
        got = dispatch_tool("ontology_get_node", {"node_id": "t12-n6"})
    assert got.get("node_id") == "t12-n6", got
    after = _listing(built.root)
    assert "write.lock" in after
    assert not (after & _FORBIDDEN_WRITE), sorted(after & _FORBIDDEN_WRITE)
    _assert_new_names(before, after, ALLOWED_WRITE_NEW)


def test_t12_n7_crabharness_apply_single(tmp_path, monkeypatch, single_registry, crabharness_importable):
    """T12-N7: 단일 모드에서 crabharness 승격 패키지 하나가 쓰이고 그래프에서 읽힌다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    # 병합 시험 바탕의 billing.db는 모양만 맞춘 한 열짜리 표를 가진다. 실제 billing.db는 BillingHooks가
    # 만든 표를 가지므로 그 가짜 표를 지워 BillingHooks가 제 표를 만들게 한다.
    conn = sqlite3.connect(built.root / "billing.db")
    try:
        conn.execute("DROP TABLE billing_events")
        conn.commit()
    finally:
        conn.close()
    before = _listing(built.root)
    from crabharness.apply import apply_promotion_package

    with _owner_scope(built):
        result = apply_promotion_package(_package_file(tmp_path), dry_run=False)
    assert result["summary"]["nodes_written"] == 1, result
    assert result["errors"] == [], result
    assert result["billing"]["ok"] is True, result
    graph = factory.make_graph_store(_single_settings(built.root))
    try:
        assert graph.get_node_by_id_scoped("t12-ds", [built.pack_id]) is not None
    finally:
        graph.close()
    after = _listing(built.root)
    assert not (after & _FORBIDDEN_WRITE), sorted(after & _FORBIDDEN_WRITE)
    _assert_new_names(before, after, ALLOWED_WRITE_NEW)


def test_t12_n8_concurrent_creation_is_serialized(tmp_path, monkeypatch, single_registry):
    """T12-N8: 같은 공유 파일에 훅을 등록하는 저장소 생성은 한 번에 하나씩만 acquire에 든다."""
    import threading

    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    real_acquire = SharedSqliteFile.acquire
    state = {"inside": 0, "max": 0}
    guard = threading.Lock()
    overlap = threading.Barrier(2, timeout=1.0)

    def counting(self, owner, hook):
        with guard:
            state["inside"] += 1
            state["max"] = max(state["max"], state["inside"])
        try:
            try:
                overlap.wait()
            except threading.BrokenBarrierError:
                pass
            return real_acquire(self, owner, hook)
        finally:
            with guard:
                state["inside"] -= 1

    monkeypatch.setattr(SharedSqliteFile, "acquire", counting)
    makers = {
        "graph": factory.make_graph_store,
        "docs": factory.make_doc_store,
        "sql": factory.make_sql_store,
        "auth": factory.make_auth_sql_store,
    }
    start = threading.Barrier(len(makers), timeout=10)
    stores: dict = {}
    errors: list = []

    def run(name, make):
        try:
            start.wait()
            stores[name] = make(settings)
        except BaseException as exc:  # noqa: BLE001
            errors.append((name, exc))

    threads = [threading.Thread(target=run, args=item, name=f"t12-n8-{item[0]}") for item in makers.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_WAIT * 2)
    try:
        assert not any(thread.is_alive() for thread in threads)
        assert errors == [], errors
        assert sorted(stores) == sorted(makers)
        assert state["max"] == 1, state
    finally:
        for store in stores.values():
            store.close()
    assert _open_entries(single_registry) == []


def test_t12_n9_owner_registration_does_not_hold_registry_lock(tmp_path, monkeypatch, single_registry):
    """T12-N9: 소유자 등록은 레지스트리 잠금 밖이다. 다른 스레드의 쓰기 단위를 기다리는 동안 그 잠금은 비어 있다."""
    import threading

    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    path = settings.single_sqlite_file
    shared = factory._open_single_file(path)
    base_owner = object()
    shared.acquire(base_owner, None)
    unit_entered, release_unit = threading.Event(), threading.Event()
    v_acquire, v_done, w_done = threading.Event(), threading.Event(), threading.Event()
    problems: list = []

    def hold_unit():
        try:
            with shared.unit_of_work():
                unit_entered.set()
                release_unit.wait(10)
        except BaseException as exc:  # noqa: BLE001
            problems.append(exc)

    def register():
        try:
            with factory.hold_single_file(settings):
                pass
        except BaseException as exc:  # noqa: BLE001
            problems.append(exc)
        finally:
            v_done.set()

    def read_registry():
        try:
            factory._open_single_file(path)
            with factory._SINGLE_FILES_LOCK:
                pass
        except BaseException as exc:  # noqa: BLE001
            problems.append(exc)
        finally:
            w_done.set()

    unit = threading.Thread(target=hold_unit, name="t12-n9-unit")
    unit.start()
    assert unit_entered.wait(10)
    real_acquire = SharedSqliteFile.acquire
    registrar = threading.Thread(target=register, name="t12-n9-v")

    def signalling(self, owner, hook):
        if threading.current_thread() is registrar:
            v_acquire.set()
        return real_acquire(self, owner, hook)

    monkeypatch.setattr(SharedSqliteFile, "acquire", signalling)
    reader = threading.Thread(target=read_registry, name="t12-n9-w")
    try:
        registrar.start()
        assert v_acquire.wait(10)
        reader.start()
        # 단언 하나: 쓰기 단위가 열려 있어도 레지스트리만 읽는 호출은 끝난다.
        assert w_done.wait(5), "registry reader is blocked behind owner registration"
        # 단언 둘: 소유자 등록은 단위가 끝나기 전에는 끝나지 않는다.
        assert not v_done.wait(0.3)
    finally:
        release_unit.set()
        for thread in (unit, registrar, reader):
            if thread.ident is not None:
                thread.join(timeout=20)
    assert v_done.is_set()
    assert problems == [], problems
    shared.release(base_owner)


def _run_entry(entry: str, built, tmp_path):
    """여섯 진입 가운데 하나를 부른다. HTTP는 응답을, 나머지는 반환하지 않고 예외를 올린다."""
    from opencrab import auth
    from opencrab.stores import factory

    if entry == "require_local_principal":
        return auth.require_local_principal()
    if entry == "maybe_bootstrap_on_empty":
        return auth.maybe_bootstrap_on_empty()
    if entry == "http":
        return _post_once(_bearer(_SECRET))
    if entry == "hold_single_file":
        with factory.hold_single_file(_single_settings(built.root)):
            return None
    if entry == "dispatch":
        with _owner_scope(built):
            return _dispatch_add_node("t12-entry")
    from crabharness.apply import apply_promotion_package

    with _owner_scope(built):
        return apply_promotion_package(_package_file(tmp_path), dry_run=False)


_ENTRIES = [
    "require_local_principal",
    "maybe_bootstrap_on_empty",
    "http",
    "hold_single_file",
    "dispatch",
    "crabharness",
]


@pytest.mark.parametrize("entry", _ENTRIES)
def test_t12_e1_ready_root_is_refused_before_write_lock(
    tmp_path, monkeypatch, http_env, single_registry, fresh_tool_context, crabharness_importable, entry
):
    """T12-E1: 미승인(ready) root는 여섯 진입이 모두 write.lock 앞에서 거부하고 root는 그대로다."""
    built = _authorized_with_token(tmp_path, monkeypatch)
    _write_ledger_state(built.root, "ready")
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    before = _listing(built.root)
    hashes = _file_hashes(built.root)

    if entry == "http":
        assert _run_entry(entry, built, tmp_path).status_code == 503
    else:
        with pytest.raises(RuntimeError, match=_NOT_AUTHORIZED):
            _run_entry(entry, built, tmp_path)
    assert _listing(built.root) == before
    assert _file_hashes(built.root) == hashes
    assert "write.lock" not in _listing(built.root)
    assert _open_entries(single_registry) == []


@pytest.mark.parametrize("variant", ["relative_path", "other_directory"])
@pytest.mark.parametrize("entry", ["dispatch", "crabharness"])
def test_t12_e2_bad_settings_are_refused_before_write_lock(
    tmp_path, monkeypatch, single_registry, fresh_tool_context, crabharness_importable, entry, variant
):
    """T12-E2: 설정 값 오류는 write.lock 앞에서 거부된다."""
    from opencrab.config import get_settings

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    other = tmp_path / "elsewhere"
    other.mkdir()
    bad = "single.db" if variant == "relative_path" else str(other / "single.db")
    monkeypatch.setenv("SINGLE_SQLITE_FILE", bad)
    get_settings.cache_clear()
    before = _listing(built.root)
    with pytest.raises(RuntimeError, match="requires LOCAL_DATA_DIR to be the database directory"):
        _run_entry(entry, built, tmp_path)
    assert _listing(built.root) == before
    assert "write.lock" not in _listing(built.root)


@pytest.mark.parametrize("command", ["backfill-pack-id", "source-fts-orphans"])
def test_t12_e5_packs_commands_refuse_in_single_mode(tmp_path, monkeypatch, single_registry, command):
    """T12-E5: 단일 모드의 packs --apply 둘은 명시 문구로 거부되고 root는 그대로다."""
    import shutil

    from click.testing import CliRunner

    from opencrab.cli import main

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    for name in ("graph.db", "doc_store.db"):
        shutil.copy2(tmp_path / "live" / name, built.root / name)
    before = _listing(built.root)
    hashes = _file_hashes(built.root)
    result = CliRunner().invoke(main, ["packs", command, "--apply"])
    assert result.exit_code == 1, (result.exit_code, result.output, result.exception)
    flat = " ".join(result.output.split())
    assert "SINGLE_SQLITE_FILE is set" in flat, flat
    assert "writes are not supported in single mode" in flat, flat
    assert _listing(built.root) == before
    assert _file_hashes(built.root) == hashes
    assert "write.lock" not in _listing(built.root)


def test_t12_g1_hold_makes_first_call_work(tmp_path, monkeypatch, single_registry):
    """T12-G1: hold_single_file 아래에서는 write.lock을 쥔 채 첫 factory 호출이 통과한다. 대조군은 거부된다."""
    from opencrab.locking import write_lock
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    with factory.hold_single_file(settings):
        with write_lock(str(built.root)):
            sql = factory.make_sql_store(settings)
            sql.close()
    with write_lock(str(built.root)):
        with pytest.raises(RuntimeError, match="lifetime lock order violated"):
            factory.make_sql_store(settings)


def test_t12_g2_owner_release(tmp_path, monkeypatch, single_registry):
    """T12-G2: 블록이 끝나면 소유자가 풀린다. 다른 소유자가 없으면 공유 객체가 닫히고 있으면 열린 채 남는다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    path = settings.single_sqlite_file
    with factory.hold_single_file(settings):
        assert not single_registry[path]._closed
    assert single_registry[path]._closed
    sql = factory.make_sql_store(settings)
    try:
        shared = single_registry[path]
        users = len(shared._users)
        with factory.hold_single_file(settings):
            assert len(shared._users) == users + 1
        assert not shared._closed
        assert len(shared._users) == users
    finally:
        sql.close()
    assert shared._closed


def test_t12_g4_reentrant_hold(tmp_path, monkeypatch, single_registry):
    """T12-G4: hold_single_file을 두 겹으로 감싸도 통과하고 둘 다 풀린다."""
    from opencrab.locking import write_lock
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    with factory.hold_single_file(settings):
        shared = single_registry[settings.single_sqlite_file]
        with factory.hold_single_file(settings):
            assert len(shared._users) == 2
            with write_lock(str(built.root)):
                sql = factory.make_sql_store(settings)
                sql.close()
            assert not shared._closed
        assert len(shared._users) == 1
    assert shared._closed and not shared._users


def test_t12_g5_context_is_reused(tmp_path, monkeypatch, single_registry, fresh_tool_context):
    """T12-G5: 쓰기 도구를 두 번 부르면 두 번째는 같은 컨텍스트를 쓰고 공유 객체는 열린 채 남는다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    with _owner_scope(built):
        _dispatch_add_node("t12-g5-a")
        context = fresh_tool_context._context
        shared = single_registry[str(built.root / "single.db")]
        users = len(shared._users)
        _dispatch_add_node("t12-g5-b")
    assert fresh_tool_context._context is context
    assert not shared._closed
    assert len(shared._users) == users


@pytest.mark.parametrize(
    "name", ["make_sql_store", "make_auth_sql_store", "hold_single_file", "make_billing_sql_store"]
)
def test_t12_g8_construction_inside_unit_is_refused(tmp_path, monkeypatch, single_registry, name):
    """T12-G8: 작업 단위 안의 저장소 생성은 거부된다. 인증 저장소는 503으로 가려지지 않는 RuntimeError다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    sql = factory.make_sql_store(settings)
    try:
        def call():
            if name == "make_sql_store":
                return factory.make_sql_store(settings)
            if name == "make_auth_sql_store":
                return factory.make_auth_sql_store(settings)
            if name == "hold_single_file":
                with factory.hold_single_file(settings):
                    return None
            return factory.make_billing_sql_store(settings, sql)

        with sql.shared_file.unit_of_work():
            with pytest.raises(RuntimeError) as info:
                call()
        assert _IN_UNIT in str(info.value), str(info.value)
        assert type(info.value) is RuntimeError, type(info.value)
        # 대조군: 단위 밖에서는 같은 호출이 통과한다.
        result = call()
        close = getattr(result, "close", None)
        if callable(close):
            close()
    finally:
        sql.close()


class _TimedRLock:
    """acquire에 제한 시간을 둔 RLock 대역이다. 시간이 지나면 교착으로 보고 AssertionError를 올린다."""

    def __init__(self, seconds: float) -> None:
        import threading

        self._lock = threading.RLock()
        self._seconds = seconds

    def acquire(self, blocking=True, timeout=-1):
        if not self._lock.acquire(timeout=self._seconds):
            raise AssertionError("deadlock")
        return True

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()


@pytest.mark.parametrize("target", ["open_single_file", "make_billing_sql_store"])
def test_t12_g10_unit_and_creator_do_not_deadlock(tmp_path, monkeypatch, single_registry, target):
    """T12-G10: 쓰기 단위를 쥔 스레드가 저장소 생성 스레드와 서로 기다리는 교착이 서지 않는다."""
    import threading

    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    path = settings.single_sqlite_file
    sql = factory.make_sql_store(settings)
    shared = sql.shared_file
    monkeypatch.setattr(factory, "_SINGLE_FILES_LOCK", _TimedRLock(10.0))
    unit_entered, t2_in_acquire = threading.Event(), threading.Event()
    t1_error: list = []
    t2_result: list = []
    t2_error: list = []

    def creator():
        try:
            if not unit_entered.wait(10):
                raise AssertionError("unit never entered")
            t2_result.append(factory.make_graph_store(settings))
        except BaseException as exc:  # noqa: BLE001
            t2_error.append(exc)

    t2 = threading.Thread(target=creator, name="t12-g10-creator")
    real_acquire = SharedSqliteFile.acquire

    def signalling(self, owner, hook):
        if threading.current_thread() is t2:
            t2_in_acquire.set()
        return real_acquire(self, owner, hook)

    monkeypatch.setattr(SharedSqliteFile, "acquire", signalling)

    def unit_holder():
        try:
            with shared.unit_of_work():
                unit_entered.set()
                if not t2_in_acquire.wait(10):
                    raise AssertionError("creator never reached acquire")
                if target == "open_single_file":
                    factory._open_single_file(path)
                else:
                    factory.make_billing_sql_store(settings, sql)
        except BaseException as exc:  # noqa: BLE001
            t1_error.append(exc)

    t1 = threading.Thread(target=unit_holder, name="t12-g10-unit")
    try:
        t2.start()
        t1.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()
        assert len(t1_error) == 1 and type(t1_error[0]) is RuntimeError, t1_error
        assert "cannot enter an active unit of work" in str(t1_error[0])
        assert t2_error == [] and len(t2_result) == 1, t2_error
    finally:
        for store in t2_result:
            store.close()
        sql.close()


# ---------------------------------------------------------------------------
# init(D12)
# ---------------------------------------------------------------------------

_INIT_REFUSAL = "init does not support SINGLE_SQLITE_FILE"


def _init_env(monkeypatch, tmp_path) -> None:
    """표준 위치의 env 파일을 읽지 않게 해 시험 결과가 머신에 기대지 않게 한다."""
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(tmp_path / "no-such.env"))


@pytest.mark.parametrize("variant", ["a_cwd_outside_root", "b_cwd_is_root", "c_dotenv_with_force", "d_broken_settings"])
def test_t12_e8_init_refuses_single_mode(tmp_path, monkeypatch, single_registry, variant):
    """T12-E8: 단일 모드 init은 .env를 만들기 전에 거부한다. 설정이 깨졌으면 예외를 올린다(fail-closed)."""
    from click.testing import CliRunner
    from pydantic import ValidationError

    from opencrab.cli import main
    from opencrab.config import get_settings

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    _init_env(monkeypatch, tmp_path)
    root_before = _listing(built.root)
    hashes = _file_hashes(built.root)
    work = tmp_path / "work"
    work.mkdir()

    def assert_root_untouched():
        assert _listing(built.root) == root_before
        assert _file_hashes(built.root) == hashes

    def invoke(*args):
        return CliRunner().invoke(main, list(args))

    def assert_refused(result):
        assert result.exit_code == 1, (result.exit_code, result.output, result.exception)
        assert _INIT_REFUSAL in " ".join(result.output.split()), result.output

    if variant == "a_cwd_outside_root":
        # cwd가 root 밖의 빈 디렉터리다.
        monkeypatch.chdir(work)
        assert_refused(invoke("init"))
        assert os.listdir(work) == []
    elif variant == "b_cwd_is_root":
        # cwd가 root 자신이다. root에 .env와 write.lock이 생기면 안 된다.
        monkeypatch.chdir(built.root)
        assert_refused(invoke("init"))
    elif variant == "c_dotenv_with_force":
        # 환경변수 없이 cwd의 .env에만 SINGLE_SQLITE_FILE 줄이 있다. --force도 그 .env를 덮어쓰지 않는다.
        env_text = f"SINGLE_SQLITE_FILE={built.root / 'single.db'}\n"
        (work / ".env").write_text(env_text)
        monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
        get_settings.cache_clear()
        monkeypatch.chdir(work)
        assert_refused(invoke("init", "--force"))
        assert (work / ".env").read_text() == env_text
    else:
        # 설정이 깨졌다. 단일 모드 설정이 없는 환경이고 init은 검증 오류를 그대로 올린다.
        monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
        monkeypatch.setenv("WRITE_LOCK_TIMEOUT", "not-a-number")
        monkeypatch.chdir(work)
        result = invoke("init")
        assert result.exit_code != 0
        assert isinstance(result.exception, ValidationError), repr(result.exception)
        assert os.listdir(work) == []
    assert_root_untouched()


def test_t12_g9_init_outside_single_mode_is_unchanged(tmp_path, monkeypatch):
    """T12-G9: 비단일 모드 init은 .env를 만들고, 그 전에 get_settings 캐시를 채우지 않는다."""
    from click.testing import CliRunner

    from opencrab import cli as cli_mod
    from opencrab.config import get_settings

    _init_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    monkeypatch.delenv("WRITE_LOCK_TIMEOUT", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    get_settings.cache_clear()
    seen = {}

    def fake_bootstrap():
        seen["env_exists"] = (work / ".env").exists()
        seen["cache_size"] = get_settings.cache_info().currsize

    monkeypatch.setattr(cli_mod, "_bootstrap_local_user", fake_bootstrap)
    result = CliRunner().invoke(cli_mod.main, ["init"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert seen == {"env_exists": True, "cache_size": 0}
