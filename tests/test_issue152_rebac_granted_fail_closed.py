"""
``rebac_policies.granted`` 의 boolean 불변식을 고정한다 (이슈 #152).

SQLite 는 BOOLEAN 타입이 없어 ``granted`` 가 INTEGER 로 저장되고, 이 스토어의 구 DDL 에는
값 범위 CHECK 가 없었다. 그래서 직접 SQL 로 ``granted = 2`` 를 넣으면 ``check_policy`` 가
``bool(2)`` 로 허용했다(fail-open). 이 파일은 세 층을 고정한다.

1. 쓰기 경계: ``set_policy`` 는 ``True``/``False`` 와 정확히 ``int`` 0/1 만 받고 나머지를
   ``ValueError`` 로 거부하며 행을 쓰지 않는다. 판정은 DB 에 닿기 전에 끝나므로 SQLite 와
   PostgreSQL 이 같은 예외를 낸다.
2. 읽기 fail-closed: 저장값이 정확히 0/1 (또는 bool) 이 아니면 ``check_policy`` 와
   ``list_policies`` 가 deny(``False``) 로 해석하고 경고를 남긴다. ``None`` 이 아니라 ``False``
   인 이유는 ``ReBACEngine.check`` 가 ``None`` 을 "정책 없음" 으로 보고 그래프 탐색으로
   넘어가 허용할 수 있기 때문이다. 이 파일의 엔진 대조군이 그 차이를 실증한다.
3. DDL: 새로 만든 SQLite 테이블은 ``CHECK (granted IN (0, 1))`` 로 오염 삽입을 거부한다.
   기존 테이블 소급(재생성 마이그레이션)은 이 이슈 범위 밖이며 후속 이슈가 다룬다.

레거시 테이블은 SQLStore 연결 전에 raw sqlite3 로 구 DDL 원문(CHECK 만 없는 형태) 그대로
만들어 시뮬레이션한다. SQLStore 의 DDL 은 ``CREATE TABLE IF NOT EXISTS`` 라 그 테이블을
그대로 둔다. REAL 오염은 1.0 이 아니라 1.5 로 만든다. INTEGER affinity 가 1.0 을 정수 1 로
저장해 오염이 사라지기 때문이다.

PostgreSQL 은 OPENCRAB_PG_TEST_URL 이 있을 때만 uuid 스키마로 격리해 돈다 (없으면 skip).
픽스처 패턴은 test_sql_store_upsert_parity.py 를 따른다.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import opencrab.stores.sql_store as sql_store_module
from opencrab.stores.sql_store import SQLStore

# 이슈 #152 이전 main 의 SQLite DDL 원문. CHECK 만 없고 전 컬럼과 UNIQUE 는 같다.
_LEGACY_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS rebac_policies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   TEXT NOT NULL,
    permission   TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    granted      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (subject_id, permission, resource_id)
)
"""

# (resource_id, SQL 리터럴, 저장 후 typeof). 4건 전부 0/1 이 아닌 오염 값이다.
_DIRTY_ROWS: list[tuple[str, str, str]] = [
    ("dirty-two", "2", "integer"),
    ("dirty-neg", "-1", "integer"),
    ("dirty-text", "'yes'", "text"),
    ("dirty-real", "1.5", "real"),
]

_INVALID_INPUTS: list[Any] = [2, -1, 1.0, 0.0, Decimal(1), "yes", "1", None]


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        store = SQLStore(f"sqlite:///{tmp_path / 'issue152.db'}")
        assert store.available
        try:
            yield store
        finally:
            store._engine.dispose()
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG granted 불변식 테스트 스킵")

    from sqlalchemy import create_engine

    schema = f"t{uuid.uuid4().hex[:12]}_o152"
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
        if store is not None:
            store._engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def legacy_store(tmp_path: Path):
    """CHECK 없는 구 테이블에 정상 2건과 오염 4건을 미리 넣은 SQLite 스토어."""
    db = tmp_path / "legacy152.db"
    raw = sqlite3.connect(db)
    try:
        raw.execute(_LEGACY_SQLITE_DDL)
        raw.execute(
            "INSERT INTO rebac_policies (subject_id, permission, resource_id, granted) "
            "VALUES ('u', 'view', 'ok-true', 1), ('u', 'view', 'ok-false', 0)"
        )
        for rid, literal, _ in _DIRTY_ROWS:
            raw.execute(
                "INSERT INTO rebac_policies (subject_id, permission, resource_id, granted) "
                f"VALUES ('u', 'view', '{rid}', {literal})"
            )
        raw.commit()
    finally:
        raw.close()
    store = SQLStore(f"sqlite:///{db}")
    assert store.available
    try:
        yield store
    finally:
        store._engine.dispose()


