#!/usr/bin/env python3
"""Repair pgvector rows whose ``pack_id`` holds the legacy literal ``'None'``.

이슈 #306 (#197의 데이터 후속). #197/PR#303 이전 pgvector 쓰기 경로는
``str(meta.get("pack_id", ""))``를 썼다. 이 표현은 ``metadata`` 키가 "없을 때만"
기본값을 쓰므로, ``metadata={"pack_id": None, ...}``처럼 키는 있고 값이 파이썬
``None``인 경우 그 값을 문자열화해 리터럴 ``"None"``을 ``pack_id`` 컬럼에 그대로
적었다. 현재 게이트(``reject_foreign_slot_writes``)는 이 리터럴을 "None이라는
이름의 팩이 소유한 슬롯"으로 읽으므로, 원래 그 슬롯의 정당한 소유자(실제로는
UNOWNED로 재적재하려는 호출자)가 재적재하면 거부된다. 이 스크립트는 그 과거
버그가 이미 써놓은 **잔존 데이터**를 복구한다. 현재 코드에는 결함이 없다
(``tests/test_vector_slot_ownership.py::TestNonePackIdIsStoredAsUnowned`` 참고).

대상은 pgvector 뿐이다: sqlite-vec은 ``_sanitize_metadata``가 저장 전에 값을
접어 이 형태가 구조적으로 발생할 수 없고, chroma는 전용 ``pack_id`` 컬럼이 없다.

탐지 조건은 ``pack_id = 'None'`` 과 ``metadata @> '{"pack_id": null}'::jsonb``
(메타 키가 실제로 존재하고 값이 JSON null인 경우만) 둘 다를 요구한다: 진짜로
"None"이라는 이름의 팩이 소유한 행(그 metadata에는 문자열 ``"None"``이 그대로
들어 있어 이 containment 조건을 만족하지 않는다)까지 오염으로 오인해 파괴하지
않기 위함이다.

SAFETY (Autonomy Contract 매핑):
  - 기본 dry-run. 실제 쓰기는 명시적 ``--apply``가 있어야 한다.
  - ``--apply``는 반드시 백업을 선행한다(``--backup-to <path>``, 또는 명시적
    ``--skip-backup``). 백업은 영향받는 행만 담은 자기완결 JSON 스냅샷이다
    (``pg_dump`` 전체 스냅샷은 이 좁은 단일 컬럼 복구에 과도하다).
  - 백업 파일은 임시 파일에 완전히 쓰고 fsync한 뒤 ``os.link()``로 최종 경로에
    배타 생성(이미 있으면 실패, 덮어쓰지 않음)하고, 성공 후 부모 디렉터리를
    fsync한다. 백업 경로(``--backup-to``)를 지정했고 대상 행이 있으면 DB
    트랜잭션은 이 게시가 전부 성공한 뒤에만 COMMIT한다. 백업 실패는 트랜잭션
    전체를 ROLLBACK시킨다(코드 4). 백업 경로를 지정했어도 대상 행이 0건이면
    백업할 내용이 없으므로 파일을 만들지 않고 그대로 COMMIT한다. 이 두 경로에서는
    "DB는 바뀌었는데 백업이 없는" 상태가 구조적으로 불가능하다. ``--skip-backup``
    으로 백업 자체를 명시적으로 생략한 경로는 이 게이트 대상이 아니며, 대상 행이
    있어도 게시 없이 바로 COMMIT한다(사용자가 직접 선택한 생략이지 유실이 아니다).
  - 탐지-백업-UPDATE는 단일 SQL 문(``WITH ... FOR UPDATE ... UPDATE ... RETURNING``)
    으로 원자화한다: 별도 SELECT 후 조건부 UPDATE 방식의 TOCTOU 경합이 없다.
  - 실행 전 예상 건수와 실제 rowcount가 다르면 트랜잭션을 중단한다(코드 5).
  - 멱등: 이미 고쳐진 행(``pack_id=''``)은 탐지 조건에 애초에 걸리지 않으므로
    재실행해도 안전하게 0건 처리된다.
  - 라이브 데이터에는 실행하지 않는다. 검증은 격리된 개발/테스트 인스턴스에서만
    한다.
  - ``--pg-url``(또는 ``POSTGRES_URL``)을 출력할 때 authority 구역의 비밀번호는
    ``***``로 가린다(``sqlalchemy.engine.url.make_url(...).render_as_string(
    hide_password=True)``). **알려진 한계**: 이 마스킹은 DSN의 query 문자열
    파라미터(예: ``?sslpassword=...``)까지는 가리지 않는다. 이 저장소의 실제
    DSN 관례(``opencrab/config.py``의 ``postgres_url`` 기본값과 그 값을 그대로
    넘기는 호출자들)는 query에 비밀을 담지 않지만, ``--pg-url``로 임의 DSN을
    주는 것 자체는 코드로 막혀 있지 않다. query에 비밀을 담은 DSN을 쓰는
    운영자는 이 출력이 그 비밀까지 가려주지 않는다는 점을 알아야 한다.
  - 스냅샷은 ``table``/``database``/``host``/``port``/``schema`` 다섯 값 전부가
    현재 대상과 일치할 때만 유효하다(``validate_snapshot_signature``). 대상
    서버를 하나로
    특정할 수 없는 ``--pg-url``(다중 호스트, PostgreSQL ``service`` 설정,
    소켓-경유-쿼리스트링 등 -- ``target_identity_reason`` 참고)이면 ``--apply``
    자체를 거부한다(코드 3, DB 쓰기 0건). ``--skip-backup``을 명시했을 때만
    같은 사유를 정보성으로 출력하고 스냅샷 없이 수리를 진행한다(운영자가
    직접 롤백을 포기한 경로이므로).
  - **알려진 한계 (대상 서버 식별)**: ``target_identity_reason``이 보는
    근거는 DSN 문자열과 프로세스 환경(``PGHOSTADDR``/``PGSERVICE``/
    ``PGSERVICEFILE``/``PGPORT`` 등)뿐이다. 이 검사를 조건 없이 수행하는
    것은 ``main()``뿐이다(``--rollback-from`` 분기에 들어가기 전 1회).
    ``repair()``는 ``backup_to``와 처리된 행이 모두 있을 때만 이 검사를
    스스로 수행한다. 라이브러리로 직접 호출해도 이 조건이 성립하면 검사를
    거친다. 다만 ``backup_to``가 없거나(``--skip-backup``) 처리 대상 행이
    0건이면 ``repair()``는 이 검사를 건너뛴다. ``rollback()``은 반대로 어떤
    호출 경로에서도 이 검사를 스스로 수행하지 않는다: CLI에서 오는
    ``--rollback-from``만 ``main()``의 사전 검사를 거치고, 테스트처럼
    ``rollback()``을 라이브러리로 직접 호출하면(``main()``을 거치지 않으므로)
    이 식별 검사 자체가 없다. 이 모듈의 ``connect()``를 거치지 않고
    ``connect_args``로 직접 만든 ``Engine``을 넘기는 경로도 마찬가지로 이
    판정 범위 밖이다. 또한 이
    검사는 DSN의 호스트 이름이 안정적으로 같은 서버를 가리킨다고
    가정한다. DNS나 프록시가 같은 이름을 다른 서버로 돌리면 검사는
    통과하지만 실제 대상은 달라진다. (``_resolve_table_schema``는
    이와 달리 이미 맺어진 연결 위에서 ``pg_namespace``/``pg_class``/
    ``to_regclass()``를 직접 질의하므로 DSN/환경 근거에 의존하지
    않지만, 그 연결 자체가 의도한 서버로 갔는지는 판정하지 않는다.)
    근본 해법은 연결 뒤 서버 자신에게 물어(``current_database()``,
    ``inet_server_addr()``, ``inet_server_port()``,
    ``pg_postmaster_start_time()``) 그 값을 스냅샷 서명으로 쓰는 것이며,
    이 PR은 그 서버-질의 결속을 구현하지 않았다.
  - **알려진 한계 (스냅샷 모양 검증)**: ``load_snapshot()``의 입력 검증은
    지금까지 실측된 손상/조작 형태(비객체 최상위, 비배열 ``rows``, 누락/
    비문자열 ``node_id``/``xmin``, 중복 ``node_id``, 비UTF-8, 과대 정수,
    깊은 재귀 등)를 하나씩 열거해 막는다. 이 열거에 없는 새로운 손상
    형태는 여전히 처리되지 않은 예외로 새어나갈 수 있다. 근본 해법은
    스키마 하나로 필수 키/타입/버전을 한 번에 검증하는 것이며, 이 PR은
    그 스키마 계층을 도입하지 않았다.
  - **알려진 한계 (백업 게시 경합 방어의 범위)**: ``write_backup_atomic``의
    TOCTOU 방어(``O_EXCL``/``O_NOFOLLOW`` 임시 파일 생성, inode 대사)는
    그 디렉터리에 **쓰기 권한이 없는** 로컬 관찰자의 심볼릭 링크 선점/
    바꿔치기만 막는다. 그 디렉터리에 쓸 수 있는 주체(같은 사용자로 도는
    다른 프로세스, 또는 같은 파일시스템 권한을 공유하는 다른 계정)는 이
    방어의 대상이 아니다 -- 다만 그런 주체는 이미 이 스크립트가 쓰는
    PostgreSQL 자격증명에도 접근할 개연성이 높으므로, 백업을 무력화하기
    보다 DB를 직접 고치는 쪽이 더 쉬운 공격면이다.

ROLLBACK (``--rollback-from <snapshot.json>``, ``--apply`` 필요):
  스냅샷이 기록한 각 행에 대해 PostgreSQL 시스템 컬럼 ``xmin``(그 행을 마지막으로
  쓴 트랜잭션 id)이 복구 직후 값과 여전히 같을 때만 되돌린다. ``xmin``이 다르면
  그 행은 복구 이후 어떤 형태로든(정당한 소유자의 인수, 삭제 후 재생성, 다른
  도구의 수정) 다시 쓰인 것이므로, metadata 내용이 우연히 같아도 물리적으로
  다른 행 버전이면 정확히 구분해 건너뛴다. 조용히 덮어쓰지 않고 각 행을
  롤백됨/xmin 불일치로 건너뜀/이미 삭제됨 세 상태로 분류해 전부 보고한다.

  **한계**: PostgreSQL 트랜잭션 id는 32비트로 순환하고, ``VACUUM FREEZE``가
  오래된 행의 ``xmin``을 ``FrozenTransactionId``로 바꿔 원래 값을 지운다. 이
  도구의 수리부터 롤백까지의 창은 운영자가 즉시 또는 곧 실행하는 짧은 구간을 전제하므로
  실무상 문제가 되지 않지만, **오래된 백업 파일(며칠~몇 주 뒤)에 대한
  ``--rollback-from``은 안전하다고 보장하지 않는다**. 이 한계는 실행 시점마다
  stderr 경고로도 출력된다(문서/docstring만 읽지 않는 운영자도 보게 하기 위함).

  백업 서명(``table``/``database``/``host``/``port``/``schema``)이 현재
  대상과 다르면 즉시 거부한다(코드 4). 대상 서버를 하나로 특정할 수 없는 ``--pg-url``이면
  서명 자체가 엉뚱한 서버와 비교될 수 있으므로 스냅샷을 읽기도 전에 거부한다
  (코드 3). ``--apply`` 수리 경로와 달리 ``--rollback-from``에는
  ``--skip-backup`` 같은 탈출구가 없다: 롤백은 스냅샷의 서버 결속 자체가
  전제이므로 이 결속은 선택 사항이 아니다.

EXIT CODES:
  0 성공, 2 사용법/안전 게이트 실패, 3 연결/사전조건 실패, 4 백업 검증 실패,
  5 건수 불일치(UPDATE/롤백 rowcount != 예상, 또는 롤백 부분 처리).

Usage:
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ...
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ... \\
        --apply --backup-to /path/to/backup.json
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ... \\
        --apply --rollback-from /path/to/backup.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from opencrab.stores._graph_common import IDENT_RE

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_BACKUP = 4
EXIT_COUNT_MISMATCH = 5


class CountMismatchError(RuntimeError):
    """실행 전 예상 건수와 실제 rowcount가 다를 때."""


class SnapshotError(ValueError):
    """백업 스냅샷 파일이 손상됐거나, 서명이 대상과 다르거나, 중복 node_id가 있을 때."""


class BackupPublicationStateError(OSError):
    """The backup publication state or the database outcome needs an operator.

    ``publication_state`` is one of "published", "not published", "unconfirmed".
    "unconfirmed" is the only state that means this process could not prove what is
    on disk. ``db_outcome`` says what happened to the database transaction, or is None
    when the caller has not set it yet. This process preserves a path when it cannot
    prove the entry is its own. Do not retry the same backup path automatically.
    The text names only the backup path chosen by the user and the temp name.
    """

    def __init__(self, detail: str, *, publication_state: str, db_outcome: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.publication_state = publication_state
        self.db_outcome = db_outcome

    def __str__(self) -> str:
        outcome = self.db_outcome if self.db_outcome is not None else "not applicable"
        state = self.publication_state
        if state == "unconfirmed":
            state = "unconfirmed (uncertain)"  # the only state that means this process could not prove what is on disk
        return f"backup publication state: {state}; database outcome: {outcome}; {self.detail}"


def _check_ident(name: str, label: str) -> None:
    if not IDENT_RE.fullmatch(name):
        raise ValueError(f"Unsafe {label}: {name!r}")


def _is_db_safe_text(value: str) -> bool:
    """psycopg2 가 텍스트 바인드 파라미터로 받아들일 수 있는 문자열인지 본다.

    JSON 문자열 이스케이프(``\\ud800`` 같은 짝 없는 서로게이트)는
    ``json.load()`` 자체는 오류 없이 통과시키지만, 그 결과 ``str`` 은
    ``encode()`` 가능한 코덱이 하나도 없어 psycopg2 바인드 시점에
    ``UnicodeEncodeError`` 를 낸다. NUL 문자(``\\u0000``)도 PostgreSQL
    ``text`` 컬럼이 담을 수 없어 psycopg2 가 클라이언트 측에서
    ``ValueError`` 를 낸다. 둘 다 이 도구 자신이 DB 에서 읽어 쓰는 값
    (``node_id``, ``xmin::text``)에는 나타나지 않으므로, 나타나면 수기
    조작이나 손상된 스냅샷으로 보고 여기서 미리 거부한다."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _as_dict_metadata(value: Any) -> dict[str, Any]:
    """JSONB 값을 dict로 정규화(드라이버가 str/dict 어느 쪽을 주든 안전)."""
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


