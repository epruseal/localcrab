"""chroma.lock ownership: every local PersistentClient owner is excluded (#140).

Before this change only the MCP tool layer took ``chroma.lock``, so the REST
app, the CLI, and the migration scripts opened a second ``PersistentClient`` on
the same persist directory with no exclusion at all. Ownership now sits on the
``ChromaStore`` local-mode instance, so every entry point that goes through the
factory is covered automatically.

Every test here uses ``tmp_path``; nothing touches a real data directory.

Reverse-mutation note (which tests are RED against the pre-fix code):
  RED  : test_live_store_blocks_exclusive_claim
         test_second_instance_keeps_lock_after_first_closes
         test_exclusive_holder_refuses_new_store
         test_failed_init_does_not_leak_the_lock
         test_partial_client_is_torn_down_before_the_lock_is_released
         test_client_creation_failure_releases_lock
         test_factory_locks_beside_the_data_dir
         test_symlinked_data_dir_shares_one_lock
         test_symlinked_persist_dir_agrees_with_the_migration_lock
         test_lock_is_released_even_when_client_close_raises
  GREEN both ways (release-regression guards, not RED tests):
         test_closed_store_releases_the_lock
         test_sqlite_vec_backend_takes_no_chroma_lock
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from opencrab.locking import acquire_file_lock, chroma_lock_dir, release_file_lock

# An exclusive claimant standing in for the offline pack loader or a migration.
# Run in a CHILD process on purpose: flock is scoped to the open file
# description, so an in-process probe would not model a separate owner.
_PROBE = r"""
import sys
from opencrab.locking import acquire_file_lock, release_file_lock
try:
    fh = acquire_file_lock("chroma.lock", sys.argv[1], shared=False, timeout=1.0)
except TimeoutError:
    print("REFUSED")
else:
    release_file_lock(fh)
    print("GRANTED")
"""


def exclusive_probe(lock_dir: str) -> str:
    """Return GRANTED or REFUSED for an exclusive chroma.lock claim."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, lock_dir],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    result = (out.stdout or "").strip()
    assert result in ("GRANTED", "REFUSED"), f"probe failed: {out.stdout}{out.stderr}"
    return result


@pytest.fixture
def chroma_cls():
    pytest.importorskip("chromadb")
    from opencrab.stores.chroma_store import ChromaStore

    return ChromaStore


def make_store(chroma_cls, persist_path: str, name: str = "lockcheck", **kw):
    from _vec_helpers import MockEF

    return chroma_cls(
        host="localhost",
        port=8000,
        collection_name=name,
        local_mode=True,
        local_path=persist_path,
        embedding_function=MockEF(16),
        **kw,
    )


# ---------------------------------------------------------------------------
# Exclusion in both directions
# ---------------------------------------------------------------------------