def _raw_granted(store: SQLStore, subject_id: str, resource_id: str) -> Any:
    with store._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT granted FROM rebac_policies "
                "WHERE subject_id = :sid AND permission = 'view' AND resource_id = :rid"
            ),
            {"sid": subject_id, "rid": resource_id},
        ).fetchone()
    return None if row is None else row[0]


def _policy_count(store: SQLStore) -> int:
    with store._engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM rebac_policies")).scalar())


# ---------------------------------------------------------------------------
# 판정 헬퍼 (SQLAlchemy 를 거치지 않는다)
# ---------------------------------------------------------------------------


class TestGrantedDomain:
    """쓰기와 읽기가 공유하는 단일 판정식. 구현 전에는 속성 조회에서 실패한다."""

    def _validator(self):
        validator = getattr(sql_store_module, "_is_valid_granted", None)
        assert validator is not None, "sql_store 에 _is_valid_granted 가 없다"
        return validator

    @pytest.mark.parametrize("value", [True, False, 0, 1])
    def test_accepts_bool_and_exact_int_zero_one(self, value):
        assert self._validator()(value) is True

    @pytest.mark.parametrize("value", _INVALID_INPUTS, ids=repr)
    def test_rejects_everything_else(self, value):
        # 1.0 == 1 과 Decimal(1) == 1 은 참이지만 타입이 int 가 아니므로 거부돼야 한다.
        assert self._validator()(value) is False


# ---------------------------------------------------------------------------
# 1층. 쓰기 경계 (양 방언)
# ---------------------------------------------------------------------------


class TestSetPolicyInput:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(True, True), (False, False), (1, True), (0, False)],
        ids=["True", "False", "int1", "int0"],
    )
    def test_valid_inputs_round_trip(self, sql_store, value, expected):
        sql_store.set_policy("s", "view", f"r-{expected}", granted=value)
        assert sql_store.check_policy("s", "view", f"r-{expected}") is expected
        assert _raw_granted(sql_store, "s", f"r-{expected}") in (expected, int(expected))

    @pytest.mark.parametrize("value", _INVALID_INPUTS, ids=repr)
    def test_invalid_input_is_rejected_and_nothing_is_written(self, sql_store, value):
        before = _policy_count(sql_store)
        with pytest.raises(ValueError):
            sql_store.set_policy("s", "view", "r-bad", granted=value)
        assert _policy_count(sql_store) == before
        assert sql_store.check_policy("s", "view", "r-bad") is None

    @pytest.mark.parametrize("value", _INVALID_INPUTS, ids=repr)
    def test_invalid_reset_keeps_existing_row(self, sql_store, value):
        sql_store.set_policy("s", "view", "r-keep", granted=True)
        with pytest.raises(ValueError):
            sql_store.set_policy("s", "view", "r-keep", granted=value)
        assert sql_store.check_policy("s", "view", "r-keep") is True


# ---------------------------------------------------------------------------
# 2층. 읽기 fail-closed (SQLite 레거시 테이블)
# ---------------------------------------------------------------------------