def _resolve_table_schema(conn: Any, table: str) -> str | None:
    """``table``이 현재 ``search_path``에서 실제로 가리키는 릴레이션의
    스키마를 구한다.

    ``current_schema()``는 ``search_path``의 첫 스키마를 그대로 돌려줄 뿐,
    그 스키마에 ``table``이 없고 뒤 순번 스키마에만 있으면 틀린 값을 준다
    (이중 적대검증, 코덱스 리뷰의 실측 재현). ``to_regclass``는
    ``_repair_sql``의 비한정 ``FROM {table}``과 똑같은 ``search_path``
    해석 규칙으로 릴레이션을 찾으므로, 실제로 수리/롤백이 건드리는
    테이블과 항상 같은 스키마를 돌려준다. 그 이름의 릴레이션이 없으면
    ``None``을 돌려준다."""
    from sqlalchemy import text

    return conn.execute(
        text(
            "SELECT n.nspname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass(:tbl)"
        ),
        {"tbl": table},
    ).scalar()


# ---------------------------------------------------------------------------
# SQL (테이블명은 IDENT_RE로 검증된 값만 f-string 보간한다)
# ---------------------------------------------------------------------------


def _detect_sql(table: str) -> str:
    return (
        f"SELECT node_id, pack_id, metadata FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb "
        "ORDER BY node_id"
    )