class TestChromaLockExclusion:
    def test_live_store_blocks_exclusive_claim(self, chroma_cls, tmp_path):
        """정상: 살아 있는 로컬 스토어가 배타 요구자를 막는다.

        수정 전에는 배타 획득이 그냥 성공했다. MCP 밖의 소유자가 잠금을 잡지
        않아 살아 있는 클라이언트가 배타 요구자에게 보이지 않았기 때문이다."""
        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))
        assert store.available
        try:
            assert exclusive_probe(data_dir) == "REFUSED"
        finally:
            store.close()

    def test_closed_store_releases_the_lock(self, chroma_cls, tmp_path):
        """정상: close() 뒤에는 배타 획득이 성공한다.

        수정 전 코드에서도 통과한다(그때는 잠금을 아예 안 잡았으므로). RED
        테스트가 아니라 해제 회귀 방지용이며, 위 테스트와 짝일 때만 의미가 있다."""
        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))
        store.close()
        assert exclusive_probe(data_dir) == "GRANTED"

    def test_second_instance_keeps_lock_after_first_closes(self, chroma_cls, tmp_path):
        """정상: 잠금 소유가 인스턴스별이다 — 프로세스 내 다중 인스턴스.

        A 와 B 를 같은 경로에 연다. 둘 다 동작해야 하고(POSIX 공유 잠금은 서로
        막지 않는다), A 를 닫아도 B 가 자기 핸들을 들고 있으므로 배타 획득은
        여전히 거부되어야 한다. 전역 핸들 하나를 재바인딩하는 구현이나 두 번째
        인스턴스가 잠금을 안 잡는 구현은 이 순서를 통과하지 못한다."""
        data_dir = str(tmp_path)
        persist = os.path.join(data_dir, "chroma")
        a = make_store(chroma_cls, persist, "inst_a")
        b = make_store(chroma_cls, persist, "inst_b")
        try:
            assert a.available and b.available, "동일 경로 다중 인스턴스가 서로를 막았다"
            assert exclusive_probe(data_dir) == "REFUSED"
            a.close()
            assert exclusive_probe(data_dir) == "REFUSED", "B 의 잠금이 A 와 함께 풀렸다"
        finally:
            b.close()
        assert exclusive_probe(data_dir) == "GRANTED"

    def test_exclusive_holder_refuses_new_store(self, chroma_cls, tmp_path):
        """에러: 배타 잠금이 잡힌 동안 스토어 생성이 타임아웃으로 거부된다.

        핵심은 이 타임아웃이 available=False 로 삼켜지지 않는다는 점이다.
        삼켜지면 잠금이 풀린 뒤에도 벡터 계층이 프로세스 수명 동안 불능으로
        남는다. 잠금 해제 뒤 새 인스턴스가 정상 동작하는 것까지 확인한다."""
        from opencrab.stores.chroma_store import ChromaLockTimeoutError

        data_dir = str(tmp_path)
        persist = os.path.join(data_dir, "chroma")
        fh = acquire_file_lock("chroma.lock", data_dir, shared=False, timeout=5.0)
        try:
            with pytest.raises(ChromaLockTimeoutError) as caught:
                make_store(chroma_cls, persist, lock_timeout=0.5)
            assert isinstance(caught.value, TimeoutError)
            assert "chroma.lock" in str(caught.value)
        finally:
            release_file_lock(fh)

        recovered = make_store(chroma_cls, persist, lock_timeout=5.0)
        try:
            assert recovered.available, "잠금이 풀린 뒤에도 벡터 계층이 불능이다"
        finally:
            recovered.close()

    def test_sqlite_vec_backend_takes_no_chroma_lock(self, tmp_path, monkeypatch):
        """비회귀: sqlite-vec 백엔드는 chroma.lock 을 잡지 않는다.

        sqlite-vec 은 SQLite WAL 규율을 쓰므로 chroma 의 flock 계층을 잡으면
        무의미한 보유가 된다."""
        pytest.importorskip("sqlite_vec")
        from opencrab.config import Settings
        from opencrab.stores.factory import make_vector_store

        data_dir = str(tmp_path)
        monkeypatch.setenv("LOCAL_DATA_DIR", data_dir)
        cfg = Settings(
            LOCAL_DATA_DIR=data_dir,
            VECTOR_BACKEND="sqlite-vec",
            EMBEDDING_BACKEND="openai",
        )
        store = make_vector_store(cfg)
        try:
            assert exclusive_probe(data_dir) == "GRANTED"
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()


# ---------------------------------------------------------------------------
# Failure paths must not strand the lock or a live client
# ---------------------------------------------------------------------------


