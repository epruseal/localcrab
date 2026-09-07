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
  - 스냅샷은 ``table``/``database``/``host``/``port`` 네 값 전부가 현재 대상과
    일치할 때만 유효하다(``validate_snapshot_signature``). 대상 서버를 하나로
    특정할 수 없는 ``--pg-url``(다중 호스트, PostgreSQL ``service`` 설정,
    소켓-경유-쿼리스트링 등 -- ``target_identity_reason`` 참고)이면 ``--apply``
    자체를 거부한다(코드 3, DB 쓰기 0건). ``--skip-backup``을 명시했을 때만
    같은 사유를 정보성으로 출력하고 스냅샷 없이 수리를 진행한다(운영자가
    직접 롤백을 포기한 경로이므로).

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

  백업 서명(``table``/``database``/``host``/``port``)이 현재 대상과 다르면
  즉시 거부한다(코드 4). 대상 서버를 하나로 특정할 수 없는 ``--pg-url``이면
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

    return create_engine(pg_url, pool_pre_ping=True, hide_parameters=True)


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
        "options",
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
        key for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE") if os.environ.get(key)
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


def write_backup_atomic(backup_to: str, snapshot: dict[str, Any]) -> None:
    """임시 파일에 쓰고 fsync한 뒤 ``os.link()``로 배타 생성, 부모 디렉터리를
    fsync한다. ``os.link``는 대상이 이미 있으면 ``FileExistsError``를 내는
    원자적 단일 syscall이라 "존재하면 실패, 없으면 원자 생성"을 정확히
    보장한다(``os.replace``는 무조건 덮어써 이 성질을 주지 못한다)."""
    tmp = f"{backup_to}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(tmp, backup_to)
    except FileExistsError:
        raise FileExistsError(f"backup target already exists: {backup_to}") from None
    finally:
        os.unlink(tmp)
    dir_path = os.path.dirname(os.path.abspath(backup_to)) or "."
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
) -> None:
    """table/database/host/port 4가지 모두 일치해야 통과한다.

    포트는 현재 값만 생략-포트를 5432로 정규화한다(``port is None`` 검사,
    ``port or 5432``가 아님: 0 같은 값을 진실성으로 밀어 넣지 않는다). 스냅샷
    쪽 ``port``/``host``에는 기본값을 주지 않는다 -- 정상 경로로 만든 스냅샷은
    항상 구체적인 host/port를 갖고 있으므로, 키가 없는 스냅샷(손상·수기
    조작·구버전)은 ``None``이 되어 어떤 현재 값과도 일치할 수 없다. 즉
    ``None == None`` 우연 통과가 구조적으로 없다.
    """
    norm_port = 5432 if port is None else port
    snap_host = snapshot.get("host")
    snap_port = snapshot.get("port")
    if (
        snapshot.get("table") != table
        or snapshot.get("database") != database
        or not snap_host
        or snap_host != host
        or snap_port != norm_port
    ):
        raise SnapshotError(
            "backup signature mismatch: snapshot is for "
            f"table={snapshot.get('table')!r} database={snapshot.get('database')!r} "
            f"host={snap_host!r} port={snap_port!r}, but current target is "
            f"table={table!r} database={database!r} host={host!r} port={norm_port!r}"
        )


# ---------------------------------------------------------------------------
# 복구 (--apply, 백업 선행 후 커밋)
# ---------------------------------------------------------------------------


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
    """
    from sqlalchemy import text

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
                host = engine.url.host
                port = engine.url.port if engine.url.port is not None else 5432
                snapshot = {
                    "table": table,
                    "database": database,
                    "host": host,
                    "port": port,
                    "rows": result_rows,
                }
                write_backup_atomic(backup_to, snapshot)
            trans.commit()
        except Exception:
            trans.rollback()
            raise
    return result_rows


# ---------------------------------------------------------------------------
# 롤백 (--rollback-from, xmin 지문 대조)
# ---------------------------------------------------------------------------

ROLLBACK_STATUS_DONE = "rolled_back"
ROLLBACK_STATUS_XMIN_MISMATCH = "skipped_xmin_mismatch"
ROLLBACK_STATUS_DELETED = "skipped_deleted"


def rollback(engine: Any, table: str, snapshot: dict[str, Any]) -> dict[str, str]:
    """스냅샷의 각 행을 ``xmin`` 지문이 일치할 때만 되돌린다. 불일치/삭제된 행은
    조용히 건너뛰지 않고 상태를 개별 보고한다(호출자가 부분 처리를 확인하도록)."""
    from sqlalchemy import text

    statuses: dict[str, str] = {}
    with engine.connect() as conn:
        trans = conn.begin()
        try:
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

    host = engine.url.host
    port = engine.url.port if engine.url.port is not None else 5432
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
        from sqlalchemy import text

        with engine.connect() as conn:
            database = conn.execute(text("SELECT current_database()")).scalar()
        try:
            validate_snapshot_signature(snapshot, table, database, host, port)
        except SnapshotError as exc:
            print(f"! {exc}")
            return EXIT_BACKUP

        print(
            "! WARNING: rollback matches rows via PostgreSQL 'xmin', which "
            "wraps around (32-bit transaction ids) and is rewritten by VACUUM "
            "FREEZE. This tool assumes the repair-to-rollback window is short "
            "(run promptly). Rollback of an OLD snapshot (days/weeks later) is "
            "NOT guaranteed safe.",
            file=sys.stderr,
        )

        statuses = rollback(engine, table, snapshot)
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
    except (OSError, FileExistsError) as exc:
        print(f"! backup write failed, repair rolled back: {exc}")
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
