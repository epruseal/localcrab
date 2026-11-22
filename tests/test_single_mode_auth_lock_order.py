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


def test_t12_e4_classification(tmp_path, monkeypatch, single_registry):
    """T12-E4: SingleFileUnavailableError의 분류와 make_auth_sql_store의 sqlite3 오류 변환."""
    from opencrab.auth import token_store_unavailable_reason
    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile, SingleFileUnavailableError

    assert issubclass(SingleFileUnavailableError, RuntimeError)
    assert SingleFileUnavailableError("x").reason == "store-unavailable"
    assert token_store_unavailable_reason(SingleFileUnavailableError("x", reason="store-busy")) == "store-busy"
    assert token_store_unavailable_reason(SingleFileUnavailableError("x")) == "store-unavailable"
    # 대조군: 일반 RuntimeError와 B11의 root 거부는 분류하지 않는다.
    assert token_store_unavailable_reason(RuntimeError("single SQLite root requires SINGLE_SQLITE_FILE: x")) is None
    assert token_store_unavailable_reason(RuntimeError("anything")) is None

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    cases = {
        "busy": (_op_error(sqlite3.SQLITE_BUSY, "database is locked"), "store-busy"),
        "no_such_table": (sqlite3.OperationalError("no such table: single_file_metadata"), "store-unavailable"),
        "not_a_database": (sqlite3.DatabaseError("file is not a database"), "store-unavailable"),
    }
    for name, (raised, reason) in cases.items():
        def boom(self, *args, _exc=raised, **kwargs):
            raise _exc

        with monkeypatch.context() as patch:
            patch.setattr(SharedSqliteFile, "validate_binding", boom)
            with pytest.raises(SingleFileUnavailableError) as info:
                factory.make_auth_sql_store(settings)
        assert info.value.reason == reason, name
        assert info.value.__cause__ is raised, name
        assert _open_entries(single_registry) == []
    # 실측: 결속 검증이 metadata 표를 못 찾으면 sqlite3.OperationalError가 그대로 나온다(E3 사례 다1의 근거).
    _edit_single_db(built.root, "DROP TABLE single_file_metadata")
    with pytest.raises(SingleFileUnavailableError) as info:
        factory.make_auth_sql_store(settings)
    assert type(info.value.__cause__) is sqlite3.OperationalError
    assert "no such table" in str(info.value.__cause__)
    assert info.value.reason == "store-unavailable"


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


def test_t12_e9_bootstrap_single_branch(tmp_path, monkeypatch, single_registry):
    """T12-E9: 단일 분기는 열기 오류를 삼키지 않고 사용자를 만들지 않는다."""
    from opencrab import auth, locking
    from opencrab.stores._single_file import SingleFileUnavailableError

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    monkeypatch.setattr(locking, "default_lock_wait_timeout", lambda: 2.0)
    users = _user_count(built.root)

    # (마) 활성 사용자
    principal = auth.maybe_bootstrap_on_empty()
    assert principal is not None and principal.user_id == built.owner
    # (나) 수명 잠금 독점
    with _lifetime_lock_held(built.root):
        with pytest.raises(SingleFileUnavailableError) as info:
            auth.maybe_bootstrap_on_empty()
    assert info.value.reason == "store-busy"
    # (라) 비활성 사용자
    _edit_single_db(built.root, "UPDATE users SET disabled=1")
    assert auth.maybe_bootstrap_on_empty() is None
    # (다) 사용자 없음
    _edit_single_db(built.root, "UPDATE users SET disabled=0, is_local=0")
    assert auth.maybe_bootstrap_on_empty() is None
    assert _user_count(built.root) == users
    assert _open_entries(single_registry) == []
    # (가) 미승인 root
    _write_ledger_state(built.root, "ready")
    with pytest.raises(RuntimeError, match=_NOT_AUTHORIZED):
        auth.maybe_bootstrap_on_empty()


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