def _count_sql(table: str) -> str:
    return (
        f"SELECT count(*) FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb"
    )


def _repair_sql(table: str) -> str:
    return (
        "WITH to_fix AS ("
        f"SELECT node_id, pack_id, metadata FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb "
        "FOR UPDATE"
        ") "
        f"UPDATE {table} SET pack_id = '' "
        f"FROM to_fix WHERE {table}.node_id = to_fix.node_id "
        f"RETURNING {table}.node_id, to_fix.pack_id AS prior_pack_id, "
        f"to_fix.metadata, {table}.xmin::text AS xmin"
    )


def _audit_sql(table: str) -> str:
    return (
        f"SELECT pack_id, count(*) AS n FROM {table} "
        "WHERE pack_id IS NOT NULL AND pack_id != '' AND pack_id != 'None' "
        "AND (pack_id ~ '^\\s*$' OR lower(pack_id) IN ('null', 'none')) "
        "GROUP BY pack_id ORDER BY pack_id"
    )


def _rollback_row_sql(table: str) -> str:
    return (
        "WITH cand AS ("
        f"SELECT node_id FROM {table} "
        "WHERE node_id = :node_id AND pack_id = '' AND xmin::text = :xmin "
        "FOR UPDATE"
        ") "
        f"UPDATE {table} SET pack_id = 'None' "
        f"FROM cand WHERE {table}.node_id = cand.node_id "
        f"RETURNING {table}.node_id"
    )


# ---------------------------------------------------------------------------
# 연결
# ---------------------------------------------------------------------------


def connect(pg_url: str) -> Any:
    from sqlalchemy import create_engine

    from opencrab.stores._pg_url import normalize_pg_url

    return create_engine(
        normalize_pg_url(pg_url), pool_pre_ping=True, hide_parameters=True
    )


_SAFE_TARGET_QUERY_KEYS = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "sslpassword",
        "connect_timeout",
        "application_name",
        "fallback_application_name",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "client_encoding",
        "tcp_user_timeout",
    }
)


def target_identity_reason(engine: Any) -> str | None:
    """스냅샷을 특정 서버 하나에 안전하게 결속할 수 없으면 그 사유 문자열을,
    결속할 수 있으면 ``None``을 반환한다.

    ``engine.url.host``만 보면 안 된다: SQLAlchemy/libpq는 DSN의 query
    문자열에 ``host``/``port``/``service``/``hostaddr``가 있으면 그 값으로
    실제 연결 대상을 덮어쓴다(authority의 host는 무시됨). 예:
    ``postgresql://u:p@localhost/db?host=h2,h3`` 는 ``engine.url.host``가
    ``'localhost'``로 무해해 보이지만 실제 연결은 ``h2,h3``로 간다. 또한
    authority 자체에 콤마로 구분한 다중 호스트(``postgresql://u:p@h1,h2/db``,
    libpq의 다중-호스트/failover 문법)를 적어도 ``engine.url.host``가
    ``'h1,h2'``라는 비어 있지 않은 문자열이 되어 위 빈 값 검사를 통과하지만,
    실제 연결은 h1과 h2 가운데 그때그때 다른 서버로 갈 수 있어 스냅샷을 한
    서버에 결속할 수 없다. PostgreSQL service 설정, 소켓-경유-쿼리스트링
    형태도 authority만으로는 검출되지 않으므로 query 키 자체를 함께 거부한다.
    ``host``/``port``/``service``/``hostaddr`` 이름만 나열하는 블록리스트로는
    부족하다: psycopg2는 query의 ``dsn`` 키를 conninfo 문자열로 병합하는데,
    그 문자열 안의 ``hostaddr``/``host``/``service``는 kwargs에 같은 키가
    없는 한 그대로 살아남아 authority와 무관하게 실제 연결 대상을 정한다
    (실측: authority host가 존재하지 않는 이름이어도
    ``?dsn=hostaddr%3D...``가 실제 서버로 연결을 성공시킨다). 드라이버마다
    이런 캐리어 키(예: psycopg3의 ``conninfo``)가 더 있을 수 있으므로,
    위험 키만 나열하는 대신 알려진 안전 키(``_SAFE_TARGET_QUERY_KEYS``)만
    허용하는 화이트리스트로 이 범주 전체를 막는다.

    DSN 문자열만으로는 부족하다: ``PGHOSTADDR``/``PGSERVICE``/``PGSERVICEFILE``
    환경변수는 DSN이 그 값을 직접 주지 않는 한 실제 연결 시점에 적용돼,
    DSN의 host와 무관한 주소로 연결을 보낼 수 있다(실측: DSN host가
    존재하지 않는 이름이어도 ``PGHOSTADDR``가 설정돼 있으면 그 주소로 연결에
    성공한다). ``PGPORT``도 DSN이 포트를 생략했을 때만 같은 방식으로 적용된다.
    이 값들은 프로세스 환경이라 DSN을 아무리 명확히 적어도 스냅샷 기록
    (``engine.url.host``/``port``)과 실제 접속 서버가 갈라질 수 있으므로,
    query 키와 같은 이유로 함께 거부한다.

    host/port가 확정돼도 부족한 경우가 하나 더 있다: ``options`` query 키
    (그리고 이를 DSN 없이 대신하는 ``PGOPTIONS`` 환경변수)는 libpq에
    ``-c search_path=other_schema`` 같은 접속 시점 GUC를 전달해 같은 호스트/
    포트/데이터베이스 안에서도 실제로 쓰는 스키마를 바꿀 수 있다. 스냅샷
    서명은 지금은 schema도 함께 기록해 대조하지만(``validate_snapshot_signature``),
    그 대조에만 기대면 수리와 롤백 시점의 스키마가 우연히 같아 서명은
    통과하되 둘 다 의도와 다른 릴레이션을 가리키는 경우까지는 못 막는다.
    ``options``는 임의의 GUC를 실을 수 있는 자유 형식 문자열이라
    ``search_path``만 골라 걸러내는 파싱은 새로운 우회 형태를 계속 놓칠
    위험이 있으므로, 이 도구는 서명 대조와 별개로 ``options``/``PGOPTIONS``
    자체를 접속 인자로 아예 허용하지 않는다.
    ``main()``과 ``repair()``가 이 판정을 공유해 CLI 경로와 직접 호출 경로가
    어긋나지 않게 한다.
    """
    if not engine.url.host:
        return (
            "cannot resolve a single host for this --pg-url (empty/ambiguous "
            "host: multi-host authority, PostgreSQL service config, or "
            "Unix-socket-via-query-parameter forms are not supported)"
        )
    if "," in engine.url.host:
        return (
            f"--pg-url authority names multiple hosts ({engine.url.host!r}); "
            "libpq may connect to any one of them, so a rollback-safe "
            "snapshot cannot be bound to a single server for this target"
        )
    unsafe = set(engine.url.query) - _SAFE_TARGET_QUERY_KEYS
    if unsafe:
        return (
            f"--pg-url query string sets {sorted(unsafe)}, which is not a "
            "known-safe parameter and may override the connect-time target "
            "(e.g. psycopg2's 'dsn' key merges host/hostaddr/service into "
            "the connection regardless of the authority); a rollback-safe "
            "snapshot cannot be bound to one server for this target"
        )
    env_blocked = sorted(
        key
        for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
        if os.environ.get(key)
    )
    if env_blocked:
        return (
            f"environment variable(s) {env_blocked} are set, which can override "
            "the connection target at connect time regardless of --pg-url; a "
            "rollback-safe snapshot cannot be bound to one server for this target"
        )
    if engine.url.port is None and os.environ.get("PGPORT"):
        return (
            "--pg-url omits an explicit port and the PGPORT environment "
            "variable is set, which can override the connect-time port; a "
            "rollback-safe snapshot cannot be bound to one server for this target"
        )
    return None


