"""
issue #578: SQLite 스토어 생성자가 write.lock 획득 시간 초과를 삼키면 일부 또는
전체 스토어가 불가용인 MCP 컨텍스트가 게시되고 재시작 전까지 남는다.

수리 뒤 계약: 스토어 부트스트랩의 write.lock 획득 시간 초과는 생성자 밖으로
전파된다(``TimeoutError`` 하위 클래스 ``StoreBootstrapLockTimeoutError``).
컨텍스트는 게시되지 않고, 잠금이 풀린 뒤 다음 호출이 재시작 없이 정상
컨텍스트를 만든다. 잠금과 무관한 초기화 실패는 종전대로 ``available=False``다.

이 모듈은 최상위에서 새 예외 클래스를 import하지 않는다. 수리 전 코드에서
import 오류가 아니라 "예외가 나지 않음"이라는 동작으로 실패해야 RED 증거가
되기 때문이다. 새 헬퍼를 직접 시험하는 H1만 함수 안에서 import한다.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from opencrab.config import get_settings
from opencrab.locking import file_lock

_TIMEOUT_NAME = "StoreBootstrapLockTimeoutError"
_SHORT_TIMEOUT = "0.3"


class _Holder:
    """다른 스레드가 write.lock 을 쥔 상태를 만들고 명시 신호가 있을 때만 푼다.

    ``tests/test_write_lock_ownership_141.py`` 의 홀더는 5초 뒤 스스로 푼다. 여기서는
    시간 초과 관찰 시점에 홀더가 살아 있음을 시험이 직접 단언하도록 자동 해제를
    두지 않는다(안전 상한은 스레드 daemon + 긴 wait 이다).
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with file_lock("write.lock", self.data_dir):
            self.holding.set()
            self.release.wait(timeout=120)

    def alive(self) -> bool:
        return self.thread.is_alive() and not self.release.is_set()

    def stop(self) -> None:
        self.release.set()
        self.thread.join(timeout=10)

    def __enter__(self) -> _Holder:
        self.thread.start()
        assert self.holding.wait(timeout=10), "홀더 스레드가 write.lock 을 잡지 못했다"
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


