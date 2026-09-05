#!/usr/bin/env python3
"""읽기 전용 사전 점검: SQLite opencrab.db 에 FK 위반(고아 행)이 있는지 검사한다 (#181).

이 스크립트는 파일을 절대 변경하지 않는다(모든 쿼리는 SELECT). ``PRAGMA
foreign_keys=ON`` 을 전역으로 걸기 전에, 이미 들어간 데이터가 그 강제를
통과하는지 미리 확인하는 것이 이 스크립트의 유일한 목적이다.

사용법:
    python check_fk_orphans.py /path/to/opencrab.db

종료 코드:
    0  고아 행 없음 (FK 강제를 걸어도 기존 데이터가 안전하다)
    1  고아 행 발견 (아래 목록을 보고 정리한 뒤에만 FK 강제를 켤 것)
    2  파일을 열 수 없음 (경로 오류 등)

이 스크립트는 SQLite 자체 진단 프래그마(``PRAGMA foreign_key_check``)를
직접 쓴다 -- FK 를 켜지 않고도(읽기 전용 커넥션) SQLite 가 이미 알고 있는
위반 목록을 그대로 돌려준다. 별도로 손으로 짠 조인 쿼리를 유지할 필요가
없다.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import urllib.parse


def check(db_path: str) -> list[tuple]:
    """``db_path`` 의 FK 위반 행을 전부 반환한다. 읽기 전용 커넥션만 연다."""
    # ``db_path`` 에 URI 메타문자(``?``, ``#`` 등)가 있으면 percent-encode 없이는
    # SQLite 가 그 뒤를 쿼리 파라미터로 파싱해 완전히 다른 파일을 연다(리뷰
    # 지적, PR #337 -- 실측: "real?name.db" 를 그대로 붙이면 "real" 이 열리고
    # mode=ro 인식도 깨져 쓰기까지 통과했다). 절대 경로로 정규화한 뒤
    # percent-encode 해서 경로 구분자(``/``)만 그대로 두고 나머지를 이스케이프한다.
    encoded = urllib.parse.quote(os.path.abspath(db_path))
    uri = f"file:{encoded}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        # PRAGMA foreign_key_check 는 이 커넥션의 foreign_keys 설정과
        # 무관하게 항상 현재 데이터의 위반을 진단한다(SQLite 문서 확인됨) --
        # 이 커넥션에서 pragma foreign_keys=ON 을 걸 필요가 없다(읽기 전용
        # 모드에서는 어차피 그 설정이 쓰기 검사에 영향을 주지 않는다).
        cur = conn.execute("PRAGMA foreign_key_check")
        return cur.fetchall()
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-opencrab.db>", file=sys.stderr)
        return 2
    db_path = sys.argv[1]
    try:
        violations = check(db_path)
    except sqlite3.DatabaseError as exc:
        # sqlite3.OperationalError (경로 오류, 권한 부족 등) 는 이 클래스의
        # 하위 클래스라 그대로 잡힌다. 손상되었거나 SQLite 형식이 아닌
        # 파일은 OperationalError 가 아니라 이 기반 클래스로 직접 올라오므로
        # (실측: "file is not a database"), 좁게 잡으면 그 경우가 여기를
        # 빠져나가 진단 오류(2) 대신 되잡히지 않은 예외로 죽는다.
        print(f"cannot open {db_path!r} read-only: {exc}", file=sys.stderr)
        return 2

    if not violations:
        print(f"{db_path}: FK 위반 0건 -- foreign_keys=ON 을 걸어도 안전하다.")
        return 0

    print(f"{db_path}: FK 위반 {len(violations)}건 발견 -- 아래 행을 정리한 뒤에만 켤 것.")
    # PRAGMA foreign_key_check 각 행: (table, rowid, parent_table, fk_id)
    for table, rowid, parent_table, fk_id in violations:
        print(f"  table={table} rowid={rowid} parent_table={parent_table} fk_id={fk_id}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
