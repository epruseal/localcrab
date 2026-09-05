"""
issue #141: write.lock 소유권 지도에 남은 구멍 5건(+ 검증 중 발견한 6번째) 회귀.

각 테스트는 "잠금이 없으면 통과하지 않는다"는 검출력을 갖도록, 보호 대상
동작이 이미 write.lock 을 쥔 스레드가 있는 동안 진행되지 않고 대기함을
직접 관찰한다(스레드 기반 -- opencrab.locking.file_lock 의 프로세스 내
직렬화는 경로별 threading.RLock 을 모든 스레드가 공유하므로, 서브프로세스
없이도 실제 상호배제를 관찰할 수 있다).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from opencrab.locking import file_lock


class _LockHolder:
    """다른 스레드가 write.lock 을 쥔 상태를 만들고 신호로 풀어 준다."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with file_lock("write.lock", self.data_dir):
            self.holding.set()
            self.release.wait(timeout=5)

    def __enter__(self) -> _LockHolder:
        self.thread.start()
        assert self.holding.wait(timeout=5), "lock holder thread never acquired write.lock"
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=5)


def _assert_blocks_then_completes(target, *, poll: float = 0.3) -> None:
    """*target* (인자 없는 콜러블)을 별도 스레드에서 실행해, 호출 직후에는
    아직 끝나지 않았고(블록) 잠금 해제 후에는 끝남을 확인하는 공통 헬퍼."""
    done = threading.Event()
    error: list[BaseException] = []

    def runner() -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - 리레이즈해 테스트에서 보고
            error.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=poll)
    assert not done.is_set(), "보호 대상 동작이 write.lock 을 기다리지 않고 곧바로 끝났다"
    return t, done, error


# ---------------------------------------------------------------------------
# 항목 2: 로컬 SQLite 스토어 3종의 스키마 부트스트랩
# ---------------------------------------------------------------------------


def test_local_graph_store_init_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.stores.local_graph_store import LocalGraphStore

    data_dir = str(tmp_path)
    with _LockHolder(data_dir) as holder:
        t, done, error = _assert_blocks_then_completes(
            lambda: LocalGraphStore(db_path=str(tmp_path / "graph.db"))
        )
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_local_sql_doc_store_init_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    data_dir = str(tmp_path)
    with _LockHolder(data_dir) as holder:
        t, done, error = _assert_blocks_then_completes(
            lambda: LocalSQLDocStore(str(tmp_path / "doc_store.db"))
        )
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_sqlite_vec_store_init_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    data_dir = str(tmp_path)
    with _LockHolder(data_dir) as holder:
        t, done, error = _assert_blocks_then_completes(
            lambda: SqliteVecStore(
                db_path=str(tmp_path / "vectors.db"),
                embedding_function=lambda texts: [[0.0] * 8 for _ in texts],
                dim=8,
            )
        )
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_bootstrap_lock_is_reentrant_inside_held_write_lock(tmp_path: Path) -> None:
    """이미 같은 디렉터리의 write.lock 을 쥔 진입점(ingest 등) 안에서 스토어를
    생성해도 추가 대기 없이 곧바로 진행돼야 한다(재진입성)."""
    from opencrab.locking import write_lock
    from opencrab.stores.local_graph_store import LocalGraphStore

    data_dir = str(tmp_path)
    with write_lock(data_dir):
        store = LocalGraphStore(db_path=str(tmp_path / "graph.db"))
    assert store._available in (True, False)  # 데드락 없이 여기 도달하면 성공


# ---------------------------------------------------------------------------
# 항목 2 (SQLStore 어댑터) + 항목 6 (execution/_sql.py, IdentityEngine)
# ---------------------------------------------------------------------------