@pytest.fixture
def short_lock(monkeypatch):
    """WRITE_LOCK_TIMEOUT 을 줄이고 설정 캐시를 시험 안으로 격리한다."""
    monkeypatch.setenv("WRITE_LOCK_TIMEOUT", _SHORT_TIMEOUT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 스토어 네 종: 생성자 팩토리
# ---------------------------------------------------------------------------


def _make_graph(directory: Path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(db_path=str(directory / "graph.db"))


def _make_doc(directory: Path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(directory / "doc_store.db"))


def _make_vec(directory: Path):
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(
        db_path=str(directory / "vectors.db"),
        embedding_function=lambda texts: [[0.0] * 8 for _ in texts],
        dim=8,
    )


def _make_sql(directory: Path):
    from opencrab.stores.sql_store import SQLStore

    return SQLStore(url=f"sqlite:///{directory / 'opencrab.db'}")


_STORES = [
    pytest.param(_make_graph, "LocalGraphStore", id="graph"),
    pytest.param(_make_doc, "LocalSQLDocStore", id="doc"),
    pytest.param(_make_vec, "SqliteVecStore", id="vec"),
    pytest.param(_make_sql, "SQLStore", id="sql"),
]


@pytest.fixture
def cleanup_probe(monkeypatch):
    """실패한 생성 직후 정리 호출을 관측한다(재시도·시험 정리 전에 단언한다)."""
    from sqlalchemy.engine import Engine

    from opencrab.stores._sqlite_base import _SqliteConnMixin

    calls: list[str] = []
    real_close = _SqliteConnMixin.close
    real_dispose = Engine.dispose

    def spy_close(self):
        calls.append(f"close:{type(self).__name__}")
        return real_close(self)

    def spy_dispose(self, *args, **kwargs):
        calls.append("dispose:Engine")
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(_SqliteConnMixin, "close", spy_close)
    monkeypatch.setattr(Engine, "dispose", spy_dispose)
    return calls


@pytest.mark.parametrize("make,type_name", _STORES)
def test_store_constructor_propagates_write_lock_timeout_and_recovers(
    tmp_path, short_lock, cleanup_probe, make, type_name
):
    """정상 + 복구: 잠금이 쥐인 동안 생성은 예외를 올리고 부분 자원을 닫는다. 풀린
    뒤 같은 경로의 재생성은 가용 스토어다."""
    with _Holder(str(tmp_path)) as holder:
        with pytest.raises(TimeoutError, match="write.lock") as caught:
            make(tmp_path)
        assert holder.alive(), "시간 초과 관찰 시점에 홀더가 이미 풀려 있었다"
        assert type(caught.value).__name__ == _TIMEOUT_NAME
        # 실패한 생성 직후, 재시도 전에 관측한다.
        if type_name == "SQLStore":
            assert cleanup_probe == ["dispose:Engine"]
        else:
            assert cleanup_probe == [f"close:{type_name}"]
    store = make(tmp_path)
    try:
        assert store.available is True
    finally:
        store.close()


@pytest.mark.parametrize("make,type_name", _STORES)
def test_non_lock_init_failure_still_degrades_without_raising(tmp_path, short_lock, make, type_name):
    """대조군: 잠금이 아닌 초기화 실패(DB 파일 자리가 디렉터리)는 종전대로 예외 없이
    ``available=False`` 다. 이 변경은 잠금 시간 초과만 전파한다."""
    directory = tmp_path / "sub"
    directory.mkdir()
    victim = {
        "LocalGraphStore": "graph.db",
        "LocalSQLDocStore": "doc_store.db",
        "SqliteVecStore": "vectors.db",
        "SQLStore": "opencrab.db",
    }[type_name]
    (directory / victim).mkdir()
    store = make(directory)
    assert store.available is False


@pytest.mark.parametrize("make,type_name", _STORES)
def test_lock_held_in_another_data_dir_does_not_affect_construction(tmp_path, short_lock, make, type_name):
    """대조군: 다른 데이터 디렉터리의 write.lock 은 이 스토어 생성을 막지 않는다."""
    other = tmp_path / "other"
    mine = tmp_path / "mine"
    other.mkdir()
    mine.mkdir()
    with _Holder(str(other)):
        store = make(mine)
    try:
        assert store.available is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 헬퍼 단위
# ---------------------------------------------------------------------------


def test_store_bootstrap_lock_converts_only_the_acquisition_timeout():
    from opencrab.locking import StoreBootstrapLockTimeoutError, store_bootstrap_lock

    original = TimeoutError("acquisition timed out")

    @contextmanager
    def failing_acquisition():
        raise original
        yield  # pragma: no cover

    with pytest.raises(StoreBootstrapLockTimeoutError) as caught:
        with store_bootstrap_lock(failing_acquisition()):
            raise AssertionError("body must not run")
    assert isinstance(caught.value, TimeoutError)
    assert str(caught.value) == "acquisition timed out"
    assert caught.value.__cause__ is original

    body_error = TimeoutError("raised by the protected block")
    with pytest.raises(TimeoutError) as body_caught:
        with store_bootstrap_lock(nullcontext()):
            raise body_error
    assert body_caught.value is body_error
    assert type(body_caught.value) is TimeoutError

    @contextmanager
    def failing_for_other_reason():
        raise OSError("lock file could not be opened")
        yield  # pragma: no cover

    with pytest.raises(OSError, match="could not be opened"):
        with store_bootstrap_lock(failing_for_other_reason()):
            pass


# ---------------------------------------------------------------------------
# 컨텍스트: 진짜 _get_context 와 진짜 팩토리
# ---------------------------------------------------------------------------


@pytest.fixture
def real_context(tmp_path, monkeypatch):
    """진짜 ``_get_context`` 와 진짜 sqlite-vec 팩토리를 임시 데이터 디렉터리에서
    쓴다. 시험 시작 때만 컨텍스트와 설정 캐시를 비우고 실패와 재시도 사이에는
    비우지 않는다."""
    import opencrab.mcp.tools as tools_mod

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("VECTOR_BACKEND", "sqlite-vec")
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    monkeypatch.setenv("WRITE_LOCK_TIMEOUT", _SHORT_TIMEOUT)
    get_settings.cache_clear()
    monkeypatch.setattr(tools_mod, "_context", {})
    try:
        yield tools_mod
    finally:
        published = dict(tools_mod._context)
        for store in published.values():
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110 - teardown only
                    pass
        get_settings.cache_clear()


_CONTEXT_STORES = ("neo4j", "chroma", "mongo", "sql")


def test_degraded_context_is_not_published_and_recovers_without_restart(real_context, tmp_path):
    tools_mod = real_context
    with _Holder(str(tmp_path)) as holder:
        with pytest.raises(TimeoutError) as caught:
            tools_mod._get_context()
        assert holder.alive()
        assert type(caught.value).__name__ == _TIMEOUT_NAME
        assert tools_mod._context == {}, "저하된 컨텍스트가 게시되었다"
        assert tools_mod._context_init_owner is None

        # 잠금이 쥐인 동안 두 번째 호출도 같은 결과다(실패를 캐시하지 않는다).
        with pytest.raises(TimeoutError):
            tools_mod._get_context()
        assert tools_mod._context == {}

    ctx = tools_mod._get_context()
    for name in _CONTEXT_STORES:
        assert ctx[name].available is True, name
    assert tools_mod._get_context() is ctx


def test_tool_boundary_reports_clear_error_then_recovers(real_context, tmp_path, bind_test_principal):
    """도구 경계: _get_context 를 실제로 부르는 읽기 도구가 잠금 중에는 오류 봉투를
    돌려주고, 풀린 뒤 같은 프로세스에서 같은 호출이 정상 컨텍스트로 응답한다."""
    from opencrab.mcp.server import MCPServer

    tools_mod = real_context
    server = MCPServer()
    request = {"name": "ontology_get_node", "arguments": {"node_id": "no-such-node"}}

    with _Holder(str(tmp_path)) as holder:
        response = server._modern_tools_call(request)
        assert holder.alive()
    body = json.loads(response["content"][0]["text"])
    assert body == {"error": f"ontology_get_node failed ({_TIMEOUT_NAME})"}
    assert tools_mod._context == {}

    response = server._modern_tools_call(request)
    body = json.loads(response["content"][0]["text"])
    assert "failed (" not in json.dumps(body)
    for name in _CONTEXT_STORES:
        assert tools_mod._context[name].available is True, name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_optional_store_returns_none_on_lock_timeout_and_recovers(tmp_path, short_lock, monkeypatch):
    """``packs repair-registry``/``status`` 가 쓰는 ``_optional_store`` 의 계약은 "스토어 하나가
    안 열려도 명령은 계속"이다. 잠금 시간 초과는 ``SystemExit`` 가 아니라 ``None`` 으로
    끝나야 하고(호출자는 ``None`` 을 "알 수 없음"으로 처리한다), 풀린 뒤에는 가용 스토어다."""
    from opencrab import cli as cli_mod

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    cfg = get_settings()
    with _Holder(str(tmp_path)) as holder:
        assert cli_mod._optional_store(cfg, "graph") is None
        assert holder.alive()
    store = cli_mod._optional_store(cfg, "graph")
    try:
        assert store is not None and store.available is True
    finally:
        store.close()


def test_cli_make_stores_lets_the_lock_timeout_propagate(tmp_path, short_lock, monkeypatch):
    """직접 ``_make_stores`` 를 부르는 명령은 시간 초과 예외를 그대로 받는다. ``SystemExit`` 로
    바꾸면 ``_optional_store`` 의 ``except Exception`` 을 지나쳐 명령 전체가 끝난다."""
    from opencrab import cli as cli_mod

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    cfg = get_settings()
    with _Holder(str(tmp_path)) as holder:
        with pytest.raises(TimeoutError, match="write.lock") as caught:
            cli_mod._make_stores(cfg, graph=True)
        assert holder.alive()
    assert type(caught.value).__name__ == _TIMEOUT_NAME


# ---------------------------------------------------------------------------
# 시작 가드 세 곳: 그래프 생성자가 실패하면 먼저 만든 SQL 스토어를 닫는다
# ---------------------------------------------------------------------------


class _ClosableSql:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _install_guard_fakes(monkeypatch, *, error: BaseException):
    from opencrab.stores import factory as factory_mod

    sql = _ClosableSql()
    monkeypatch.setattr(factory_mod, "make_sql_store", lambda cfg: sql)

    def failing_graph(cfg):
        raise error

    monkeypatch.setattr(factory_mod, "make_graph_store", failing_graph)
    return sql


class _FakeLockTimeoutError(TimeoutError):
    """그래프 생성자가 올리는 잠금 시간 초과의 대역. 가드가 이 예외를 그대로 전파하고
    먼저 만든 스토어를 닫는지만 본다(실제 클래스는 스토어·컨텍스트 시험이 다룬다)."""


def test_http_startup_guard_closes_sql_store_when_graph_store_times_out(monkeypatch):
    from opencrab.mcp import http_app

    sql = _install_guard_fakes(monkeypatch, error=_FakeLockTimeoutError("timed out waiting for write.lock"))

    async def run() -> None:
        async with http_app._startup_registry_check(object()):
            raise AssertionError("startup must not complete")

    with pytest.raises(_FakeLockTimeoutError):
        asyncio.run(run())
    assert sql.closed == 1


def _bind_local_principal(monkeypatch):
    from opencrab import auth as auth_mod

    principal = auth_mod.Principal(user_id="test-user", is_local=True, disabled=False)
    monkeypatch.setattr(auth_mod, "refuse_stale_shared_secret_env", lambda: None)
    monkeypatch.setattr(auth_mod, "maybe_bootstrap_on_empty", lambda: principal)
    monkeypatch.setattr(auth_mod, "require_local_principal", lambda: principal)
    return principal


def test_stdio_server_guard_closes_sql_store_when_graph_store_times_out(monkeypatch):
    from opencrab.mcp import server as server_mod

    _bind_local_principal(monkeypatch)
    # buffer 속성이 없는 스트림이라 main() 의 UTF-8 재래핑 분기를 건너뛴다.
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    sql = _install_guard_fakes(monkeypatch, error=_FakeLockTimeoutError("timed out waiting for write.lock"))

    with pytest.raises(_FakeLockTimeoutError):
        server_mod.main()
    assert sql.closed == 1


def test_cli_serve_stdio_guard_closes_sql_store_when_graph_store_times_out(monkeypatch):
    from click.testing import CliRunner

    from opencrab.cli import main

    _bind_local_principal(monkeypatch)
    sql = _install_guard_fakes(monkeypatch, error=_FakeLockTimeoutError("timed out waiting for write.lock"))

    result = CliRunner().invoke(main, ["serve", "--transport", "stdio"])
    # CliRunner 는 일반 예외도 종료 코드 1로 바꾸므로 예외 종류와 정리를 함께 단언한다.
    assert isinstance(result.exception, _FakeLockTimeoutError), repr(result.exception)
    assert sql.closed == 1