# ---------------------------------------------------------------------------
# 탐지 / 감사 (읽기 전용)
# ---------------------------------------------------------------------------


def detect(engine: Any, table: str) -> list[dict[str, Any]]:
    """오염 후보를 잠금 없이 보고한다(dry-run 경로). 아무것도 바꾸지 않는다."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(_detect_sql(table))).mappings().all()
    return [
        {
            "node_id": r["node_id"],
            "pack_id": r["pack_id"],
            "metadata": _as_dict_metadata(r["metadata"]),
        }
        for r in rows
    ]


def audit(engine: Any, table: str) -> list[dict[str, Any]]:
    """자동 복구 대상이 아닌 의심스러운 값(공백 전용, 대소문자 변형)을 정보용으로
    보고한다. 진짜 팩 이름과 구분할 근거가 없으므로 절대 자동 복구하지 않는다."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(_audit_sql(table))).mappings().all()
    return [{"pack_id": r["pack_id"], "n": r["n"]} for r in rows]


# ---------------------------------------------------------------------------
# 백업 파일 (원자적/크래시 안전 게시)
# ---------------------------------------------------------------------------


def write_backup_atomic(backup_to: str, snapshot: dict[str, Any]) -> tuple[int, int]:
    """임시 파일에 쓰고 fsync한 뒤 ``os.link()``로 배타 생성, 부모 디렉터리를
    fsync한다. ``os.link``는 대상이 이미 있으면 ``FileExistsError``를 내는
    원자적 단일 syscall이라 "존재하면 실패, 없으면 원자 생성"을 정확히
    보장한다(``os.replace``는 무조건 덮어써 이 성질을 주지 못한다).

    임시 파일 자체는 ``os.open()``을 ``O_CREAT | O_EXCL | O_NOFOLLOW``로
    호출해 만든다. 임시 파일 경로(``<backup>.tmp-<pid>``)는 pid로만 예측
    가능하므로, 다른 사용자가 쓸 수 있는 디렉터리라면 그 경로에 미리
    심볼릭 링크를 심어 둘 수 있다. ``O_EXCL``은 그 경로에 이미 무엇이
    있으면(파일이든 링크든) 생성 자체를 거부하고, ``O_NOFOLLOW``는 그 사이
    심긴 링크를 따라가지 않는다. 제한적인 ``umask``는 요청한 ``0o600``에서
    소유자 권한까지 제거할 수 있다. 열린 fd의 권한을 ``0o600``으로 확정해야
    스냅샷의 기밀성과 소유자의 롤백 읽기 권한을 함께 보장한다.

    ``os.open``이 지키는 것은 생성되는 그 순간뿐이다(이중 적대검증, 코덱스
    리뷰의 실측 재현). ``json.dump``/``fsync``가 끝나고 ``os.link``가
    실행되기까지의 창에서, unlink 권한이 있는 다른 사용자가 ``tmp`` 경로의
    파일을 지우고 자기 파일이나 심볼릭 링크로 바꿔치기하면, 경로만 보고
    거는 ``os.link(tmp, backup_to)``는 바꿔치기된 대상을 그대로 최종
    백업으로 게시한다. 문서상 ``os.link``의 ``follow_symlinks`` 기본값은
    ``True``다. 측정 플랫폼(Linux, ext4, Python 3.13.5)에서 심볼릭 링크 소스에
    건 세 호출의 결과는 다음과 같았다. 이전 코드 형태인 ``os.link(src, dst)``
    (기본 플래그, ``dir_fd`` 없음)와 현재 코드 형태인 ``os.link(src, dst,
    src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)``는 심볼릭 링크
    자체를 연결했다. 같은 호출에 ``follow_symlinks=True``를 명시하면 대상을
    연결했다. 그 밖의 인자 조합은 측정하지 않았다. 현재 코드는
    ``follow_symlinks=False``를 명시하며 기본값에 기대지 않는다.

    경로 대신 이미 연 fd 자체를 거는 ``os.link(f"/proc/self/fd/{fd}",
    backup_to)``(리눅스 매직 심볼릭 링크를 통한 게시)를 먼저 시도했으나,
    procfs와 대상이 같은 ext4 디바이스에 있는데도 ``EXDEV``로 실패함을
    실측 확인했다(``O_TMPFILE``로도 동일). 이 기법은 이식성이 없어 쓰지
    않는다.

    **부모 디렉터리 fd 고정(#363).** 이 함수는 부모 디렉터리를 한 번 열어
    그 fd로 모든 연산(``os.open``, ``os.link``, ``os.stat``, ``os.unlink``,
    ``os.fsync``)을 한다. 상위 경로가 나중에 다른 디렉터리로 바뀌어도 연산은
    처음 연 디렉터리 객체에 머문다. 경로로 ``unlink``하면 바뀐 상위 경로
    아래의 무관한 파일을 지운다(실측). fd 상대 ``unlink``는 그렇지 않다.

    **신뢰 모델.** 부모 디렉터리는 이 프로세스의 소유자 소유이고 다른
    사용자가 그 안에 쓸 수 없다고 가정한다. 이 가정 아래서는 고정한 디렉터리
    안의 항목을 다른 사용자가 바꿔치기할 수 없으므로 "inode 확인 뒤 상대
    ``unlink``"가 다른 파일을 지울 수 없다. 그 안에 쓸 수 있는 주체에게는
    inode 확인이 증명이 아니라 실패 시 닫힘 탐지일 뿐이다. 협력적 잠금은
    경로 소유권을 증명하지 않으며 이 함수는 잠금에 기대지 않는다.

    **실패별 결과.** 아래에서 "보존"은 지우지 않는다는 뜻이다.
      - 링크 전 실패: fd를 닫고, 우리 임시 파일임을 inode로 확인한 때만
        지운다. 확인하지 못하면 임시 파일을 보존하고 원래 예외에 그 이름을
        ``add_note``로 붙여 다시 낸다. 게시된 것은 없다.
      - 링크 뒤 확인(``os.stat``) 실패 또는 inode 불일치: 최종 파일과 임시
        파일을 모두 보존하고 ``BackupPublicationStateError``
        (``publication_state="unconfirmed"``)를 낸다. 확인하지 못한 최종
        파일을 이 함수는 지우지 않는다.
      - 확인 뒤 임시 파일 정리나 디렉터리 ``fsync`` 실패: 우리 최종 파일임을
        inode로 확인한 때만 지우고 원래 예외를 낸다(임시 파일이 남았으면 그
        이름을 노트로 붙인다). 최종 파일을 지우지 못하면 ``"published"``,
        다른 파일이면 ``"unconfirmed"`` 상태 오류를 낸다. 이미 없으면 지운
        것으로 본다.
    파일 fd는 링크 전에 닫으므로 게시 뒤에는 닫기 오류가 결과를 바꾸지
    못한다. 반환값은 게시한 파일의 ``(st_dev, st_ino)``이며 호출자가 나중에
    그 파일을 지울 때 신원 확인에 쓴다.

    **이슈 원문과의 차이.** 이슈 #363은 확인 실패 뒤 백업 파일이 남지 않기를
    요구했다. 소유자 결정 D3이 그것을 좁혔다. 확인하지 못한 최종 파일은 지우지
    않고 상태 오류로 보고한다.

    **재시도 계약.** 같은 경로의 자동 재시도 거부는 배타 ``os.link``와
    ``O_EXCL`` 임시 파일 생성으로만 강제한다. 최종 파일이나 같은 PID의 임시
    파일이 남아 있으면 재실행은 실패한다. 다른 PID에서는 다른 PID의 임시
    파일만 남은 경우 재실행이 통과할 수 있다. 영구 표식 파일은 없다. 이
    계약은 운영자 확인이며 지속적 강제가 아니다. SIGKILL은 임시 파일(링크
    전) 또는 최종 파일(링크 후)을 남기며 이 프로그램은 그것을 알아보지
    못한다. 운영자가 확인하고 지운다."""
    path = os.path.abspath(backup_to)
    directory, name = os.path.split(path)
    tmp_name = f"{name}.tmp-{os.getpid()}"
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _write_backup_in_directory(dir_fd, name, tmp_name, path, snapshot)
    finally:
        _quiet_close(dir_fd)