def test_sql_store_sqlite_create_tables_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.stores.sql_store import SQLStore

    db_path = tmp_path / "opencrab.db"
    with _LockHolder(str(tmp_path)) as holder:
        t, done, error = _assert_blocks_then_completes(
            lambda: SQLStore(url=f"sqlite:///{db_path}")
        )
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_write_lock_for_store_is_noop_for_postgres_and_memory() -> None:
    import contextlib

    from opencrab.stores.sql_store import write_lock_for_store

    class _Fake:
        def __init__(self, url: str, is_sqlite: bool) -> None:
            self._url = url
            self._is_sqlite = is_sqlite

    pg = _Fake("postgresql://user@host/db", is_sqlite=False)
    mem = _Fake("sqlite:///:memory:", is_sqlite=True)
    assert isinstance(write_lock_for_store(pg), contextlib.nullcontext)
    assert isinstance(write_lock_for_store(mem), contextlib.nullcontext)


def test_write_lock_for_store_parses_sqlalchemy_url_variants(tmp_path: Path) -> None:
    """removeprefix 수동 파싱이 아니라 make_url 을 쓰므로 쿼리스트링이 붙은
    URL 도 올바른 파일 경로를 뽑아낸다(설계 검증 R1 지적 반영)."""
    from opencrab.stores.sql_store import write_lock_for_store

    class _Fake:
        def __init__(self, url: str) -> None:
            self._url = url
            self._is_sqlite = True

    db_path = tmp_path / "sub" / "opencrab.db"
    fake = _Fake(f"sqlite+pysqlite:///{db_path}?timeout=5")
    with _LockHolder(str(db_path.parent)) as holder:
        t, done, error = _assert_blocks_then_completes(lambda: write_lock_for_store(fake).__enter__())
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_ensure_tables_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.execution._sql import ensure_tables
    from opencrab.stores.sql_store import SQLStore

    db_path = tmp_path / "opencrab.db"
    store = SQLStore(url=f"sqlite:///{db_path}")
    assert store.available
    ddl = ["CREATE TABLE IF NOT EXISTS _t141_probe (id INTEGER PRIMARY KEY)"]
    with _LockHolder(str(tmp_path)) as holder:
        t, done, error = _assert_blocks_then_completes(lambda: ensure_tables(store, ddl, ddl))
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


def test_identity_engine_ensure_tables_waits_for_write_lock(tmp_path: Path) -> None:
    from opencrab.ontology.identity import IdentityEngine
    from opencrab.stores.sql_store import SQLStore

    db_path = tmp_path / "opencrab.db"
    store = SQLStore(url=f"sqlite:///{db_path}")
    assert store.available
    with _LockHolder(str(tmp_path)) as holder:
        t, done, error = _assert_blocks_then_completes(lambda: IdentityEngine(store))
        holder.release.set()
        t.join(timeout=5)
    assert done.is_set()
    assert not error, error


# ---------------------------------------------------------------------------
# 항목 1: crabharness apply_promotion_package
# ---------------------------------------------------------------------------


