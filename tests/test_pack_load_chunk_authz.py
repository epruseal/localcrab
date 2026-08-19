"""#205 인가 게이트: `load_chunks`/`load_chunks_incremental` 이 팩 소유권을 지운다.

`opencrab/pack/load.py` 의 두 청크 로더는 진입에서 `write_gate.authorize(sql,
principal, pack_name)` 를 부른다(그 파일의 모듈 docstring과 각 함수 docstring이
정본). 이 파일은 그 인가가 실제로 **막는지**를 행동으로 건다 — AST 계약이나 호출
여부만으로는 "거부됐을 때 아무것도 안 써졌는가"까지 보증하지 못한다.

거는 것 (요약, 카운트 없이 상태로): 소유자 아닌 principal 의 비공개/공개-읽기 팩
거부(각각 `PackNotFoundError`/`PackForbiddenError`) + 거부 후 `doc_sources` 전량
불변 + 벡터 `upsert_texts` 미호출, 미바인딩 principal 의 `RuntimeError`, 등록부
미가용 시 fail-closed, 존재하지 않는 팩의 `PackNotFoundError`, 소유자 정상 경로
회귀, 그리고 `sql` 파라미터가 키워드 전용 필수(기본값 없음)라는 시그니처 자체.

재현:
    PYTHONPATH=<repo-root> python3 -m pytest tests/test_pack_load_chunk_authz.py -q
청크 축 전체(로더 행동 + 인가)는
    PYTHONPATH=<repo-root> python3 -m pytest tests/test_pack_load.py tests/test_pack_load_chunk_authz.py \
        tests/test_write_sink_inventory.py -q
"""

from __future__ import annotations

import inspect
import json
import pathlib
import tempfile

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import (
    PackForbiddenError,
    PackNotFoundError,
    create_pack,
    set_visibility,
)
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

# ---------------------------------------------------------------------------
# 더블·헬퍼
# ---------------------------------------------------------------------------


class _SpyVec:
    """`upsert_texts` 호출 횟수만 기록한다 — 거부 후 0회임을 확인하는 용도.

    `available=True` 다: 거부는 `vec.available` 단락보다 **앞**에서 나야 하므로
    (load.py 의 `authorize(...)` 가 그 단락보다 먼저 온다는 주석 참고), 벡터
    미가용으로 스킵시켜 인가 검사 자체를 우회하면 안 된다.
    """

    available = True

    def __init__(self) -> None:
        self.upsert_calls = 0
        self.ids: list[str] = []

    def upsert_texts(self, texts, metadatas=None, ids=None):
        self.upsert_calls += 1
        self.ids.extend(ids or [])

    def delete(self, ids):  # noqa: ARG002
        return None


class _UnavailableRegistrySql:
    """`sql.available` 이 거짓 -- `write_gate.authorize` 의 fail-closed 분기용.

    `assert_writable` 까지 갈 필요가 없다(그 앞에서 거부돼야 한다) 이므로
    `available` 속성 하나만 있으면 충분하다 -- 실제로 그 이상은 호출되지 않는다는
    것이 이 더블로 거는 것이다(실 `SQLStore` 였다면 `assert_writable` 이 돌아
    거부 사유가 `PackNotFoundError`/`PackForbiddenError` 로 바뀌어 이 분기 자체가
    가려진다).
    """

    available = False


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return path


def _chunk_row(chunk_id: str, text: str = "본문", **metadata) -> dict:
    return {"id": chunk_id, "text": text, "document_id": chunk_id, "metadata": metadata}


def _chunks_file(tmp_path: pathlib.Path, name: str, rows: list[dict]) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="chunkauthz_", dir=str(tmp_path)))
    return _write_jsonl(d / name, rows)