class TestChromaLockFailurePaths:
    def test_client_creation_failure_releases_lock(self, chroma_cls, tmp_path, monkeypatch):
        """에러: PersistentClient 생성 자체가 실패하면 잠금을 푼다.

        부분 클라이언트가 없는 경우다. 실패한 인스턴스를 계속 보존한 채로
        확인해야 한다 — 버리면 참조 계수가 대신 정리해 공허해진다."""
        import chromadb

        monkeypatch.setattr(
            chromadb,
            "PersistentClient",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))
        assert store.available is False
        assert store._lock_fh is None
        assert exclusive_probe(data_dir) == "GRANTED"
        del store

    def test_partial_client_is_torn_down_before_the_lock_is_released(
        self, chroma_cls, tmp_path, monkeypatch
    ):
        """에러: 컬렉션 생성이 실패하면 부분 클라이언트를 헐고 잠금을 푼다.

        배타 획득 성공만 단언하면, 살아 있는 클라이언트를 그대로 둔 채 잠금만
        푸는 구현이 통과한다. 그래서 클라이언트의 close() 호출과 참조 비움을
        함께 단언한다."""
        import chromadb

        closed: list[bool] = []

        class PartialClient:
            def get_or_create_collection(self, *a, **k):
                raise RuntimeError("collection boom")

            def close(self):
                closed.append(True)

        monkeypatch.setattr(chromadb, "PersistentClient", lambda *a, **k: PartialClient())
        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))
        assert store.available is False
        assert closed == [True], "부분 클라이언트가 닫히지 않았다"
        assert store._client is None and store._collection is None
        assert store._lock_fh is None
        assert exclusive_probe(data_dir) == "GRANTED"
        del store

    def test_lock_is_released_even_when_client_close_raises(self, chroma_cls, tmp_path):
        """에러: 클라이언트 종료가 예외를 던져도 잠금은 풀린다.

        해제를 종료 호출 뒤에 그냥 붙인 구현은 실패한다. 해제가 finally 에 있어야
        한다. 예외 자체는 그대로 전파된다."""
        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))

        class Exploding:
            def close(self):
                raise RuntimeError("native close failed")

        store._client = Exploding()
        with pytest.raises(RuntimeError, match="native close failed"):
            store.close()
        assert exclusive_probe(data_dir) == "GRANTED"

    def test_failed_init_does_not_leak_the_lock(self, tmp_path, monkeypatch):
        """엣지: 컨텍스트 초기화가 벡터 뒤에서 실패해도 잠금이 쌓이지 않는다.

        배타 획득과 /proc/self/fd 만 보면 공허하게 통과한다 — 실패한
        _build_context() 의 지역 변수가 풀리는 순간 참조 계수가 정리해버리기
        때문이다. 그래서 팩토리 반환값을 테스트가 직접 붙들고 close() 호출
        여부를 기록해 단언한다. 반복 호출은 1회로는 누수가 드러나지 않기
        때문이다."""
        pytest.importorskip("chromadb")
        import opencrab.mcp.tools as tools_mod
        from opencrab.stores import factory as factory_mod

        data_dir = str(tmp_path)
        monkeypatch.setenv("LOCAL_DATA_DIR", data_dir)
        monkeypatch.setenv("VECTOR_BACKEND", "chroma")
        monkeypatch.setattr(tools_mod, "_context", {})

        kept: list = []
        real_make_vector = factory_mod.make_vector_store

        def spy_vector(cfg):
            store = real_make_vector(cfg)
            kept.append(store)
            store._close_calls = 0
            real_close = store.close

            def counting_close():
                store._close_calls += 1
                return real_close()

            store.close = counting_close
            return store

        monkeypatch.setattr(factory_mod, "make_vector_store", spy_vector)
        monkeypatch.setattr(
            factory_mod,
            "make_doc_store",
            lambda cfg: (_ for _ in ()).throw(RuntimeError("doc store boom")),
        )

        for _ in range(3):
            with pytest.raises(RuntimeError, match="doc store boom"):
                tools_mod._get_context()

        assert len(kept) == 3, "매 시도가 새 벡터 스토어를 만들지 않았다"
        assert [s._close_calls for s in kept] == [1, 1, 1], (
            "초기화 실패 경로가 이미 만든 벡터 스토어를 명시적으로 닫지 않았다"
        )
        # kept still holds every store, so refcounting has released nothing.
        assert exclusive_probe(data_dir) == "GRANTED"


# ---------------------------------------------------------------------------
# One lock file, whoever derives it
# ---------------------------------------------------------------------------