def test_apply_promotion_package_holds_write_lock_across_live_write(tmp_path, monkeypatch) -> None:
    """스토어 생성부터 노드/엣지 쓰기까지 write.lock 안에서 돎을, 그 구간
    동안 (재진입이 아닌) 별도 스레드가 같은 디렉터리의 write.lock 을 얻을 수
    없음으로 확인한다. auth/pack 해석 등 무관한 의존은 모두 가짜로 바꿔
    이 테스트가 오직 잠금 구조만 검증하게 한다.

    apply_promotion_package 는 opencrab 컴포넌트를 함수 몸체 안에서
    ``from opencrab.X import Y`` 로 매 호출마다 새로 임포트하므로,
    ``crabharness.apply`` 모듈 자체의 속성이 아니라 각 원본 모듈(factory,
    builder, auth, config, pack.ownership)의 속성을 패치해야 실제로 적용
    된다.

    ``crabharness`` 는 opencrab 과 별개 배포 패키지라 이 테스트 스위트의
    conftest 가 그 임포트 경로를 마련해 두지 않는다 -- 저장소 루트가 이미
    ``sys.path`` 에 있으면 바깥쪽 ``crabharness/`` (프로젝트 디렉터리, 실제
    패키지는 그 밑의 ``crabharness/crabharness/``)가 __init__.py 없는
    네임스페이스 패키지로 잡혀버려 ``crabharness.apply`` 를 못 찾는다. 먼저
    캐시된 ``crabharness`` 계열 모듈을 지우고 프로젝트 디렉터리를 앞쪽에
    넣어야 진짜 패키지가 잡힌다.
    """
    import json
    import sys
    from pathlib import Path as _Path

    for _name in list(sys.modules):
        if _name == "crabharness" or _name.startswith("crabharness."):
            del sys.modules[_name]
    _crabharness_dir = str(_Path(__file__).resolve().parent.parent / "crabharness")
    if _crabharness_dir not in sys.path:
        sys.path.insert(0, _crabharness_dir)

    from crabharness.apply import apply_promotion_package

    import opencrab.auth as auth_mod
    import opencrab.config as config_mod
    import opencrab.ontology.builder as builder_mod
    import opencrab.pack.ownership as ownership_mod
    import opencrab.stores.factory as factory_mod


    package_path = tmp_path / "package.json"
    package_path.write_text(
        json.dumps({"package_id": "pkg-141", "mission_id": "m1", "run_id": "r1", "nodes": [], "edges": []}),
        encoding="utf-8",
    )

    lock_held_during_construction = threading.Event()
    checked = threading.Event()

    class _FakeSettings:
        local_data_dir = str(tmp_path)

    def fake_make_graph_store(settings):
        # 별도 스레드에서 같은 디렉터리 write.lock 을 즉시(0.05s 타임아웃)
        # 얻으려 시도한다. file_lock 의 프로세스 내 직렬화는 경로별
        # threading.RLock 을 스레드 구분 없이 공유하므로, 호출 스레드(테스트
        # 대상 코드)가 이미 그 락을 쥐고 있으면 별도 스레드는 얻지 못하고
        # TimeoutError -- 안 쥐고 있으면 곧바로 성공해 버린다.
        def probe() -> None:
            try:
                with file_lock("write.lock", str(tmp_path), timeout=0.05):
                    lock_held_during_construction.clear()
            except TimeoutError:
                lock_held_during_construction.set()
            finally:
                checked.set()

        p = threading.Thread(target=probe, daemon=True)
        p.start()
        p.join(timeout=2)
        return object()

    class _FakeBuilder:
        def __init__(self, **kwargs) -> None:
            pass

    monkeypatch.setattr(config_mod, "Settings", lambda: _FakeSettings())
    monkeypatch.setattr(factory_mod, "make_graph_store", fake_make_graph_store)
    monkeypatch.setattr(factory_mod, "make_doc_store", lambda settings: object())
    monkeypatch.setattr(factory_mod, "make_sql_store", lambda settings: object())
    monkeypatch.setattr(factory_mod, "make_billing_sql_store", lambda settings, sql: object())
    monkeypatch.setattr(builder_mod, "OntologyBuilder", _FakeBuilder)
    monkeypatch.setattr(auth_mod, "current_principal", lambda: "test-principal")
    monkeypatch.setattr(ownership_mod, "resolve_write_pack", lambda sql, principal, pack_id: "pack-1")

    apply_promotion_package(str(package_path))
    assert checked.is_set(), "락 점검 프로브가 실행되지 않았다"
    assert lock_held_during_construction.is_set(), (
        "apply_promotion_package 이 스토어 생성 시점에 write.lock 을 쥐고 있지 않다"
    )




# ---------------------------------------------------------------------------
# 항목 4/5: scripts/migrate_pack_ownership.py 의 write.lock 미획득과 sql 미종료
# ---------------------------------------------------------------------------

@pytest.fixture
def mpo_env(tmp_path: Path, monkeypatch):
    """scripts/ 는 패키지가 아니므로 sys.path 에 얹어 직접 import 한다
    (tests/test_migrate_pack_ownership.py 의 동일 관례). LOCAL_DATA_DIR 을
    격리된 tmp_path 로 고정하고 settings 캐시를 앞뒤로 비운다."""
    import sys as _sys

    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    get_settings.cache_clear()
    # main() 은 이미 부트스트랩된 로컬 사용자가 있어야 진행한다 -- 없으면
    # write.lock 진입 전에 SystemExit(2) 로 끝나 이 테스트들의 관찰 대상
    # (락 획득, sql 종료)에 도달하지 못한다.
    bootstrap_local_user(make_sql_store(get_settings()))
    yield tmp_path
    get_settings.cache_clear()