def _doc_sources_snapshot(docs) -> list[tuple]:
    rows = docs._conn.execute(
        "SELECT source_id, text, metadata FROM doc_sources").fetchall()
    return sorted(tuple(r) for r in rows)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """진짜 SQLite 2스토어(등록부 `SQLStore` + doc `LocalSQLDocStore`) + `LOCAL_DATA_DIR`
    실재. `load_chunks`/`load_chunks_incremental` 은 graph 스토어를 쓰지 않으므로
    (시그니처에 없다) 여기서는 만들지 않는다."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    yield docs, sql
    docs.close()


@pytest.fixture
def owner_a(store):
    """실 등록 사용자 A + A 가 소유한 비공개 팩 "authz-pack" (기본 visibility)."""
    docs, sql = store
    user_id = create_user(sql, "owner-a", is_local=True)
    pack_id = create_pack(sql, user_id, "authz-pack")
    assert pack_id == "authz-pack", "collided with a pre-existing pack of the same id"
    return Principal(user_id=user_id, is_local=True, disabled=False)


@pytest.fixture
def user_b(store):
    """실 등록 사용자 B -- "authz-pack" 을 소유하지 않는다.

    `is_local=False`: `idx_users_single_local` 이 로컬 사용자를 스토어당 1인으로
    강제한다(`opencrab.auth.create_user` 참고) -- A 가 이미 로컬 사용자를 채웠으니
    B 는 비로컬로 등록해야 두 사용자 픽스처가 같은 `store` 에서 공존한다."""
    docs, sql = store
    user_id = create_user(sql, "user-b", is_local=False)
    return Principal(user_id=user_id, is_local=False, disabled=False)


# ---------------------------------------------------------------------------
# 1/2 — 비소유자 거부 + 팩 불변 (load_chunks / load_chunks_incremental)
# ---------------------------------------------------------------------------


class TestNonOwnerRejectionLoadChunks:
    def test_private_pack_raises_pack_not_found(self, tmp_path, store, owner_a, user_b):
        docs, sql = store
        with principal_scope(owner_a):
            baseline = _chunks_file(tmp_path, "baseline.jsonl", [_chunk_row("c1", "원본")])
            ok, err = pack_load.load_chunks("authz-pack", baseline, _SpyVec(), docs, sql=sql)
        assert (ok, err) == (1, 0), "베이스라인 자체가 실패했다 — 전제가 깨졌다"
        baseline_rows = _doc_sources_snapshot(docs)
        assert len(baseline_rows) > 0, "베이스라인 행이 0건이면 '불변' 단언이 공허해진다"

        intruder = _chunks_file(tmp_path, "intruder.jsonl", [_chunk_row("c2", "남의 글")])
        vec = _SpyVec()
        with principal_scope(user_b):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks("authz-pack", intruder, vec, docs, sql=sql)

        assert _doc_sources_snapshot(docs) == baseline_rows, (
            "비공개 팩 거부 후 doc_sources 가 바뀌었다 — 거부가 부분 쓰기를 허용했다")
        assert vec.upsert_calls == 0, "거부됐는데 벡터에 썼다"

    def test_public_read_pack_raises_pack_forbidden(self, tmp_path, store, owner_a, user_b):
        docs, sql = store
        with principal_scope(owner_a):
            baseline = _chunks_file(tmp_path, "baseline.jsonl", [_chunk_row("c1", "원본")])
            ok, err = pack_load.load_chunks("authz-pack", baseline, _SpyVec(), docs, sql=sql)
        assert (ok, err) == (1, 0)
        set_visibility(sql, owner_a, "authz-pack", "public-read")
        baseline_rows = _doc_sources_snapshot(docs)
        assert len(baseline_rows) > 0

        intruder = _chunks_file(tmp_path, "intruder.jsonl", [_chunk_row("c2", "남의 글")])
        vec = _SpyVec()
        with principal_scope(user_b):
            with pytest.raises(PackForbiddenError):
                pack_load.load_chunks("authz-pack", intruder, vec, docs, sql=sql)

        assert _doc_sources_snapshot(docs) == baseline_rows, (
            "공개 팩 거부 후 doc_sources 가 바뀌었다 — 거부가 부분 쓰기를 허용했다")
        assert vec.upsert_calls == 0, "거부됐는데 벡터에 썼다"


class TestNonOwnerRejectionLoadChunksIncremental:
    def test_private_pack_raises_pack_not_found(self, tmp_path, store, owner_a, user_b):
        docs, sql = store
        with principal_scope(owner_a):
            baseline = _chunks_file(tmp_path, "baseline.jsonl", [_chunk_row("c1", "원본")])
            ok, err = pack_load.load_chunks("authz-pack", baseline, _SpyVec(), docs, sql=sql)
        assert (ok, err) == (1, 0)
        baseline_rows = _doc_sources_snapshot(docs)
        assert len(baseline_rows) > 0

        intruder = _chunks_file(tmp_path, "intruder.jsonl", [_chunk_row("c2", "남의 글")])
        vec = _SpyVec()
        with principal_scope(user_b):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks_incremental(
                    "authz-pack", intruder, vec, docs, {}, sql=sql)

        assert _doc_sources_snapshot(docs) == baseline_rows
        assert vec.upsert_calls == 0

    def test_public_read_pack_raises_pack_forbidden(self, tmp_path, store, owner_a, user_b):
        docs, sql = store
        with principal_scope(owner_a):
            baseline = _chunks_file(tmp_path, "baseline.jsonl", [_chunk_row("c1", "원본")])
            ok, err = pack_load.load_chunks("authz-pack", baseline, _SpyVec(), docs, sql=sql)
        assert (ok, err) == (1, 0)
        set_visibility(sql, owner_a, "authz-pack", "public-read")
        baseline_rows = _doc_sources_snapshot(docs)
        assert len(baseline_rows) > 0

        intruder = _chunks_file(tmp_path, "intruder.jsonl", [_chunk_row("c2", "남의 글")])
        vec = _SpyVec()
        with principal_scope(user_b):
            with pytest.raises(PackForbiddenError):
                pack_load.load_chunks_incremental(
                    "authz-pack", intruder, vec, docs, {}, sql=sql)

        assert _doc_sources_snapshot(docs) == baseline_rows
        assert vec.upsert_calls == 0


class _NoVec:
    """벡터 미가용 배포 형태. `load_chunks` 는 이 경우 적재를 통째로 skip 한다."""

    available = False

    def __init__(self) -> None:
        self.upsert_calls = 0

    def upsert_texts(self, texts, metadatas=None, ids=None):  # pragma: no cover
        self.upsert_calls += 1


class TestRejectionDoesNotDependOnDeploymentShape:
    """벡터가 없는 배포에서도 비소유자는 거부된다.

    `load_chunks` 는 `vec.available` 이 거짓이면 `(0, 0)` 으로 조용히 빠져나간다.
    인가를 그 단락 **뒤**로 옮기면 이 배포 형태에서만 비소유자 호출이 거부 대신
    "0건 성공" 으로 보이고, 위의 거부 테스트들은 전부 `available=True` 인 더블을
    쓰므로 그 변이를 하나도 잡지 못한다. 순서를 잡는 것은 이 테스트뿐이다.
    """

    def test_non_owner_is_refused_even_when_the_vector_store_is_unavailable(
            self, tmp_path, store, owner_a, user_b):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1", "남의 글")])
        with principal_scope(user_b):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks("authz-pack", f, _NoVec(), docs, sql=sql)
        assert _doc_sources_snapshot(docs) == []


# ---------------------------------------------------------------------------
# 3 — 미바인딩 principal
# ---------------------------------------------------------------------------


class TestUnboundPrincipal:
    def test_load_chunks_raises_runtime_error_mentioning_principal_scope(
            self, tmp_path, store):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        with pytest.raises(RuntimeError, match="principal_scope"):
            pack_load.load_chunks("authz-pack", f, vec, docs, sql=sql)
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []

    def test_load_chunks_incremental_raises_runtime_error_mentioning_principal_scope(
            self, tmp_path, store):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        with pytest.raises(RuntimeError, match="principal_scope"):
            pack_load.load_chunks_incremental("authz-pack", f, vec, docs, {}, sql=sql)
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []


# ---------------------------------------------------------------------------
# 4 — 등록부 미가용, fail-closed
# ---------------------------------------------------------------------------


class TestRegistryUnavailableFailsClosed:
    """`sql.available=False` 면 `authorize` 가 `assert_writable` 에 닿기도 전에
    거부한다(`write_gate.authorize` 참고). 반드시 principal 을 먼저 묶는다 --
    안 그러면 `_require_bound_principal` 의 무관한 `RuntimeError` 로도 이 테스트가
    통과해, `authorize` 호출 자체를 지우는 변이를 못 잡는다. 그래서 메시지에
    "ownership cannot be verified" 가 있는지까지 확인한다."""

    def test_load_chunks_raises_runtime_error_ownership_cannot_be_verified(
            self, tmp_path, store):
        docs, _sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        principal = Principal(user_id="whoever", is_local=True, disabled=False)
        with principal_scope(principal):
            with pytest.raises(RuntimeError, match="ownership cannot be verified"):
                pack_load.load_chunks(
                    "authz-pack", f, vec, docs, sql=_UnavailableRegistrySql())
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []

    def test_load_chunks_incremental_raises_runtime_error_ownership_cannot_be_verified(
            self, tmp_path, store):
        docs, _sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        principal = Principal(user_id="whoever", is_local=True, disabled=False)
        with principal_scope(principal):
            with pytest.raises(RuntimeError, match="ownership cannot be verified"):
                pack_load.load_chunks_incremental(
                    "authz-pack", f, vec, docs, {}, sql=_UnavailableRegistrySql())
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []


# ---------------------------------------------------------------------------
# 5 — 존재하지 않는 팩
# ---------------------------------------------------------------------------


class TestNonexistentPack:
    def test_load_chunks_raises_pack_not_found(self, tmp_path, store, owner_a):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        with principal_scope(owner_a):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks("no-such-pack", f, vec, docs, sql=sql)
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []

    def test_load_chunks_incremental_raises_pack_not_found(self, tmp_path, store, owner_a):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1")])
        vec = _SpyVec()
        with principal_scope(owner_a):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks_incremental("no-such-pack", f, vec, docs, {}, sql=sql)
        assert vec.upsert_calls == 0
        assert _doc_sources_snapshot(docs) == []


# ---------------------------------------------------------------------------
# 6 — 소유자 정상 경로 회귀
# ---------------------------------------------------------------------------


class TestOwnerHappyPathRegression:
    def test_owner_load_chunks_succeeds_and_rows_land(self, tmp_path, store, owner_a):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1", "본문1"), _chunk_row("c2", "본문2")])
        vec = _SpyVec()
        with principal_scope(owner_a):
            ok, err = pack_load.load_chunks("authz-pack", f, vec, docs, sql=sql)
        assert (ok, err) == (2, 0)
        assert sorted(vec.ids) == ["c1", "c2"]
        rows = _doc_sources_snapshot(docs)
        assert {r[0] for r in rows} == {"c1", "c2"}

    def test_owner_load_chunks_incremental_succeeds_and_converges_to_same(
            self, tmp_path, store, owner_a):
        docs, sql = store
        f = _chunks_file(tmp_path, "c.jsonl", [_chunk_row("c1", "본문1")])
        vec = _SpyVec()
        with principal_scope(owner_a):
            ok, err = pack_load.load_chunks("authz-pack", f, vec, docs, sql=sql)
            assert (ok, err) == (1, 0)
            live_chunks = {
                sid: (txt, json.loads(md))
                for sid, txt, md in docs._conn.execute(
                    "SELECT source_id, text, metadata FROM doc_sources")
            }
            stats = pack_load.load_chunks_incremental(
                "authz-pack", f, vec, docs, live_chunks, sql=sql)
        c_new, c_txt, c_meta, c_same, err, _bypack = stats
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 1, 0), (
            "동일 청크 재적재가 same 으로 수렴하지 않았다")


# ---------------------------------------------------------------------------
# 7 — 시그니처 고정: sql 은 키워드 전용 필수
# ---------------------------------------------------------------------------


class TestSqlParamIsKeywordOnlyRequired:
    """`sql` 에 기본값을 주면 fail-open 이 된다 -- "주어지면 인가, 안 주어지면
    스킵" 이 되는 순간 #204(신원 축 기본값 fail-open)와 같은 모양으로 되돌아간다.
    이 게이트는 그 회귀가 코드 리뷰를 몰래 통과하지 못하게 시그니처 자체를 고정한다.
    """

    def test_load_chunks_sql_is_keyword_only_with_no_default(self):
        sig = inspect.signature(pack_load.load_chunks)
        param = sig.parameters["sql"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"sql 이 더 이상 키워드 전용이 아니다 -- 위치 인자로 새어 들어올 수 있다: {param.kind}")
        assert param.default is inspect.Parameter.empty, (
            "sql 에 기본값이 생겼다 -- 빠뜨린 호출이 조용히 인가를 건너뛰는 "
            "fail-open 이 된다(#204 에서 신원 축에 되돌린 것과 같은 모양)")

    def test_load_chunks_incremental_sql_is_keyword_only_with_no_default(self):
        sig = inspect.signature(pack_load.load_chunks_incremental)
        param = sig.parameters["sql"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"sql 이 더 이상 키워드 전용이 아니다 -- 위치 인자로 새어 들어올 수 있다: {param.kind}")
        assert param.default is inspect.Parameter.empty, (
            "sql 에 기본값이 생겼다 -- 빠뜨린 호출이 조용히 인가를 건너뛰는 "
            "fail-open 이 된다(#204 에서 신원 축에 되돌린 것과 같은 모양)")