class TestChromaLockPath:
    def test_factory_locks_beside_the_data_dir(self, tmp_path, monkeypatch):
        """규약 고정: 팩토리가 만든 스토어의 잠금 파일이 <data_dir>/chroma.lock.

        외부 오프라인 로더가 배타 잠금을 잡는 자리와 같아야 한다. 어긋나면
        배제가 조용히 성립하지 않는다."""
        pytest.importorskip("chromadb")
        from opencrab.config import Settings
        from opencrab.stores.factory import make_vector_store

        data_dir = str(tmp_path)
        monkeypatch.setenv("LOCAL_DATA_DIR", data_dir)
        cfg = Settings(LOCAL_DATA_DIR=data_dir, VECTOR_BACKEND="chroma")
        store = make_vector_store(cfg)
        try:
            assert store.available
            assert exclusive_probe(data_dir) == "REFUSED"
            assert os.path.exists(os.path.join(data_dir, "chroma.lock"))
        finally:
            store.close()

    def test_symlinked_data_dir_shares_one_lock(self, chroma_cls, tmp_path):
        """엣지: 데이터 디렉터리를 별칭으로 지나도 같은 잠금 파일을 잡는다.

        별칭으로 연 인스턴스만 남긴 뒤 실경로 기준으로 확인해야 한다. 둘 다
        살려 두면 실경로 인스턴스 하나만으로 배타 획득이 막혀, 별칭 인스턴스가
        엉뚱한 잠금 파일을 잡아도 통과한다."""
        real = tmp_path / "realdata"
        real.mkdir()
        alias = tmp_path / "aliasdata"
        alias.symlink_to(real)

        via_real = make_store(chroma_cls, str(real / "chroma"), "via_real")
        via_alias = make_store(chroma_cls, str(alias / "chroma"), "via_alias")
        try:
            via_real.close()
            assert exclusive_probe(str(real)) == "REFUSED", (
                "별칭으로 연 인스턴스가 실경로와 다른 잠금 파일을 잡았다"
            )
        finally:
            via_alias.close()
        assert exclusive_probe(str(real)) == "GRANTED"

    def test_symlinked_persist_dir_agrees_with_the_migration_lock(self, chroma_cls, tmp_path):
        """엣지: persist 디렉터리가 심볼릭 링크여도 마이그레이션과 같은 잠금이다.

        마이그레이션과 외부 로더는 <local_data_dir>/chroma.lock 을 잡는다.
        persist 경로 자체를 realpath 로 풀어 파생하면 링크 대상 옆에 잠금이
        생겨 배제가 갈라진다. 별칭 통합만 확인하고 이 대조를 빠뜨리면 깨진
        채로 통과한다."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        persist = data_dir / "chroma"
        persist.symlink_to(elsewhere)

        assert chroma_lock_dir(str(persist)) == str(data_dir)

        store = make_store(chroma_cls, str(persist))
        try:
            assert exclusive_probe(str(data_dir)) == "REFUSED", (
                "스토어가 마이그레이션이 쓰는 잠금 파일과 다른 곳을 잡았다"
            )
        finally:
            store.close()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCliLockTimeoutShape:
    def test_make_stores_reports_a_lock_timeout_as_an_operator_error(
        self, tmp_path, monkeypatch
    ):
        """정상: CLI 가 traceback 대신 운영자용 메시지와 종료 코드 1 을 낸다.

        벡터 스토어 생성은 각 명령의 except RuntimeError 블록 밖에 있으므로
        변환은 _make_stores() 안에서 일어나야 한다."""
        pytest.importorskip("chromadb")
        from opencrab import cli as cli_mod
        from opencrab.config import Settings

        data_dir = str(tmp_path)
        monkeypatch.setenv("LOCAL_DATA_DIR", data_dir)
        monkeypatch.setenv("CHROMA_LOCK_TIMEOUT", "0.5")
        cfg = Settings(
            LOCAL_DATA_DIR=data_dir, VECTOR_BACKEND="chroma", CHROMA_LOCK_TIMEOUT=0.5
        )

        fh = acquire_file_lock("chroma.lock", data_dir, shared=False, timeout=5.0)
        try:
            with pytest.raises(SystemExit) as caught:
                cli_mod._make_stores(cfg, vector=True)
            assert caught.value.code == 1
        finally:
            release_file_lock(fh)

    def test_make_stores_closes_earlier_stores_when_a_later_one_fails(
        self, tmp_path, monkeypatch
    ):
        """엣지: 뒤쪽 팩토리가 실패하면 앞서 연 스토어를 닫는다.

        실패 원자성이 없으면 벡터 생성이 실패할 때 앞서 연 graph 가 열린 채
        남는다."""
        from opencrab import cli as cli_mod
        from opencrab.config import Settings
        from opencrab.stores import factory as factory_mod

        closed: list[str] = []

        class FakeGraph:
            def close(self):
                closed.append("graph")

        monkeypatch.setattr(factory_mod, "make_graph_store", lambda cfg: FakeGraph())
        monkeypatch.setattr(
            factory_mod,
            "make_vector_store",
            lambda cfg: (_ for _ in ()).throw(RuntimeError("vector boom")),
        )
        cfg = Settings(LOCAL_DATA_DIR=str(tmp_path))
        with pytest.raises(RuntimeError, match="vector boom"):
            cli_mod._make_stores(cfg, graph=True, vector=True)
        assert closed == ["graph"], "앞서 연 스토어가 닫히지 않았다"


# ---------------------------------------------------------------------------
# Lock ordering
# ---------------------------------------------------------------------------


_WRITE_PROBE = r"""
import sys
from opencrab.locking import acquire_file_lock, release_file_lock
try:
    fh = acquire_file_lock("write.lock", sys.argv[1], shared=False, timeout=1.0)
except TimeoutError:
    print("REFUSED")
else:
    release_file_lock(fh)
    print("GRANTED")