def _quiet_close(fd: int) -> None:
    """Close an fd whose close error carries no needed information."""
    try:
        os.close(fd)
    except OSError:
        pass


def _unlink_if_ours(dir_fd: int, name: str, ident: tuple[int, int]) -> bool:
    """Remove ``name`` inside ``dir_fd`` only if it is the file this process wrote.

    The function returns True when the entry is gone (removed now, or already absent). It
    returns False and deletes nothing when the entry has another identity. Any other stat or unlink
    error propagates. Every call is relative to the pinned directory fd, so a replaced
    ancestor path cannot redirect it. The identity check is a fail-closed detector for a
    writer inside the directory. The proof rests on the trust model in the module docstring.
    """
    try:
        found = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if (found.st_dev, found.st_ino) != ident:
        return False
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    return True


def _write_backup_in_directory(
    dir_fd: int, name: str, tmp_name: str, path: str, snapshot: dict[str, Any]
) -> tuple[int, int]:
    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    ident: tuple[int, int] | None = None
    open_fd: int | None = fd
    try:
        written = os.fstat(fd)
        ident = (written.st_dev, written.st_ino)
        # A restrictive umask can remove owner access from the requested mode.
        # Set the mode on this open descriptor before the file receives backup data.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fd)
        open_fd = None
        os.close(fd)
        try:
            os.link(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            raise FileExistsError(f"backup target already exists: {path}") from None
    except BaseException as exc:
        if open_fd is not None:
            _quiet_close(open_fd)
        _discard_temp_after_failure(dir_fd, tmp_name, ident, exc)
        raise

    # The final entry exists. Confirm it is the file this process wrote.
    try:
        published = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise BackupPublicationStateError(
            f"cannot confirm {path!r} after the link ({exc}); the final file and the temp file "
            f"{tmp_name!r} are preserved",
            publication_state="unconfirmed",
        ) from exc
    if (published.st_dev, published.st_ino) != ident:
        raise BackupPublicationStateError(
            f"backup publish race detected: {tmp_name!r} was replaced before it could be linked to "
            f"{path!r}; the final file and the temp file are preserved",
            publication_state="unconfirmed",
        )

    try:
        if not _unlink_if_ours(dir_fd, tmp_name, ident):
            raise OSError(f"temp file {tmp_name!r} is not the file this process wrote; it is preserved")
        os.fsync(dir_fd)
    except OSError as failure:
        _unpublish(dir_fd, name, tmp_name, path, ident, failure)
        raise
    return ident


def _discard_temp_after_failure(dir_fd: int, tmp_name: str, ident: tuple[int, int] | None, exc: BaseException) -> None:
    """Failure before the link. Remove the temp only when it is provably ours; else keep it and say so."""
    if ident is None:
        exc.add_note(f"temp file {tmp_name!r} preserved: its identity was never recorded")
        return
    try:
        if not _unlink_if_ours(dir_fd, tmp_name, ident):
            exc.add_note(f"temp file {tmp_name!r} preserved: it is not the file this process wrote")
    except OSError as cleanup:
        exc.add_note(f"temp file {tmp_name!r} preserved: cleanup failed ({cleanup})")


def _unpublish(
    dir_fd: int, name: str, tmp_name: str, path: str, ident: tuple[int, int], failure: OSError
) -> None:
    """The link is confirmed but a later step failed. Remove our final, or report what stays."""
    temp_note = _temp_note(tmp_name, _entry_state(dir_fd, tmp_name))
    try:
        removed = _unlink_if_ours(dir_fd, name, ident)
    except OSError as cause:
        raise BackupPublicationStateError(
            f"cannot remove {path!r} after: {failure}; the final file is preserved ({cause})"
            + (f"; {temp_note}" if temp_note else ""),
            publication_state="published",
        ) from failure
    if not removed:
        raise BackupPublicationStateError(
            f"{path!r} is not the file this process wrote after: {failure}; it is preserved"
            + (f"; {temp_note}" if temp_note else ""),
            publication_state="unconfirmed",
        ) from failure
    if temp_note:
        failure.add_note(f"final removed; {temp_note}")


def _entry_state(dir_fd: int, name: str) -> str:
    """"present", "absent" (only FileNotFoundError), or "unknown" (any other stat error)."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _temp_note(tmp_name: str, state: str) -> str:
    """Words for an operator about a temp file. Empty when the temp is gone."""
    if state == "present":
        return f"temp file {tmp_name!r} is preserved"
    if state == "unknown":
        return f"temp file {tmp_name!r} is preserved: its state could not be read"
    return ""


def _discard_published_backup(backup_to: str, ident: tuple[int, int]) -> bool:
    """Remove the backup published by this process. See ``_unlink_if_ours`` for the result.

    This function opens the parent directory fresh. The function preserves an entry when its
    device or inode differs from the recorded identity. It reports an absent entry as gone.
    """
    directory, name = os.path.split(os.path.abspath(backup_to))
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _unlink_if_ours(dir_fd, name, ident)
    finally:
        _quiet_close(dir_fd)


def load_snapshot(path: str) -> dict[str, Any]:
    """유효한 JSON이라도 모양이 다르면(수기 조작, 손상, 구버전) 여기서
    ``SnapshotError``로 거부한다. 그러지 않으면 최상위가 객체가 아니거나,
    ``rows`` 키가 아예 없거나 배열이 아니거나, 행에 문자열 ``node_id``나
    문자열 ``xmin``이 없을 때 ``AttributeError``/``TypeError``/``KeyError``/
    ``psycopg2.ProgrammingError``가 그대로 새어나가, 문서화된 백업-검증
    종료 코드(4) 대신 처리되지 않은 트레이스백이 된다. ``rows`` 키의
    부재까지 여기서 거부하는 이유는 ``snapshot.get("rows", [])``로 조용히
    빈 리스트를 대입하면 이 함수는 통과하지만 ``rollback()``이
    ``snapshot["rows"]``를 직접 인덱싱해 서명만 맞으면 그때 가서 같은 방식으로
    새어나가기 때문이다. ``node_id``와 ``xmin``을 문자열로 강제하는 이유도
    같다: 이 도구가 스스로 쓰는 값은 항상 문자열이므로(``_repair_sql``의
    ``xmin::text``), 리스트/객체 같은 값은 ``set(node_ids)``에서
    해시 불가 ``TypeError``를, psycopg2 바인드 시점에는
    ``ProgrammingError: can't adapt type``을 낸다.
    파일 자체가 비UTF-8 바이트를 담고 있거나(수기 조작, 부분 기록, 비트
    부패) 멀티바이트 문자 중간에서 잘려 있으면 ``open()``이나 ``json.load()``가
    ``UnicodeDecodeError``를 낸다. 이 예외는 ``UnicodeError``/``ValueError``의
    하위형이라 호출부가 잡는 ``(OSError, json.JSONDecodeError, SnapshotError)``
    어느 것에도 걸리지 않고 새어나가므로 여기서도 같은 방식으로 변환한다.
    같은 이유로 ``json.load()``가 낼 수 있는 다른 두 예외도 여기서 막는다:
    중첩 깊이가 큰 배열/객체는 파이썬 재귀 한계에 걸려 ``RecursionError``
    (``Exception``의 직계 하위형, ``ValueError``가 아니다)를, 자릿수가 큰
    정수 리터럴은 ``sys.set_int_max_str_digits`` 상한에 걸려 ``ValueError``를
    낸다. 이 도구 자신의 ``write_backup_atomic``은 이런 형태의 파일을
    만들지 않으므로, 두 경로 다 수기 조작이나 손상 스냅샷에서만 열린다."""
    try:
        with open(path, encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise SnapshotError(f"backup snapshot could not be parsed: {path}: {exc}") from None
    if not isinstance(snapshot, dict):
        raise SnapshotError(f"backup snapshot is not a JSON object: {path}")
    if "rows" not in snapshot or not isinstance(snapshot["rows"], list):
        raise SnapshotError(
            f"backup snapshot 'rows' is missing or not a JSON array: {path}"
        )
    node_ids = []
    for row in snapshot["rows"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("node_id"), str)
            or not isinstance(row.get("xmin"), str)
            or not _is_db_safe_text(row["node_id"])
            or not _is_db_safe_text(row["xmin"])
        ):
            raise SnapshotError(
                f"backup snapshot has a row with a missing, non-string, or "
                f"unencodable 'node_id'/'xmin': {path}"
            )
        node_ids.append(row["node_id"])
    if len(node_ids) != len(set(node_ids)):
        raise SnapshotError(f"backup snapshot has duplicate node_id entries: {path}")
    return snapshot


def validate_snapshot_signature(
    snapshot: dict[str, Any],
    table: str,
    database: str,
    host: str | None,
    port: int | None,
    schema: str | None,
) -> None:
    """table/database/host/port/schema 5가지 모두 일치해야 통과한다.

    포트는 현재 값만 생략-포트를 5432로 정규화한다(``port is None`` 검사,
    ``port or 5432``가 아님: 0 같은 값을 진실성으로 밀어 넣지 않는다). 스냅샷
    쪽 ``port``/``host``/``schema``에는 기본값을 주지 않는다 -- 정상 경로로
    만든 스냅샷은 항상 구체적인 host/port/schema를 갖고 있으므로, 키가 없는
    스냅샷(손상, 수기 조작, 구버전)은 ``None``이 되어 어떤 현재 값과도 일치할
    수 없다. 즉 ``None == None`` 우연 통과가 구조적으로 없다.

    ``schema``는 ``target_identity_reason``이 막는 host/port/database 결속과는
    다른 층위다: ``ALTER ROLE ... SET search_path`` / ``ALTER DATABASE ... SET
    search_path``는 서버 쪽 영구 설정이라 DSN이나 환경변수 어디에도 나타나지
    않는다(이중 적대검증, 코덱스 리뷰의 실측 재현). 수리와 롤백 사이에 이
    서버 쪽 설정이 바뀌면, table/database/host/port가 전부 일치해도 실제로는
    다른 스키마의 동명 테이블에 적용될 수 있다. 그래서 이 서명은 실제
    대상 테이블이 속한 스키마(호출자가 ``_resolve_table_schema``로 연결에서
    직접 조회해 넘긴다)를 별도로 기록하고 대조한다 -- ``current_schema()``의
    첫 매치가 아니라 ``to_regclass``로 ``_repair_sql``과 같은 ``search_path``
    해석 규칙을 써서 실제로 수리/롤백이 건드리는 릴레이션의 스키마를
    구하며, DSN을 파싱해서는 얻을 수 없는 값이기 때문에
    ``target_identity_reason``이 아니라 여기서 다룬다.
    """
    norm_port = 5432 if port is None else port
    snap_host = snapshot.get("host")
    snap_port = snapshot.get("port")
    snap_schema = snapshot.get("schema")
    if (
        snapshot.get("table") != table
        or snapshot.get("database") != database
        or not snap_host
        or snap_host != host
        or snap_port != norm_port
        or not snap_schema
        or snap_schema != schema
    ):
        raise SnapshotError(
            "backup signature mismatch: snapshot is for "
            f"table={snapshot.get('table')!r} database={snapshot.get('database')!r} "
            f"host={snap_host!r} port={snap_port!r} schema={snap_schema!r}, but "
            f"current target is table={table!r} database={database!r} host={host!r} "
            f"port={norm_port!r} schema={schema!r}"
        )


# ---------------------------------------------------------------------------
# 복구 (--apply, 백업 선행 후 커밋)
# ---------------------------------------------------------------------------


def _db_outcome(rollback_error: BaseException | None, commit_started: bool) -> str:
    if rollback_error is None:
        return "rolled back"
    if commit_started:
        return "unknown (commit started, rollback failed)"
    return "not committed (rollback failed; the open transaction ends with the connection)"


def _settle_failed_repair(
    exc: Exception,
    trans: Any,
    backup_to: str | None,
    published_ident: tuple[int, int] | None,
    commit_started: bool,
) -> Exception:
    """Roll back, decide the fate of a published backup, and return the exception to raise.

    The returned exception carries ``publication_state`` and ``db_outcome`` so the CLI can say
    what is true. A rollback failure never escapes this function. The rules apply in order:
    a. the writer already raised a state error: keep it and add the database outcome.
    b. the rollback failed: a state error is primary and names both facts.
    c. nothing was published: return the original error.
    d. the connection was lost: the commit result is unknown, so keep the backup.
    e. a definite failure: remove the backup only when it is provably ours.
    """
    rollback_error: BaseException | None = None
    try:
        trans.rollback()
    except Exception as failure:
        rollback_error = failure
    outcome = _db_outcome(rollback_error, commit_started)

    if isinstance(exc, BackupPublicationStateError):  # a
        exc.db_outcome = outcome
        return exc
    if rollback_error is not None:  # b
        detail = f"original error: {exc!r}; rollback error: {rollback_error!r}"
        notes = "; ".join(getattr(exc, "__notes__", []))
        if notes:
            detail += f"; notes: {notes}"
        if published_ident is None:
            state, detail = "not published", detail + "; no backup was published by this call"
        else:
            state, detail = "published", detail + f"; the backup at {backup_to!r} is preserved"
        return BackupPublicationStateError(detail, publication_state=state, db_outcome=outcome)
    if published_ident is None:  # c
        exc.publication_state, exc.db_outcome = "not published", outcome
        return exc
    if getattr(exc, "connection_invalidated", False):  # d
        exc.publication_state, exc.db_outcome = "published", "unknown (connection invalidated)"
        return exc
    try:  # e
        removed = _discard_published_backup(backup_to, published_ident)
    except OSError as cause:
        return BackupPublicationStateError(
            f"cannot remove the backup at {backup_to!r} after: {exc!r} ({cause}); it is preserved",
            publication_state="published", db_outcome=outcome)
    if not removed:
        return BackupPublicationStateError(
            f"the file at {backup_to!r} is not the one this process wrote after: {exc!r}; it is preserved",
            publication_state="unconfirmed", db_outcome=outcome)
    exc.publication_state, exc.db_outcome = "not published", outcome
    return exc


def repair(engine: Any, table: str, backup_to: str | None) -> list[dict[str, Any]]:
    """복구를 원자적으로 실행한다.

    순서: 트랜잭션 시작 -> 예상 건수 계산(잠금 없음) -> 원자 CTE 실행(FOR UPDATE
    + UPDATE + RETURNING, 한 문장) -> rowcount 대사(불일치 시 CountMismatchError,
    트랜잭션은 호출자가 예외를 받아 롤백) -> (backup_to가 있고 대상 행이 있으면)
    백업 파일을 원자적으로 게시(실패 시 예외, 트랜잭션 롤백) -> 커밋.

    즉 "DB 커밋 + 백업 유실" 조합이 구조적으로 불가능하다: backup_to가 있고
    대상 행이 있는 한 백업 게시 성공이 커밋의 전제조건이고, backup_to가
    있어도 대상 행이 0건이면 애초에 백업할 내용도 없다. backup_to가 없는
    (--skip-backup) 경로는 이 전제 자체가 걸리지 않는다.

    반대 방향("백업 게시 + DB 커밋 실패")과 롤백 실패는 ``_settle_failed_repair``가
    처리한다. 이 함수가 돌려주는 모든 예외는 ``publication_state``("published",
    "not published", "unconfirmed")와 ``db_outcome``을 단다.

    예외가 이 두 속성 없이 호출자에게 닿는 곳이 세 군데 있다. 트랜잭션 전의
    ``engine.connect()`` 호출과 ``conn.begin()`` 호출, 그리고 블록 뒤의 연결
    컨텍스트 종료(``__exit__``)다. 종료 오류는 정상 커밋 뒤에도 나므로 그때 DB는
    커밋된 상태다. 백업은 이 호출이 게시했을 때만 있다. ``repair()``는
    ``backup_to``가 있고 처리한 행이 있을 때만 백업을 쓴다. ``--skip-backup``이거나
    ``--backup-to``가 있어도 대상 행이 없으면 백업은 없다. 그래서 속성이 없는
    오류에서는 "백업이 게시됐다"고도 "롤백됐다"고도 말할 수 없다. CLI의 ``OSError``
    절은 속성이 없는 ``OSError``에 "publication state unknown"과 "database outcome
    unknown"만 출력한다. 다른 형의 예외(예: SQLAlchemy ``OperationalError``)는 그
    절을 거치지 않으며 이 모듈은 그에 대한 CLI 문구를 약속하지 않는다.

    이 이슈의 원문은 게시 뒤 확인 실패 시 백업 파일이 남지 않기를 요구했다. 소유자
    결정 D3이 그것을 좁혔다. 확인 실패 시 최종 파일을 보존하고 명시적 상태 오류를
    낸다.

      - 롤백이 실패하면 그 사실이 원래 오류를 가리지 않는다.
        ``BackupPublicationStateError``가 주 오류이고 게시 상태와 DB 결과를
        함께 적는다. 커밋을 시작한 뒤의 롤백 실패는 DB 결과를 "unknown"으로 쓴다.
      - 백업이 게시되지 않았으면 원래 오류를 그대로 낸다.
      - 연결 유실(``connection_invalidated``)이면 서버가 실제로 커밋했는지
        알 수 없으므로 백업을 지우지 않고 보존한다. 지우면 실제로는 성공한
        수리의 유일한 복구 수단을 잃을 수 있다.
      - 확정 실패이면 우리 백업임을 inode로 확인한 때만 지운다. 지우지
        못하거나 다른 파일이면 보존하고 상태 오류를 낸다.
    백업을 보존한 상태에서는 같은 경로를 자동으로 재시도하지 않고 운영자가
    확인한다(``write_backup_atomic``의 재시도 계약).
    """
    from sqlalchemy import text

    published_ident: tuple[int, int] | None = None
    commit_started = False
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            expected = conn.execute(text(_count_sql(table))).scalar()
            rows = conn.execute(text(_repair_sql(table))).mappings().all()
            if len(rows) != expected:
                raise CountMismatchError(
                    f"expected {expected} rows to repair, UPDATE affected {len(rows)}"
                )
            result_rows = [
                {
                    "node_id": r["node_id"],
                    "prior_pack_id": r["prior_pack_id"],
                    "metadata": _as_dict_metadata(r["metadata"]),
                    "xmin": r["xmin"],
                }
                for r in rows
            ]
            if backup_to and result_rows:
                reason = target_identity_reason(engine)
                if reason:
                    raise SnapshotError(f"cannot create a rollback-safe snapshot: {reason}")
                database = conn.execute(text("SELECT current_database()")).scalar()
                schema = _resolve_table_schema(conn, table)
                if schema is None:
                    raise SnapshotError(
                        f"cannot resolve schema for table {table!r} via to_regclass"
                    )
                host = engine.url.host
                port = engine.url.port if engine.url.port is not None else 5432
                snapshot = {
                    "table": table,
                    "database": database,
                    "host": host,
                    "port": port,
                    "schema": schema,
                    "rows": result_rows,
                }
                published_ident = write_backup_atomic(backup_to, snapshot)
            commit_started = True
            trans.commit()
        except Exception as exc:
            raise _settle_failed_repair(exc, trans, backup_to, published_ident, commit_started) from exc
    return result_rows


# ---------------------------------------------------------------------------
# 롤백 (--rollback-from, xmin 지문 대조)
# ---------------------------------------------------------------------------

ROLLBACK_STATUS_DONE = "rolled_back"
ROLLBACK_STATUS_XMIN_MISMATCH = "skipped_xmin_mismatch"
ROLLBACK_STATUS_DELETED = "skipped_deleted"


def rollback(engine: Any, table: str, snapshot: dict[str, Any]) -> dict[str, str]:
    """스냅샷의 각 행을 ``xmin`` 지문이 일치할 때만 되돌린다. 불일치/삭제된 행은
    조용히 건너뛰지 않고 상태를 개별 보고한다(호출자가 부분 처리를 확인하도록).

    서명 검사(database/schema 대사)와 실제 롤백 실행을 같은 연결의 같은
    트랜잭션 안에서 수행한다(이중 적대검증, 코덱스 리뷰의 실측 재현: 이전에는
    검사에 쓴 연결을 닫고 별도 연결로 롤백을 실행해, 그 사이 창에서 커넥션
    풀이 재연결되거나 대상 테이블이 교체돼도 검사와 실행이 서로 다른 대상을
    볼 수 있었다). 트랜잭션의 첫 문장으로 대상 테이블에 ACCESS SHARE 잠금을
    걸어, 검사 시점부터 커밋까지 그 이름이 가리키는 릴레이션이 바뀌지
    못하게 막는다(``LOCK TABLE``도 ``_repair_sql``과 같은 비한정 이름을 써서
    같은 ``search_path`` 해석 규칙을 그대로 따른다)."""
    from sqlalchemy import text

    statuses: dict[str, str] = {}
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text(f"LOCK TABLE {table} IN ACCESS SHARE MODE"))
            database = conn.execute(text("SELECT current_database()")).scalar()
            schema = _resolve_table_schema(conn, table)
            host = engine.url.host
            port = engine.url.port if engine.url.port is not None else 5432
            validate_snapshot_signature(snapshot, table, database, host, port, schema)
            for row in snapshot["rows"]:
                node_id = row["node_id"]
                res = conn.execute(
                    text(_rollback_row_sql(table)),
                    {"node_id": node_id, "xmin": row["xmin"]},
                ).fetchall()
                if res:
                    statuses[node_id] = ROLLBACK_STATUS_DONE
                    continue
                exists = conn.execute(
                    text(f"SELECT 1 FROM {table} WHERE node_id = :node_id"),
                    {"node_id": node_id},
                ).scalar()
                statuses[node_id] = (
                    ROLLBACK_STATUS_XMIN_MISMATCH if exists else ROLLBACK_STATUS_DELETED
                )
            trans.commit()
        except Exception:
            trans.rollback()
            raise
    return statuses


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pg-url", default=None, help="target PostgreSQL DSN (default: settings.postgres_url)")
    ap.add_argument("--table", default=None, help="pgvector table name (default: settings.embed_collection)")
    ap.add_argument("--apply", action="store_true", help="perform writes; without this the script is dry-run")
    ap.add_argument("--backup-to", default=None, help="write a JSON snapshot of affected rows here before --apply")
    ap.add_argument("--skip-backup", action="store_true", help="explicitly skip the mandatory pre-repair backup")
    ap.add_argument("--rollback-from", default=None, help="restore rows from a prior --backup-to snapshot (requires --apply)")
    args = ap.parse_args(argv)

    from opencrab.config import get_settings

    settings = get_settings()
    pg_url = args.pg_url or settings.postgres_url
    table = args.table or settings.embed_collection

    try:
        _check_ident(table, "--table")
    except ValueError as exc:
        print(f"! {exc}")
        return EXIT_USAGE

    try:
        from sqlalchemy.engine import make_url

        display_url = make_url(pg_url).render_as_string(hide_password=True)
    except Exception:
        display_url = "<unparseable --pg-url>"
    print(f"# pg-url : {display_url}")
    print(f"# table  : {table}")

    try:
        engine = connect(pg_url)
        with engine.connect():
            pass
    except Exception as exc:
        print(f"! could not connect: {exc}")
        return EXIT_PRECONDITION

    identity_reason = target_identity_reason(engine)

    if args.rollback_from:
        if not args.apply:
            print("! --rollback-from requires --apply.")
            return EXIT_USAGE
        if identity_reason:
            print(f"! {identity_reason}")
            print(
                "! refusing --rollback-from: a snapshot cannot be safely bound "
                "to this target, so its signature check would compare against "
                "the wrong server (no --skip-backup escape for rollback: the "
                "binding is not optional here)."
            )
            return EXIT_PRECONDITION
        try:
            snapshot = load_snapshot(args.rollback_from)
        except (OSError, json.JSONDecodeError, SnapshotError) as exc:
            print(f"! could not load snapshot: {exc}")
            return EXIT_BACKUP

        print(
            "! WARNING: rollback matches rows via PostgreSQL 'xmin', which "
            "wraps around (32-bit transaction ids) and is rewritten by VACUUM "
            "FREEZE. This tool assumes the repair-to-rollback window is short "
            "(run promptly). Rollback of an OLD snapshot (days/weeks later) is "
            "NOT guaranteed safe.",
            file=sys.stderr,
        )

        # 서명 검사(database/schema 대사)는 rollback() 안에서, 실제 롤백을
        # 실행하는 것과 같은 연결/트랜잭션으로 수행한다(이중 적대검증, 코덱스
        # 리뷰의 실측 재현: 검사와 실행이 서로 다른 연결이면 그 사이 창에서
        # 풀 재연결이나 대상 교체가 검사를 무의미하게 만들 수 있었다).
        try:
            statuses = rollback(engine, table, snapshot)
        except SnapshotError as exc:
            print(f"! {exc}")
            return EXIT_BACKUP
        done = sum(1 for s in statuses.values() if s == ROLLBACK_STATUS_DONE)
        total = len(statuses)
        for node_id, status in statuses.items():
            print(f"#   {node_id}: {status}")
        print(f"# rolled back {done}/{total} rows.")
        if done != total:
            print(
                f"! {total - done} row(s) were NOT rolled back (see per-row "
                "status above) -- do not assume the rollback is complete."
            )
            print("RESULT: FAIL (partial rollback)")
            return EXIT_COUNT_MISMATCH
        print("RESULT: PASS")
        return EXIT_OK

    detected = detect(engine, table)
    audited = audit(engine, table)
    print(f"# contaminated rows detected: {len(detected)}")
    for row in detected[:20]:
        print(f"#   {row['node_id']}")
    if len(detected) > 20:
        print(f"#   ... and {len(detected) - 20} more")
    if audited:
        print("# audit (informational only, NOT auto-repaired):")
        for entry in audited:
            print(f"#   pack_id={entry['pack_id']!r}: {entry['n']} row(s)")

    if not args.apply:
        print("# dry-run: no writes.")
        print("RESULT: PASS (dry-run)")
        return EXIT_OK

    if args.backup_to and args.skip_backup:
        print("! --backup-to and --skip-backup are mutually exclusive.")
        return EXIT_USAGE

    if not args.backup_to and not args.skip_backup:
        print("! --apply requires --backup-to <path> (or explicit --skip-backup).")
        return EXIT_USAGE

    if identity_reason:
        print(f"! {identity_reason}")
        if not args.skip_backup:
            print(
                "! refusing --apply: a rollback-safe snapshot cannot be created "
                "for this target (pass --skip-backup to proceed without one)."
            )
            return EXIT_PRECONDITION
        print("! proceeding without a snapshot: --skip-backup was given explicitly.")

    try:
        result_rows = repair(engine, table, args.backup_to)
    except CountMismatchError as exc:
        print(f"! {exc}")
        return EXIT_COUNT_MISMATCH
    except BackupPublicationStateError as exc:
        print(f"! {exc}")
        for note in getattr(exc, "__notes__", []):
            print(f"! note: {note}")
        print("! manual investigation is required. The same backup path must not be retried automatically.")
        return EXIT_BACKUP
    except (OSError, FileExistsError) as exc:
        state = getattr(exc, "publication_state", None)
        # An error that carries a publication state passed through the repair settle step and may come
        # from the commit, not from the backup write.
        print(f"! {'repair failed' if state else 'backup write failed'}: {exc}")
        for note in getattr(exc, "__notes__", []):
            print(f"! note: {note}")
        outcome = getattr(exc, "db_outcome", None)
        print(f"! publication state: {state or 'publication state unknown'}")
        print(f"! database outcome: {outcome or 'database outcome unknown'}")
        if outcome == "rolled back":
            print("! repair rolled back")
        if state == "published":
            print("! the backup is preserved")
        return EXIT_BACKUP
    except SnapshotError as exc:
        print(f"! {exc}")
        return EXIT_BACKUP

    print(f"# repaired {len(result_rows)} row(s).")
    if args.backup_to and result_rows:
        print(f"# backup written -> {args.backup_to}")
    print("RESULT: PASS")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
