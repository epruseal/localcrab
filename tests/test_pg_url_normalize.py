"""opencrab.stores._pg_url.normalize_pg_url: 드라이버 정규화 계약.

이슈 #414: SQLAlchemy 2.1은 드라이버 미명시 ``postgresql://`` URL의 기본
DBAPI를 psycopg2에서 psycopg(v3)로 바꿨다. 이 저장소는 psycopg2-binary만
설치하므로, 이 함수가 그 갭을 실제로 메우는지와 이미 드라이버가 있는
URL/PostgreSQL이 아닌 URL을 바이트 단위로 보존하는지를 시험으로 고정한다.

바이트 보존 계약은 설계 1라운드 검증에서 실측으로 드러난 회귀
(``render_as_string()``이 쿼리스트링 키 순서를 재정렬하고 값을 다시
percent-encoding하는 문제)의 재발 방지다: 드라이버를 바꿀 필요가 없는
입력은 애초에 ``render_as_string()``을 거치지 않는 조기 반환으로 막는다.
이 시험은 그 조기 반환이 실제로 켜져 있는지를 검사하며, SQLite만으로
돈다(PG 레인 불필요).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import ArgumentError

from opencrab.stores._pg_url import normalize_pg_url


# ---------------------------------------------------------------------------
# 정상 (Normal)
# ---------------------------------------------------------------------------


class TestNormalizePgUrlNormal:
    """드라이버 미명시 postgresql/postgres URL은 +psycopg2로 바뀐다."""

    def test_bare_postgresql_gets_psycopg2_driver(self):
        result = normalize_pg_url("postgresql://opencrab:opencrab@localhost:5432/opencrab_test")
        assert result.startswith("postgresql+psycopg2://")

    def test_postgres_alias_gets_psycopg2_driver(self):
        result = normalize_pg_url("postgres://opencrab:opencrab@localhost:5432/opencrab_test")
        assert result.startswith("postgresql+psycopg2://")

    def test_password_is_preserved_not_masked(self):
        result = normalize_pg_url("postgresql://user:secret@localhost:5432/db")
        assert "secret" in result
        assert "***" not in result

    def test_multi_host_authority_and_query_carrier_preserved(self):
        url = "postgresql://u:p@host-a,host-b:5432/db?dsn=hostaddr%3D1.2.3.4"
        result = normalize_pg_url(url)
        assert result.startswith("postgresql+psycopg2://")
        assert "host-a,host-b" in result
        assert "p" in result
        assert "dsn=hostaddr%3D1.2.3.4" in result

    def test_special_characters_in_username_and_password_preserved(self):
        url = "postgresql://us%40er:p%23a%24ss@localhost:5432/db"
        result = normalize_pg_url(url)
        assert result.startswith("postgresql+psycopg2://")
        assert "us%40er" in result
        assert "p%23a%24ss" in result


# ---------------------------------------------------------------------------
# 오류 (Error)
# ---------------------------------------------------------------------------


class TestNormalizePgUrlError:
    """빈 문자열과 None은 허용하지 않고 명확한 예외로 드러난다."""

    def test_empty_string_raises_argument_error(self):
        with pytest.raises(ArgumentError):
            normalize_pg_url("")

    def test_none_raises_argument_error(self):
        with pytest.raises(ArgumentError):
            normalize_pg_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 엣지 (Edge): 바이트 보존 계약, 1라운드 지적 재발 방지
# ---------------------------------------------------------------------------


class TestNormalizePgUrlEdgePreservation:
    """드라이버가 이미 있는 URL과 PG가 아닌 URL은 바이트 하나도 안 바뀐다."""

    def test_already_psycopg2_url_with_reorderable_query_is_byte_identical(self):
        url = "postgresql+psycopg2://u:p@localhost:5432/db?z=1&a=hello%20world"
        result = normalize_pg_url(url)
        assert result == url
        assert result is url

    def test_already_psycopg_v3_url_is_byte_identical(self):
        url = "postgresql+psycopg://u:p@localhost:5432/db"
        result = normalize_pg_url(url)
        assert result == url
        assert result is url

    def test_already_asyncpg_url_is_byte_identical(self):
        url = "postgresql+asyncpg://u:p@localhost:5432/db"
        result = normalize_pg_url(url)
        assert result == url
        assert result is url

    def test_sqlite_url_with_query_string_is_byte_identical(self):
        url = "sqlite:///./x.db?z=1&a=2"
        result = normalize_pg_url(url)
        assert result == url
        assert result is url

    def test_sqlite_memory_url_is_byte_identical(self):
        url = "sqlite:///:memory:"
        result = normalize_pg_url(url)
        assert result == url
        assert result is url