"""


def write_lock_probe(lock_dir: str) -> str:
    """Return GRANTED or REFUSED for an exclusive write.lock claim."""
    out = subprocess.run(
        [sys.executable, "-c", _WRITE_PROBE, lock_dir],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    result = (out.stdout or "").strip()
    assert result in ("GRANTED", "REFUSED"), f"probe failed: {out.stdout}{out.stderr}"
    return result


class TestLockOrderInversionIsBounded:
    """The global order is chroma.lock before write.lock (#140).

    dispatch_tool inverts it on exactly one path: the first write tool call
    builds the MCP context inside write.lock, and building it takes
    chroma.lock. Removing that inversion structurally needs a context-need axis
    the registry does not have, so it is BOUNDED instead: both sides use a
    finite wait, so the inversion resolves with a clear error rather than
    hanging. This pins that bound.
    """

    def test_inverted_order_times_out_and_frees_the_peer(self, chroma_cls, tmp_path):
        import multiprocessing
        import time

        from opencrab.locking import file_lock
        from opencrab.stores.chroma_store import ChromaLockTimeoutError

        data_dir = str(tmp_path)
        persist = os.path.join(data_dir, "chroma")

        # A migration standing in as the exclusive chroma.lock holder, in a
        # separate process so its flock is genuinely another owner.
        ready = multiprocessing.Event()
        stop = multiprocessing.Event()

        def hold_exclusive(data_dir: str, ready, stop) -> None:
            from opencrab.locking import acquire_file_lock, release_file_lock

            fh = acquire_file_lock("chroma.lock", data_dir, shared=False)
            ready.set()
            stop.wait(60)
            release_file_lock(fh)

        holder = multiprocessing.Process(
            target=hold_exclusive, args=(data_dir, ready, stop)
        )
        holder.start()
        try:
            assert ready.wait(30), "배타 보유자가 기동하지 않았다"

            acquired_calls: list[str] = []
            real_acquire = chroma_cls._acquire_local_lock

            def spy(self):
                # Non-vacuity: record that we really are inside the inverted
                # window -- this process holds write.lock at this moment.
                acquired_calls.append(write_lock_probe(data_dir))
                return real_acquire(self)

            chroma_cls._acquire_local_lock = spy
            try:
                started = time.monotonic()
                # The inversion: write.lock first, then a chroma client.
                with file_lock("write.lock", data_dir):
                    with pytest.raises(ChromaLockTimeoutError):
                        make_store(chroma_cls, persist, lock_timeout=1.0)
                elapsed = time.monotonic() - started
            finally:
                chroma_cls._acquire_local_lock = real_acquire

            # Non-vacuity: the acquisition really ran, and it ran while this
            # process held write.lock. Without these the test would pass even
            # if nothing had been attempted.
            assert acquired_calls == ["REFUSED"], (
                f"역전 상황에 실제로 들어가지 않았다: {acquired_calls}"
            )
            assert holder.is_alive(), "배타 보유자가 도중에 죽었다"
            # Bounded, not hanging.
            assert elapsed < 30, f"유한 시간에 끝나지 않았다: {elapsed}s"
            # And the peer can now proceed: write.lock was given back.
            assert write_lock_probe(data_dir) == "GRANTED"
        finally:
            stop.set()
            holder.join(30)
            if holder.is_alive():
                holder.terminate()
                holder.join(10)

    def test_migration_bounds_its_write_lock_wait(self, tmp_path, monkeypatch):
        """마이그레이션의 write.lock 획득이 무한 대기가 아니다.

        위 테스트는 공유 쪽 상한만 본다. 배타 쪽 상한은 여기서 고정한다.
        상한이 없으면 상대가 풀릴 때까지 무기한 매달린다.

        소스를 파싱하지 않고 실제로 호출한다. timeout 인자의 존재만 확인하면
        ``timeout=None`` 이 통과하는데 그것이 바로 무한 대기다."""
        import multiprocessing
        import sys as _sys
        import time

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts_dir = os.path.join(repo, "scripts")
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)
        migrate_to_local = pytest.importorskip("migrate_to_local")

        data_dir = str(tmp_path)
        # Shrink the derived bound (chroma_lock_timeout + 60) to something a
        # test can wait for, without hard-coding the derivation here.
        monkeypatch.setenv("CHROMA_LOCK_TIMEOUT", "-58")
        from opencrab.config import get_settings

        get_settings.cache_clear()
        try:
            ready = multiprocessing.Event()
            stop = multiprocessing.Event()

            def hold_write(data_dir: str, ready, stop) -> None:
                from opencrab.locking import acquire_file_lock, release_file_lock

                fh = acquire_file_lock("write.lock", data_dir, shared=False)
                ready.set()
                stop.wait(60)
                release_file_lock(fh)

            holder = multiprocessing.Process(
                target=hold_write, args=(data_dir, ready, stop)
            )
            holder.start()
            try:
                assert ready.wait(30), "write.lock 보유자가 기동하지 않았다"
                started = time.monotonic()
                with pytest.raises(TimeoutError):
                    migrate_to_local._migrate_vectors_locked(
                        None, data_dir, "irrelevant", 1, logging.getLogger(__name__)
                    )
                elapsed = time.monotonic() - started
                assert elapsed < 30, f"상한이 걸리지 않았다: {elapsed}s"
                assert holder.is_alive(), "보유자가 도중에 죽어 대기가 끝난 것이다"
            finally:
                stop.set()
                holder.join(30)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(10)
        finally:
            get_settings.cache_clear()


class TestRestContextFailureAtomicity:
    def test_rest_build_context_closes_earlier_stores(self, tmp_path, monkeypatch):
        """엣지: REST 컨텍스트 빌더도 뒤쪽 팩토리 실패 시 앞을 닫는다.

        MCP 와 CLI 는 각자 고정돼 있는데 REST 만 없었다."""
        pytest.importorskip("fastapi")
        import apps.api.main as api_main

        closed: list[str] = []

        class Fake:
            def __init__(self, name: str) -> None:
                self._name = name

            def close(self) -> None:
                closed.append(self._name)

            def ensure_constraints(self) -> None:
                pass

        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(api_main, "make_graph_store", lambda s: Fake("graph"))
        monkeypatch.setattr(api_main, "make_vector_store", lambda s: Fake("vector"))
        monkeypatch.setattr(
            api_main,
            "make_doc_store",
            lambda s: (_ for _ in ()).throw(RuntimeError("doc boom")),
        )
        with pytest.raises(RuntimeError, match="doc boom"):
            api_main._build_context()
        assert closed == ["vector", "graph"], f"정리 순서가 다르다: {closed}"


class TestTargetMigrationTearsDownBeforeRelease:
    """migrate_chroma_to_sqlite_vec must not leave a client outliving the lock.

    Two ways it could, and the test has to rule out both. Reference counting is
    not enough because chromadb's client has no ``__del__``, so an explicit
    ``close()`` is required even on the plain early returns. And on the
    exception path the traceback keeps the function's frame alive, so the frame
    must clear its own names -- ``col`` included, because a Collection holds the
    client on ``self._client``.

    The test never keeps a strong reference to a fake client: doing so would
    stop ``weakref.finalize`` from ever running and fail a correct
    implementation. Only ids, counts and weakrefs are recorded.
    """

    @pytest.mark.parametrize("close_mode", ["absent", "present", "raises"])
    def test_client_is_closed_and_dropped_before_return(
        self, close_mode, tmp_path, monkeypatch
    ):
        import gc
        import sys as _sys
        import weakref

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts_dir = os.path.join(repo, "scripts")
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)
        mod = pytest.importorskip("migrate_chroma_to_sqlite_vec")
        chromadb = pytest.importorskip("chromadb")

        events: list[str] = []
        made = {"clients": 0, "collections": 0}
        client_ids: list[int] = []
        client_refs: list = []

        class FakeCollection:
            """Holds the client, exactly as chromadb's Collection does."""

            def __init__(self, client):
                self._client = client
                made["collections"] += 1

            def count(self):
                return 3

        class FakeClientBase:
            def get_collection(self, name):
                return FakeCollection(self)

        class FakeClientAbsent(FakeClientBase):
            pass

        class FakeClientPresent(FakeClientBase):
            def close(self):
                events.append("client_closed")

        def _raising_close():
            """Plain function, NOT a bound method.

            A bound ``close`` would put ``self`` in the raising frame, and
            anything that retains that exception (pytest's log capture, for
            one) then keeps the client alive no matter how well the code under
            test clears its own references. The defect being tested is about
            the CALLER's frame, so the injected failure must not add an anchor
            of its own."""
            events.append("client_closed")
            raise RuntimeError("native close failed")

        class FakeClientRaises(FakeClientBase):
            def __init__(self):
                self.close = _raising_close

        cls = {
            "absent": FakeClientAbsent,
            "present": FakeClientPresent,
            "raises": FakeClientRaises,
        }[close_mode]

        def fake_persistent_client(*a, **k):
            client = cls()
            made["clients"] += 1
            client_ids.append(id(client))
            client_refs.append(weakref.ref(client))
            weakref.finalize(client, events.append, "client_finalized")
            return client

        monkeypatch.setattr(chromadb, "PersistentClient", fake_persistent_client)

        # Fail AFTER the collection is in hand, at a point whose frame does not
        # reference the collection or the client. A failure raised inside a
        # collection method would pin the client through that frame's `self`,
        # so even a correct implementation would look broken.
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        def boom(self):
            raise RuntimeError("reset boom")

        monkeypatch.setattr(SqliteVecStore, "reset_collection", boom, raising=True)

        class Args:
            dry_run = False
            force = True
            batch = 10

        from opencrab.config import Settings

        settings = Settings(LOCAL_DATA_DIR=str(tmp_path), EMBED_DIM=8)
        db_path = str(tmp_path / "vectors.db")

        caught = None
        try:
            mod._copy_chroma_to_vec0(
                Args(), settings, "irrelevant", db_path, str(tmp_path / "chroma")
            )
        except Exception as exc:  # noqa: BLE001
            caught = exc

        # Still holding the traceback: that is the condition this defect needs.
        assert caught is not None, "예외 경로에 들어가지 않았다"
        assert caught.__traceback__ is not None

        # Non-vacuity: the client and the collection really were created, so a
        # dead weakref means teardown, not absence.
        assert made["clients"] == 1, f"가짜 클라이언트가 만들어지지 않았다: {made}"
        assert made["collections"] == 1, f"가짜 컬렉션이 만들어지지 않았다: {made}"

        if close_mode in ("present", "raises"):
            assert "client_closed" in events, (
                "명시적 close() 가 호출되지 않았다 — 참조 계수만으로는 chroma 공유 "
                "System 자원이 반환되지 않는다"
            )

        gc.collect()

        # No frame in the traceback may still name the client.
        tb = caught.__traceback__
        while tb is not None:
            for name, value in tb.tb_frame.f_locals.items():
                assert id(value) not in client_ids, (
                    f"traceback 프레임이 클라이언트를 붙들고 있다: {name}"
                )
            tb = tb.tb_next

        assert client_refs[0]() is None, (
            "예외 traceback 이 프레임을 붙든 채 클라이언트가 살아 있다"
        )


def _raising_close_fn():
    """Module-level so the raising frame carries no ``self`` (see below)."""
    raise RuntimeError("native close failed")


def _raising_get_collection(*_a, **_k):
    """Same reason as above: no ``self`` in the frame that raises."""
    raise RuntimeError("collection boom")


class TestNoFramePinsTheClientAtReleaseTime:
    """No cleanup site may leave the chroma client anchored in its own frame.

    Writing ``close = getattr(client, "close", None)`` puts a BOUND METHOD in
    the frame, and a bound method keeps its object alive. The frame survives
    exactly when it matters: a traceback holds it while an error propagates,
    and it is still live at the moment the lock is released. The client then
    outlives the lock the whole design exists to pair it with (#140).

    None of these tests keeps a strong reference to a fake client -- that would
    stop ``weakref.finalize`` from running and fail a correct implementation.
    Only ids, counts and weakrefs are recorded. For the same reason every
    injected failure is a plain function, never a bound method: a raising bound
    method anchors the client through its own frame, which would fail a correct
    implementation for a reason that has nothing to do with the code under test.
    """

    def test_chroma_store_close_does_not_pin_the_client(self, chroma_cls, tmp_path):
        """정상: close() 뒤 잠금을 푸는 시점에 클라이언트가 이미 사라져 있다.

        여기서 가짜의 close 는 예외를 던지지 않는 **바인드 메서드**여야 한다.
        평범한 함수로 두면 인라인 `close = getattr(...)` 이 남겨도 아무것도
        붙들지 않아 결함이 관측되지 않는다. 그리고 관측 시점을 traceback 이
        아니라 해제 순간으로 잡아야 한다 — close 가 던지지 않으므로 붙들
        traceback 이 없고, 대신 그 순간 close() 프레임이 아직 살아 있다.

        예외를 던지는 경우의 잠금 해제는
        test_lock_is_released_even_when_client_close_raises 가 따로 본다."""
        import gc
        import weakref

        data_dir = str(tmp_path)
        store = make_store(chroma_cls, os.path.join(data_dir, "chroma"))

        class Quiet:
            def close(self):  # bound method on purpose
                pass

        fake = Quiet()
        ref = weakref.ref(fake)
        store._client = fake
        del fake

        observed: list[bool] = []
        real_release = type(store)._release_local_lock

        def spy_release(self):
            # close()'s own frame is still alive here. An inline
            # `close = getattr(client, "close", None)` is a bound method that
            # keeps the client alive right at this moment.
            gc.collect()
            observed.append(ref() is None)
            return real_release(self)

        store._release_local_lock = spy_release.__get__(store, type(store))
        store.close()

        assert observed == [True], (
            "잠금을 푸는 시점에 close() 프레임이 여전히 클라이언트를 붙들고 있다"
        )

    def test_connect_failure_does_not_pin_the_client_at_release(
        self, chroma_cls, tmp_path, monkeypatch
    ):
        """엣지: 연결 실패 정리도 잠금 해제 시점에 클라이언트를 붙들지 않는다.

        _connect 는 예외를 안에서 삼키므로 밖으로 나가는 traceback 이 없다.
        그래서 관측 시점을 해제 순간으로 옮긴다 — 그 순간 _connect 프레임은
        아직 살아 있으므로, 인라인 바인드 메서드가 있으면 여기서 잡힌다."""
        import gc
        import weakref

        import chromadb

        made = {"clients": 0}
        refs: list = []
        observed: list[bool] = []

        class FakeClient:
            def __init__(self):
                # Both plain functions, not bound methods: the raising frame
                # must not carry `self`, or the test's own injection anchors
                # the client and a correct implementation looks broken.
                self.close = lambda: None
                self.get_or_create_collection = _raising_get_collection

        def fake_persistent_client(*a, **k):
            client = FakeClient()
            made["clients"] += 1
            refs.append(weakref.ref(client))
            return client

        monkeypatch.setattr(chromadb, "PersistentClient", fake_persistent_client)

        real_release = chroma_cls._release_local_lock

        def spy_release(self):
            # The _connect frame is still alive right here. If it still names
            # the client, this is where an inline bound method shows up.
            gc.collect()
            observed.append(refs[0]() is None)
            return real_release(self)

        monkeypatch.setattr(chroma_cls, "_release_local_lock", spy_release)

        store = make_store(chroma_cls, os.path.join(str(tmp_path), "chroma"))
        assert store.available is False
        assert made["clients"] == 1, "가짜 클라이언트가 만들어지지 않았다"
        assert observed == [True], (
            "잠금을 푸는 시점에 _connect 프레임이 여전히 클라이언트를 붙들고 있다"
        )

    def test_migrate_to_local_does_not_pin_the_client(self, tmp_path, monkeypatch):
        """에러: 벡터 마이그레이션 실패도 클라이언트를 프레임에 남기지 않는다."""
        import gc
        import logging as _logging
        import sys as _sys
        import weakref

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts_dir = os.path.join(repo, "scripts")
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)
        mtl = pytest.importorskip("migrate_to_local")
        chromadb = pytest.importorskip("chromadb")

        made = {"clients": 0}
        client_ids: list[int] = []
        refs: list = []

        class FakeClient:
            # Bound method on purpose: an inline
            # `close = getattr(local_chroma, "close", None)` must have
            # something to pin, or the defect is unobservable.
            def close(self):
                pass

        def fake_persistent_client(*a, **k):
            client = FakeClient()
            made["clients"] += 1
            client_ids.append(id(client))
            refs.append(weakref.ref(client))
            return client

        monkeypatch.setattr(chromadb, "PersistentClient", fake_persistent_client)

        def boom(http_client, local_chroma, *a, **k):
            # Drop this stand-in's own references BEFORE raising: otherwise its
            # frame anchors the client and the check below means nothing.
            del http_client, local_chroma
            raise RuntimeError("migrate boom")

        monkeypatch.setattr(mtl, "migrate_vectors", boom)

        caught = None
        try:
            mtl._migrate_vectors_locked(
                None, str(tmp_path), "coll", 10, _logging.getLogger(__name__)
            )
        except RuntimeError as exc:
            caught = exc

        assert caught is not None and caught.__traceback__ is not None
        assert made["clients"] == 1, "가짜 클라이언트가 만들어지지 않았다"

        gc.collect()
        tb = caught.__traceback__
        while tb is not None:
            for name, value in tb.tb_frame.f_locals.items():
                assert id(value) not in client_ids, (
                    f"traceback 프레임 {tb.tb_frame.f_code.co_name} 이 "
                    f"클라이언트를 {name} 으로 붙들고 있다"
                )
            tb = tb.tb_next
        assert refs[0]() is None, "잠금이 풀린 뒤에도 클라이언트가 살아 있다"