def test_migrate_pack_ownership_apply_waits_for_write_lock(mpo_env: Path, monkeypatch) -> None:
    """--apply 실행이 5단계 시퀀스를 main() 자신의 write.lock 으로 감쌈을
    확인한다(항목 4). 스토어 생성(SQLStore/LocalGraphStore/...)은 그 자체로도
    각자 write.lock 을 잠깐씩 잡았다 놓으므로(항목 2), 실스토어를 쓰면 그
    부수 효과가 먼저 걸려 main() 자신의 잠금이 실제로 기여하는지 가려진다
    (역변이로 확인: main() 의 잠금만 없애도 실스토어 버전은 여전히 통과했다).
    그래서 스토어 생성과 5단계 함수를 전부 가짜로 바꿔, 관찰되는 대기가
    오직 main() 이 그 시퀀스 둘레에 두른 write.lock 에서만 나오게 한다."""
    import migrate_pack_ownership as migrate  # type: ignore[import-not-found]

    import opencrab.stores.factory as factory_mod

    class _FakeStore:
        available = True

        def __getattr__(self, _name: str):
            def _noop(*_a, **_kw):
                return {}

            return _noop

    monkeypatch.setattr(migrate, "_bootstrap_owner_id", lambda sql: "owner-141")
    monkeypatch.setattr(factory_mod, "make_sql_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_graph_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_doc_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_vector_store", lambda settings: _FakeStore())
    monkeypatch.setattr(migrate, "_ensure_default_pack", lambda sql, owner_id, apply: ("default", 0))
    monkeypatch.setattr(migrate, "_graph_missing_node_ids", lambda graph: [])
    monkeypatch.setattr(migrate, "_local_graph_db_path", lambda settings: None)
    monkeypatch.setattr(migrate, "_backfill_graph", lambda settings, pack_id, apply: {})
    monkeypatch.setattr(migrate, "_stage_outcome", lambda stats, keys: ("clean", "nothing to do"))
    monkeypatch.setattr(migrate, "_register_graph_packs", lambda *a, **kw: {"candidates": []})
    monkeypatch.setattr(migrate, "_register_doc_packs", lambda *a, **kw: {"unregistered": 0})
    monkeypatch.setattr(migrate, "_backfill_doc", lambda docs, graph, pack_id, apply: {})
    monkeypatch.setattr(migrate, "_docs_stage_outcome", lambda stats: ("clean", "nothing to do"))
    monkeypatch.setattr(migrate, "_backfill_vector", lambda *a, **kw: {})

    with _LockHolder(str(mpo_env)) as holder:
        t, done, error = _assert_blocks_then_completes(lambda: migrate.main(["--apply", "--skip-backup"]))
        holder.release.set()
        t.join(timeout=10)
    assert done.is_set()
    assert not error, error


