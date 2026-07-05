"""PG 테스트 환경 sanity + tripwire 검증.

OPENCRAB_PG_TEST_URL 이 실제로 연결 가능한 pgvector 인스턴스를 가리키는지,
그리고 실수로 프로덕션/개발 DB(이름이 `_test`로 끝나지 않는)를 겨냥하지
않았는지 확인하는 안전장치. 다른 PG 관련 테스트(tests/test_pg_graph_doc_parity.py)
가 로드되기 전에 이 파일이 먼저 그 위험을 잡아낸다.
"""

from __future__ import annotations

import os

import pytest

PG_URL = os.environ.get("OPENCRAB_PG_TEST_URL")


# ---------------------------------------------------------------------------
# 정상 (Normal)
# ---------------------------------------------------------------------------


class TestPgEnvNormal:
    """OPENCRAB_PG_TEST_URL이 설정된 경우의 정상 동작."""

    @pytest.mark.skipif(
        not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG env tests skipped"
    )
    def test_select_1(self):
        from sqlalchemy import create_engine, text

        engine = create_engine(PG_URL, connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar() == 1
        finally:
            engine.dispose()

    @pytest.mark.skipif(
        not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG env tests skipped"
    )
    def test_vector_extension_available(self):
        from sqlalchemy import create_engine, text

        engine = create_engine(PG_URL, connect_args={"connect_timeout": 5})
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                exists = conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).scalar()
            assert exists == 1
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# 오류 (Error)
# ---------------------------------------------------------------------------


class TestPgEnvError:
    """연결 실패는 명확하고 빠르게 드러나야 한다."""

    def test_bogus_port_fails_fast_not_hang(self):
        """존재하지 않는 포트로의 연결은 짧은 타임아웃 내에 실패해야 한다(행 방지)."""
        import time

        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import OperationalError

        bogus_url = "postgresql://opencrab:opencrab@localhost:59999/opencrab_test"
        engine = create_engine(bogus_url, connect_args={"connect_timeout": 2})
        start = time.monotonic()
        try:
            with pytest.raises(OperationalError):
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        elapsed = time.monotonic() - start
        assert elapsed < 10, f"connection to bogus port took too long ({elapsed:.1f}s) — not failing fast"


# ---------------------------------------------------------------------------
# 엣지 (Edge) — tripwire
# ---------------------------------------------------------------------------


class TestPgEnvEdgeTripwire:
    """실제 데이터 손상을 막기 위한 tripwire: 반드시 FAIL(skip 아님)해야 한다."""

    @pytest.mark.skipif(
        not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG env tests skipped"
    )
    def test_database_name_must_end_with__test(self):
        db_name = PG_URL.rsplit("/", 1)[-1].split("?", 1)[0]
        assert db_name.endswith("_test"), (
            f"OPENCRAB_PG_TEST_URL points at database {db_name!r}, which does NOT "
            "end with '_test' — refusing to risk running destructive PG tests "
            "against a non-test database. Set OPENCRAB_PG_TEST_URL to a dedicated "
            "*_test database (e.g. opencrab_test)."
        )