class TestFailClosedRead:
    def test_legacy_rows_are_stored_as_inserted(self, legacy_store):
        """픽스처 자체 검증: 오염 값이 정말 0/1 이 아닌 채로 저장돼 있다."""
        with legacy_store._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT resource_id, granted, typeof(granted) FROM rebac_policies "
                    "WHERE resource_id LIKE 'dirty-%' ORDER BY id"
                )
            ).fetchall()
        assert [(r[0], r[2]) for r in rows] == [(rid, t) for rid, _, t in _DIRTY_ROWS]
        assert all(r[1] not in (0, 1) for r in rows)

    def test_valid_rows_keep_their_meaning(self, legacy_store):
        assert legacy_store.check_policy("u", "view", "ok-true") is True
        assert legacy_store.check_policy("u", "view", "ok-false") is False
        assert legacy_store.check_policy("u", "view", "missing") is None

    @pytest.mark.parametrize("rid", [r[0] for r in _DIRTY_ROWS])
    def test_check_policy_denies_dirty_row_and_warns(self, legacy_store, caplog, rid):
        with caplog.at_level(logging.WARNING, logger="opencrab.stores.sql_store"):
            result = legacy_store.check_policy("u", "view", rid)
        assert result is False, "오염 값은 None(정책 없음) 도 True 도 아니라 deny 여야 한다"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and rid in r.getMessage()]
        assert warnings, f"{rid} 오염 행에 대한 경고 로그가 없다"

    def test_list_policies_denies_dirty_rows_and_warns_per_row(self, legacy_store, caplog):
        with caplog.at_level(logging.WARNING, logger="opencrab.stores.sql_store"):
            policies = {p["resource_id"]: p["granted"] for p in legacy_store.list_policies("u")}
        assert policies["ok-true"] is True
        assert policies["ok-false"] is False
        for rid, _, _ in _DIRTY_ROWS:
            assert policies[rid] is False
            assert any(
                r.levelno == logging.WARNING and rid in r.getMessage() for r in caplog.records
            ), f"list_policies 경로에서 {rid} 경고가 없다"

    def test_list_invalid_policies_reports_dirty_rows_with_raw_type(self, legacy_store):
        found = {p["resource_id"]: p for p in legacy_store.list_invalid_policies()}
        assert set(found) == {rid for rid, _, _ in _DIRTY_ROWS}
        assert found["dirty-two"]["granted"] == 2
        assert found["dirty-two"]["granted_type"] == "int"
        assert found["dirty-text"]["granted"] == "yes"
        assert found["dirty-text"]["granted_type"] == "str"
        assert found["dirty-real"]["granted_type"] == "float"
        assert all(p["subject_id"] == "u" and p["permission"] == "view" for p in found.values())

    def test_list_invalid_policies_is_empty_on_clean_store(self, sql_store):
        sql_store.set_policy("c", "view", "r1", granted=True)
        sql_store.set_policy("c", "view", "r2", granted=False)
        assert sql_store.list_invalid_policies() == []


# ---------------------------------------------------------------------------
# 엔진 대조군: False 대 None 판정의 근거
# ---------------------------------------------------------------------------


class TestEngineControlGroup:
    """그래프가 허용하는 상태에서 오염 정책 행이 있으면 거부, 없으면 허용."""

    @staticmethod
    def _engine(tmp_path: Path, sql: SQLStore):
        from opencrab.ontology.rebac import ReBACEngine
        from opencrab.stores.local_graph_store import LocalGraphStore

        graph = LocalGraphStore(str(tmp_path / "graph152.db"))
        graph.upsert_node("User", "u", {"name": "u"})
        graph.upsert_node("Resource", "dirty-two", {"name": "r"})
        graph.upsert_node("Resource", "no-policy", {"name": "r2"})
        graph.upsert_edge("User", "u", "owns", "Resource", "dirty-two")
        graph.upsert_edge("User", "u", "owns", "Resource", "no-policy")
        return ReBACEngine(neo4j=graph, sql=sql)

    def test_graph_grants_when_no_policy_row(self, tmp_path, legacy_store):
        engine = self._engine(tmp_path, legacy_store)
        decision = engine.check("u", "view", "no-policy")
        assert decision.granted is True, "대조군: 정책 행이 없으면 그래프 경로가 허용해야 한다"

    def test_dirty_policy_row_denies_even_when_graph_would_grant(self, tmp_path, legacy_store):
        engine = self._engine(tmp_path, legacy_store)
        decision = engine.check("u", "view", "dirty-two")
        assert decision.granted is False


# ---------------------------------------------------------------------------
# 3층. 신규 SQLite DDL
# ---------------------------------------------------------------------------


class TestNewSqliteDdl:
    def test_fresh_table_has_check_constraint(self, tmp_path):
        store = SQLStore(f"sqlite:///{tmp_path / 'fresh152.db'}")
        try:
            with store._engine.connect() as conn:
                ddl = conn.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'rebac_policies'")
                ).scalar()
        finally:
            store._engine.dispose()
        assert "CHECK (granted IN (0, 1))" in " ".join(ddl.split())

    @pytest.mark.parametrize("literal", ["2", "-1", "'yes'", "1.5"])
    def test_fresh_table_rejects_direct_dirty_insert(self, tmp_path, literal):
        store = SQLStore(f"sqlite:///{tmp_path / 'fresh152b.db'}")
        try:
            with pytest.raises(IntegrityError):
                with store._engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO rebac_policies "
                            "(subject_id, permission, resource_id, granted) "
                            f"VALUES ('u', 'view', 'r', {literal})"
                        )
                    )
            assert store.check_policy("u", "view", "r") is None
        finally:
            store._engine.dispose()