def test_migrate_pack_ownership_creates_vector_store_before_write_lock(
    mpo_env: Path, monkeypatch
) -> None:
    """항목 4 회귀 확인: 독립 검증(codex + fable verifier, 2026-09-05)이
    이전 버전에서 ``make_vector_store(settings)`` 가 ``write_lock`` 블록
    "안"에서 호출됨을 잡았다. 로컬 chroma 백엔드에서 그 생성자는 자기
    수명 동안 chroma.lock 을 쥐므로, 그 순서는 write.lock(바깥)/
    chroma.lock(안쪽) 이 되어 #140/#320 이 정한 반대 순서(chroma.lock 바깥/
    write.lock 안쪽)를 뒤집는다. 이 테스트는 두 호출을 실제 순서 그대로
    관찰해, make_vector_store 가 write_lock 진입보다 먼저 일어남을 직접
    확인한다(이전 테스트는 vector 생성 자체를 가짜로 뭉뚱그려 이 순서를
    검출하지 못했다 -- 그 공백이 이번 회귀를 놓친 근본 원인이었다)."""
    import migrate_pack_ownership as migrate  # type: ignore[import-not-found]

    import opencrab.locking as locking_mod
    import opencrab.stores.factory as factory_mod

    order: list[str] = []

    class _FakeStore:
        available = True

        def __getattr__(self, _name: str):
            def _noop(*_a, **_kw):
                return {}

            return _noop

    real_write_lock = locking_mod.write_lock

    def _tracking_write_lock(*a, **kw):
        order.append("write_lock_enter")
        return real_write_lock(*a, **kw)

    def _tracking_make_vector_store(settings):
        order.append("make_vector_store")
        return _FakeStore()

    monkeypatch.setattr(migrate, "_bootstrap_owner_id", lambda sql: "owner-141")
    monkeypatch.setattr(factory_mod, "make_sql_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_graph_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_doc_store", lambda settings: _FakeStore())
    monkeypatch.setattr(factory_mod, "make_vector_store", _tracking_make_vector_store)
    monkeypatch.setattr(locking_mod, "write_lock", _tracking_write_lock)
    monkeypatch.setattr(migrate, "_ensure_default_pack", lambda sql, owner_id, apply: ("default", 0))
    monkeypatch.setattr(migrate, "_graph_missing_node_ids", lambda graph: [])
    monkeypatch.setattr(migrate, "_local_graph_db_path", lambda settings: None)
    monkeypatch.setattr(migrate, "_backfill_graph", lambda settings, pack_id, apply: {})
    monkeypatch.setattr(migrate, "_stage_outcome", lambda stats, keys: ("clean", "nothing to do"))
    monkeypatch.setattr(migrate, "_register_graph_packs", lambda *a, **kw: {"candidates": []})
    monkeypatch.setattr(migrate, "_register_doc_packs", lambda *a, **kw: {"unregistered": 0})
    monkeypatch.setattr(migrate, "_backfill_doc", lambda docs, graph, pack_id, apply: {})
    monkeypatch.setattr(migrate, "_docs_stage_outcome", lambda stats: ("clean", "nothing to do"))
    monkeypatch.setattr(migrate, "_backfill_vector", lambda *a, **kw: {})

    rc = migrate.main(["--apply", "--skip-backup"])

    assert rc == 0
    assert order == ["make_vector_store", "write_lock_enter"], (
        "chroma.lock(벡터 스토어 생성)이 write.lock 보다 먼저 걸려야 하는데 "
        f"실제 호출 순서는 {order} 였다 (#140/#320 잠금 순서 위반)"
    )


def test_migrate_pack_ownership_closes_sql_store_on_every_exit_path(mpo_env: Path, monkeypatch) -> None:
    """sql 스토어가 (성공 경로뿐 아니라) 스테이지 실패로 인한 조기 반환
    경로에서도 닫힘을 확인한다(항목 5). SQLStore.close() 가 엔진을
    dispose 하므로, close 뒤에는 그 엔진의 커넥션 풀이 새 커넥션을 열
    수 없다 -- close 호출 여부를 이 관찰 가능한 부작용으로 검증한다."""
    import migrate_pack_ownership as migrate  # type: ignore[import-not-found]

    from opencrab.stores.sql_store import SQLStore

    created: list[SQLStore] = []
    real_init = SQLStore.__init__

    def _tracking_init(self, *a, **kw):
        real_init(self, *a, **kw)
        created.append(self)

    monkeypatch.setattr(SQLStore, "__init__", _tracking_init)

    def _boom(*a, **kw):
        raise RuntimeError("injected stage failure (issue #141 mutation probe)")

    monkeypatch.setattr(migrate, "_ensure_default_pack", _boom)

    rc = migrate.main(["--apply", "--skip-backup"])
    assert rc == 1
    assert created, "SQLStore 가 생성되지 않았다 -- 테스트 전제가 깨졌다"
    sql = created[0]
    assert sql._engine is not None
    assert sql._engine.pool.checkedin() == 0 or sql._available is False, (
        "스테이지 실패 후에도 sql 스토어가 닫히지 않았다(engine 이 여전히 살아 있다)"
    )
