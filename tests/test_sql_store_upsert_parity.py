"""
SQLStore 의 upsert 가 SQLite 와 PostgreSQL 에서 같은 의미를 갖는지 고정한다 (이슈 #81).

``register_node`` 와 ``set_policy`` 는 같은 키로 다시 호출됐을 때 **삭제 후 재삽입이 아니라
기존 행을 갱신**해야 한다. SQLite 의 ``INSERT OR REPLACE`` 는 DELETE 후 INSERT 이므로
AUTOINCREMENT ``id`` 를 새로 할당하고 ``created_at`` DEFAULT 를 재평가한다. PG 의
``ON CONFLICT ... DO UPDATE`` 는 둘 다 보존한다. 이 파일이 고정하는 공통 계약은 그 두 컬럼이다.

SQLite 에서는 같은 재작성이 행의 rowid 도 유지한다(여기서 ``id`` 는 rowid 별칭이다). 그 안정성이
왜 중요한지는 ``_sql_dialect.py`` 의 "ROWID STABILITY" 절과
``test_pg_graph_doc_parity.py::TestUpsertRowidStability`` 가 그래프 스토어에 대해 서술하고
고정한다. PostgreSQL 은 그런 보장을 주지 않는다 — UPDATE 가 MVCC 상 새 튜플 버전을 쓴다 — 그리고
이 파일도 그것을 요구하지 않는다.

같은 계약이 그래프/문서 스토어에 대해서는 ``_sql_dialect.py`` 의 "ROWID STABILITY" 절과
``test_pg_graph_doc_parity.py::TestUpsertRowidStability`` 로 이미 고정돼 있다. SQLStore 는 그
계약이 적용되지 않았던 마지막 프로덕션 지점이었다.

SQLite 는 tmp 파일 DB 로 항상 돌고, PostgreSQL 은 OPENCRAB_PG_TEST_URL 이 있을 때만 uuid
스키마로 격리해 돈다 (없으면 skip). 픽스처 패턴은 test_execution_sql.py 를 따르되, 스키마
생성 이후 모든 이탈 경로에서 정리되도록 try/finally 로 감싼다.

시각 단언은 sleep 에 기대지 않는다. SQLite 의 ``datetime('now')`` 는 1초 해상도라 같은 초
안에서 두 번 호출하면 ``created_at`` 이 재평가돼도 문자열이 같아져 결함을 놓친다. 대신 최초
등록 직후 타임스탬프를 명백한 과거 sentinel 로 직접 내려두고 재등록한다. 보존이면 sentinel 이
그대로 남고, 파괴면 현재 시각으로 바뀐다 -- 초 해상도와 무관하게 결정적이다.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from opencrab.stores.sql_store import SQLStore

# 명백한 과거 값. 재등록이 타임스탬프를 재평가하면 현재 시각으로 덮이므로 즉시 드러난다.
_SENTINEL_SQLITE = "2000-01-01 00:00:00"
_SENTINEL_PG = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        store = SQLStore(f"sqlite:///{tmp_path / 'upsert_parity.db'}")
        assert store.available
        try:
            yield store
        finally:
            store._engine.dispose()
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG upsert parity 테스트 스킵")

    from sqlalchemy import create_engine

    schema = f"t{uuid.uuid4().hex[:12]}_o81"
    admin = create_engine(dsn)
    store = None
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        sep = "&" if "?" in dsn else "?"
        store = SQLStore(f"{dsn}{sep}options=-csearch_path%3D{schema}")
        if not store.available:
            pytest.skip(f"PG 테스트 DB 접속 불가: {dsn!r}")
        yield store
    finally:
        # skip, 테스트 실패, 예외 어느 경로로 빠져나가도 스키마를 남기지 않는다.
        if store is not None:
            store._engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _sentinel(store: SQLStore) -> Any:
    """방언에 맞는 과거 sentinel 값. SQLite 는 TEXT, PG 는 TIMESTAMPTZ 컬럼이다."""
    return _SENTINEL_SQLITE if store._is_sqlite else _SENTINEL_PG


def _node_row(store: SQLStore, space: str, node_id: str) -> dict[str, Any] | None:
    with store._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, node_type, created_at, updated_at FROM ontology_nodes "
                "WHERE space = :space AND node_id = :nid"
            ),
            {"space": space, "nid": node_id},
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "node_type": row[1], "created_at": row[2], "updated_at": row[3]}


def _policy_row(store: SQLStore, sid: str, perm: str, rid: str) -> dict[str, Any] | None:
    with store._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, granted, created_at FROM rebac_policies "
                "WHERE subject_id = :sid AND permission = :perm AND resource_id = :rid"
            ),
            {"sid": sid, "perm": perm, "rid": rid},
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "granted": bool(row[1]), "created_at": row[2]}


def _count(store: SQLStore, table: str, where: str, params: dict[str, Any]) -> int:
    with store._engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).scalar_one()
        )


def _backdate_node(store: SQLStore, space: str, node_id: str) -> None:
    """created_at 과 updated_at 을 과거 sentinel 로 내린다 (RED 를 결정적으로 만든다)."""
    with store._engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ontology_nodes SET created_at = :ts, updated_at = :ts "
                "WHERE space = :space AND node_id = :nid"
            ),
            {"ts": _sentinel(store), "space": space, "nid": node_id},
        )


def _backdate_policy(store: SQLStore, sid: str, perm: str, rid: str) -> None:
    with store._engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rebac_policies SET created_at = :ts "
                "WHERE subject_id = :sid AND permission = :perm AND resource_id = :rid"
            ),
            {"ts": _sentinel(store), "sid": sid, "perm": perm, "rid": rid},
        )


# ---------------------------------------------------------------------------
# 정상
# ---------------------------------------------------------------------------


class TestUpsertPreservesIdentityNormal:
    def test_register_node_reregistration_preserves_id_and_created_at(self, sql_store):
        """재등록은 삭제 후 재삽입이 아니라 기존 행 갱신이어야 한다: id 와 created_at 은
        그대로, updated_at 과 node_type 만 갱신된다."""
        sql_store.register_node("subject", "User", "u1")
        _backdate_node(sql_store, "subject", "u1")
        before = _node_row(sql_store, "subject", "u1")
        assert before is not None
        assert before["created_at"] == _sentinel(sql_store)

        sql_store.register_node("subject", "Agent", "u1")
        after = _node_row(sql_store, "subject", "u1")
        assert after is not None

        assert after["id"] == before["id"], "재등록이 새 id 를 할당했다 (REPLACE = DELETE+INSERT)"
        assert after["created_at"] == before["created_at"], (
            "재등록이 created_at 을 재평가했다 -- '최초 생성 시각' 의미가 깨진다"
        )
        assert after["updated_at"] != _sentinel(sql_store), "재등록이 updated_at 을 갱신하지 않았다"
        assert after["node_type"] == "Agent", "재등록이 node_type 갱신을 반영하지 않았다"

    def test_set_policy_reset_preserves_id_and_created_at(self, sql_store):
        sql_store.set_policy("u1", "view", "r1", granted=True)
        _backdate_policy(sql_store, "u1", "view", "r1")
        before = _policy_row(sql_store, "u1", "view", "r1")
        assert before is not None
        assert before["granted"] is True
        assert before["created_at"] == _sentinel(sql_store)

        sql_store.set_policy("u1", "view", "r1", granted=False)
        after = _policy_row(sql_store, "u1", "view", "r1")
        assert after is not None

        assert after["id"] == before["id"], "정책 재설정이 새 id 를 할당했다"
        assert after["created_at"] == before["created_at"], "정책 재설정이 created_at 을 재평가했다"
        assert after["granted"] is False, "정책 재설정이 granted 갱신을 반영하지 않았다"

    def test_first_registration_writes_row_with_non_null_timestamps(self, sql_store):
        """신규 INSERT 경로: 두 테이블 모두 행 하나가 생기고 타임스탬프가 NULL 이 아니다.
        (updated_at 을 INSERT 컬럼에 명시하는 이유 -- DDL DEFAULT 에만 기대지 않는다.)"""
        sql_store.register_node("subject", "User", "fresh-node")
        sql_store.set_policy("fresh-subj", "view", "fresh-res", granted=True)

        node = _node_row(sql_store, "subject", "fresh-node")
        assert node is not None
        assert node["node_type"] == "User"
        assert node["created_at"] is not None
        assert node["updated_at"] is not None

        policy = _policy_row(sql_store, "fresh-subj", "view", "fresh-res")
        assert policy is not None
        assert policy["granted"] is True
        assert policy["created_at"] is not None


# ---------------------------------------------------------------------------
# 오류 / 계약
# ---------------------------------------------------------------------------


class TestUpsertContract:
    def test_sql_store_source_has_no_or_replace(self):
        """계약 핀. ``_sql_dialect.py`` 의 "ROWID STABILITY" 절이 정한 대로 upsert 는 양 방언
        모두 ON CONFLICT DO UPDATE 여야 하며 OR REPLACE 를 쓰지 않는다. 같은 형태의 부재
        단언이 ``test_sql_dialect.py`` 에도 있다."""
        from opencrab.stores import sql_store as sql_store_module

        source = Path(sql_store_module.__file__).read_text(encoding="utf-8")
        assert "OR REPLACE" not in source, (
            "sql_store.py 가 OR REPLACE 로 되돌아갔다 -- rowid 와 created_at 이 파괴된다"
        )

    def test_executed_sql_uses_on_conflict_do_update(self, sql_store):
        """소스 텍스트 핀보다 강한 실동작 핀: 두 메서드가 엔진에 실제로 넘긴 SQL 을 가로채
        REPLACE 형태가 아니라 ON CONFLICT DO UPDATE 임을 확인한다. 소스 핀은 docstring 의
        표현까지 걸리지만 이 핀은 실행 경로만 본다 -- 방언 분기가 되살아나면 SQLite 파라미터에서
        곧바로 드러난다."""
        from sqlalchemy import event

        captured: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
            captured.append(statement)

        event.listen(sql_store._engine, "before_cursor_execute", _capture)
        try:
            sql_store.register_node("subject", "User", "sql-capture")
            sql_store.set_policy("cap-s", "view", "cap-r", granted=True)
        finally:
            event.remove(sql_store._engine, "before_cursor_execute", _capture)

        writes = [
            stmt
            for stmt in captured
            if "ontology_nodes" in stmt or "rebac_policies" in stmt
        ]
        assert len(writes) == 2, f"기대한 쓰기 2건이 아니라 {len(writes)}건: {writes}"
        for stmt in writes:
            upper = stmt.upper()
            assert "OR REPLACE" not in upper, f"실행된 SQL 이 REPLACE 형태다: {stmt}"
            assert "ON CONFLICT" in upper, f"실행된 SQL 에 ON CONFLICT 가 없다: {stmt}"
            assert "DO UPDATE" in upper, f"실행된 SQL 에 DO UPDATE 가 없다: {stmt}"

    def test_reregistration_does_not_add_rows(self, sql_store):
        sql_store.register_node("subject", "User", "dup")
        sql_store.register_node("subject", "User", "dup")
        sql_store.set_policy("dup-s", "view", "dup-r", granted=True)
        sql_store.set_policy("dup-s", "view", "dup-r", granted=True)

        assert (
            _count(
                sql_store,
                "ontology_nodes",
                "space = :space AND node_id = :nid",
                {"space": "subject", "nid": "dup"},
            )
            == 1
        )
        assert (
            _count(
                sql_store,
                "rebac_policies",
                "subject_id = :sid AND permission = :perm AND resource_id = :rid",
                {"sid": "dup-s", "perm": "view", "rid": "dup-r"},
            )
            == 1
        )


# ---------------------------------------------------------------------------
# 엣지
# ---------------------------------------------------------------------------


class TestUpsertConflictKeyEdges:
    def test_same_node_id_in_different_spaces_are_distinct_rows(self, sql_store):
        """충돌 키는 (space, node_id) 다 -- node_id 만 같으면 별개 행이어야 한다."""
        sql_store.register_node("subject", "User", "shared-id")
        sql_store.register_node("resource", "Project", "shared-id")

        assert _count(sql_store, "ontology_nodes", "node_id = :nid", {"nid": "shared-id"}) == 2
        assert _node_row(sql_store, "subject", "shared-id")["node_type"] == "User"
        assert _node_row(sql_store, "resource", "shared-id")["node_type"] == "Project"

    def test_same_subject_permission_different_resource_are_distinct_rows(self, sql_store):
        """충돌 키는 (subject_id, permission, resource_id) 셋 모두다."""
        sql_store.set_policy("multi", "view", "r-a", granted=True)
        sql_store.set_policy("multi", "view", "r-b", granted=False)

        assert (
            _count(
                sql_store,
                "rebac_policies",
                "subject_id = :sid AND permission = :perm",
                {"sid": "multi", "perm": "view"},
            )
            == 2
        )
        assert _policy_row(sql_store, "multi", "view", "r-a")["granted"] is True
        assert _policy_row(sql_store, "multi", "view", "r-b")["granted"] is False

    def test_repeated_reregistration_keeps_identity_stable(self, sql_store):
        """1회성 보존이 아니라 반복 보존임을 고정한다. AUTOINCREMENT 는 재사용을 하지 않으므로
        REPLACE 였다면 호출마다 id 가 단조 증가한다."""
        sql_store.register_node("subject", "User", "repeat")
        _backdate_node(sql_store, "subject", "repeat")
        first = _node_row(sql_store, "subject", "repeat")

        for _ in range(3):
            sql_store.register_node("subject", "User", "repeat")
            current = _node_row(sql_store, "subject", "repeat")
            assert current["id"] == first["id"]
            assert current["created_at"] == first["created_at"]

        assert (
            _count(
                sql_store,
                "ontology_nodes",
                "space = :space AND node_id = :nid",
                {"space": "subject", "nid": "repeat"},
            )
            == 1
        )
