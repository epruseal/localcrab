"""``scripts/check_fk_orphans.py`` 의 종료 코드 계약 (#181).

이 스크립트는 ``PRAGMA foreign_keys=ON`` 을 전역으로 걸기 전, 읽기 전용
사전 점검으로 기존 SQLite 파일의 FK 위반 여부를 진단한다. 종료 코드 셋
(0=위반 없음, 1=위반 발견, 2=파일을 열 수 없음)이 실제 동작과 어긋나면
자동화가 오탐/오분류한다.

코드 리뷰(#337)에서 지적된 결함: 손상되었거나 SQLite 형식이 아닌 파일은
``sqlite3.OperationalError`` 가 아니라 그 기반 클래스인
``sqlite3.DatabaseError`` 를 직접 던진다(실측: "file is not a database").
좁은 ``except OperationalError`` 는 이 경우를 못 잡아 진단 오류(2) 대신
되잡히지 않은 예외로 프로세스가 죽는다 -- 이 파일의
``test_corrupt_non_sqlite_file_is_diagnostic_error`` 가 그 회귀를 고정한다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import check_fk_orphans as cli  # noqa: E402


def _make_clean_db(path: Path) -> None:
    """부모 없이는 자식이 존재할 수 없는, FK 로 깨끗한 DB 를 만든다."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))"
        )
        conn.execute("INSERT INTO parent VALUES ('p1')")
        conn.execute("INSERT INTO child VALUES ('c1', 'p1')")


def _make_orphaned_db(path: Path) -> None:
    """자식 행이 존재하지 않는 부모를 가리키는, FK 위반이 있는 DB 를 만든다."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))"
        )
        # FK 를 걸지 않은 커넥션이라 이 삽입 자체는 통과한다 -- 바로 이 상태를
        # 사전 점검이 잡아내야 한다.
        conn.execute("INSERT INTO child VALUES ('c1', 'missing-parent')")


def test_clean_db_reports_zero_violations(tmp_path: Path) -> None:
    db_path = tmp_path / "clean.db"
    _make_clean_db(db_path)

    assert cli.check(str(db_path)) == []


def test_orphaned_db_reports_the_violating_row(tmp_path: Path) -> None:
    db_path = tmp_path / "orphaned.db"
    _make_orphaned_db(db_path)

    violations = cli.check(str(db_path))
    assert len(violations) == 1
    table, _rowid, parent_table, _fk_id = violations[0]
    assert (table, parent_table) == ("child", "parent")


def test_main_exit_code_0_for_clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "clean.db"
    _make_clean_db(db_path)
    monkeypatch.setattr(sys, "argv", ["check_fk_orphans.py", str(db_path)])

    assert cli.main() == 0


def test_main_exit_code_1_for_orphaned_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "orphaned.db"
    _make_orphaned_db(db_path)
    monkeypatch.setattr(sys, "argv", ["check_fk_orphans.py", str(db_path)])

    assert cli.main() == 1


def test_main_exit_code_2_for_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(sys, "argv", ["check_fk_orphans.py", str(missing)])

    assert cli.main() == 2


def test_corrupt_non_sqlite_file_is_diagnostic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """손상/비-SQLite 파일은 진단 오류(2) 여야지, 되잡히지 않은 예외로 죽으면 안 된다."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database file, just garbage bytes")
    monkeypatch.setattr(sys, "argv", ["check_fk_orphans.py", str(corrupt)])

    assert cli.main() == 2
