"""#671 B6: 단일 모드 백업(backup_single_root)과 CLI 분기 시험.

설계 5판 9절의 T1~T16(T11b 포함)이 정본이다. 합성 root는 tests/test_factory_single_file.py의
보조 함수를 재사용한다. 모든 시험은 합성 데이터만 쓰고 라이브 경로를 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_factory_single_file import (  # noqa: F401  (_fresh_single_registry는 autouse 고정물이다)
    _authorized_and_pointed,
    _exclusive_lock_attempt,
    _names,
    _plain_table_counts,
    _single_env,
    _single_settings,
    _write_ledger_state,
)
from tests.test_factory_single_file import (
    _fresh_single_registry as _isolate_single_registry,  # noqa: F401
)

_D6_PHRASE = "삭제 저널 단위(B5)가 준비되지 않은"
_MISMATCH_PHRASE = "data-dir이 설정의 단일 root와 다르다"


@pytest.fixture(autouse=True)
def _no_failed_shared_files():
    """H2: 앞선 시험의 실패 잔재가 뒤 시험의 거부를 가리지 않게 한다."""
    from opencrab.stores import _single_file as sf

    assert dict(sf._failed_shared_files) == {}
    yield


def _set_dirs(dest: Path) -> list[Path]:
    return sorted(p for p in dest.iterdir() if p.name.startswith("backup."))


def _staging_dirs(dest: Path) -> list[Path]:
    return sorted(p for p in dest.iterdir() if p.name.startswith(".backup-staging."))


def _backup(built, dest: Path, **kwargs):
    from opencrab.stores import backup as bk

    dest.mkdir(exist_ok=True)
    return bk.backup_single_root(built.root, dest, settings=_single_settings(built.root), **kwargs)


def _leave_wal_frames(root: Path, rows: int = 500) -> None:
    """크래시 모사: wal_autocheckpoint=0으로 커밋한 뒤 연결을 닫지 않고 프로세스가 끝난다(-wal에 프레임이 남는다)."""
    code = (
        "import sqlite3, sys, os\n"
        "c = sqlite3.connect(sys.argv[1] + '/single.db', isolation_level=None)\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute('CREATE TABLE zz_crash(a INTEGER)')\n"
        "c.execute('BEGIN')\n"
        f"c.executemany('INSERT INTO zz_crash VALUES (?)', [(i,) for i in range({rows})])\n"
        "c.execute('COMMIT')\n"
        "os._exit(0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _set_counts(set_dir: Path, name: str) -> dict[str, int]:
    return _plain_table_counts(set_dir / name)


def test_t1_backup_set_names_and_block(tmp_path, monkeypatch):
    """T1: 세트 이름 집합과 매니페스트 블록, verify_backup_set 통과."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    outcome = _backup(built, tmp_path / "bk")
    set_dir = outcome.set_dir
    assert sorted(p.name for p in set_dir.iterdir()) == ["MANIFEST.json", "billing.db", "single.db"]
    manifest = json.loads((set_dir / "MANIFEST.json").read_text())
    block = manifest["single_db_recovery"]
    assert sorted(block) == ["backup_id", "billing", "database", "journals", "operations"]
    assert len(block["backup_id"]) == 32 and all(ch in "0123456789abcdef" for ch in block["backup_id"])
    assert block["database"] == "single.db" and block["billing"] == "billing.db"
    assert block["journals"] == {} and block["operations"] == []
    report = bk.verify_backup_set(set_dir)
    assert report["files_verified"] == 2


def test_t2_backup_data_dir_stays_refused(tmp_path, monkeypatch):
    """T2: backup_data_dir의 단일 거부는 그대로다(T-E8 a와 같은 단언)."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    before = _names(built.root)
    with pytest.raises(bk.BackupError, match="단일 SQLite"):
        bk.backup_data_dir(built.root, settings=_single_settings(built.root))
    assert _names(built.root) == before


def test_t3_table_row_counts_equal(tmp_path, monkeypatch):
    """T3: 세트 두 파일의 일반 표 행 수가 root와 같다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    single_before = _plain_table_counts(built.root / "single.db")
    billing_before = _plain_table_counts(built.root / "billing.db")
    outcome = _backup(built, tmp_path / "bk")
    assert _set_counts(outcome.set_dir, "single.db") == single_before
    assert _set_counts(outcome.set_dir, "billing.db") == billing_before


def test_t4_crash_state_frames_are_in_the_set(tmp_path, monkeypatch):
    """T4: 체크포인트 안 된 -wal의 행이 세트 사본에 있고 세트 안에 곁파일이 없다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _leave_wal_frames(built.root)
    assert (built.root / "single.db-wal").stat().st_size > 0
    outcome = _backup(built, tmp_path / "bk")
    assert _set_counts(outcome.set_dir, "single.db").get("zz_crash") == 500
    assert sorted(p.name for p in outcome.set_dir.iterdir()) == ["MANIFEST.json", "billing.db", "single.db"]


def test_t5_set_copy_is_wal_file(tmp_path, monkeypatch):
    """T5: 세트 사본 헤더의 18, 19번 바이트가 2다(복원한 파일이 WAL로 열린다)."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    outcome = _backup(built, tmp_path / "bk")
    header = (outcome.set_dir / "single.db").read_bytes()[:100]
    assert (header[18], header[19]) == (2, 2)


def _insert_delete_operation(root: Path, pack_id: str) -> None:
    conn = sqlite3.connect(str(root / "single.db"))
    try:
        conn.execute(
            "INSERT INTO pack_delete_operations (operation_id, pack_id, incarnation, state, result_json, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a" * 32, pack_id, "b" * 32, "pending", None, None),
        )
        conn.commit()
    finally:
        conn.close()


def _assert_d6_refusal(exc: BaseException) -> None:
    message = str(exc.value)
    assert _D6_PHRASE in message, message
    assert "operation list differs" not in message and "operations differs" not in message, message


@pytest.mark.parametrize("kind", ["rows", "journal_file"])
def test_t6_delete_traces_refuse_with_d6_phrase(tmp_path, monkeypatch, kind):
    """T6(a, b): 삭제 흔적이 있으면 D6 문구로 거부하고 세트와 staging이 남지 않는다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    if kind == "rows":
        _insert_delete_operation(built.root, built.pack_id)
    else:
        journals = built.root / "delete-pack-journals"
        journals.mkdir()
        (journals / "x.json").write_text("{}")
    dest = tmp_path / "bk"
    with pytest.raises(bk.BackupError) as exc:
        _backup(built, dest)
    _assert_d6_refusal(exc)
    assert _set_dirs(dest) == [] and _staging_dirs(dest) == []


def test_t6c_empty_journal_dir_passes(tmp_path, monkeypatch):
    """T6(c): 빈 delete-pack-journals 디렉터리는 통과한다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    (built.root / "delete-pack-journals").mkdir()
    outcome = _backup(built, tmp_path / "bk")
    assert outcome.set_dir.is_dir()


def test_t7_unauthorized_root_refused_without_writes(tmp_path, monkeypatch):
    """T7: 대장이 ready이면 BackupError로 거부하고 root에 아무것도 만들지 않는다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _write_ledger_state(built.root, "ready")
    before = _names(built.root)
    with pytest.raises(bk.BackupError):
        _backup(built, tmp_path / "bk")
    assert _names(built.root) == before
    assert "write.lock" not in before


def test_t8_destination_inside_root_refused(tmp_path, monkeypatch):
    """T8: root 안이나 root 아래를 목적지로 주면 거부한다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    inside = built.root / "bk"
    inside.mkdir()
    before = _names(built.root)
    with pytest.raises(bk.BackupError):
        bk.backup_single_root(built.root, inside, settings=_single_settings(built.root))
    with pytest.raises(bk.BackupError):
        bk.backup_single_root(built.root, built.root, settings=_single_settings(built.root))
    assert _names(built.root) == before


def test_t9_cli_without_single_setting_refuses_single_root(tmp_path, monkeypatch, capsys):
    """T9: SINGLE_SQLITE_FILE 없이 단일 root를 받는 CLI는 B11로 거부한다."""
    from opencrab.config import get_settings
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    get_settings.cache_clear()
    before = _names(built.root)
    dest = tmp_path / "bk"
    dest.mkdir()
    rc = bk._main(["--data-dir", str(built.root), "--to", str(dest)])
    assert rc == 1
    assert _names(built.root) == before


def test_t10_lifetime_lock_blocks_restore_during_backup(tmp_path, monkeypatch):
    """T10: 백업 진행 중 독점 수명 잠금 시도는 거부되고 백업 뒤에는 성공한다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    seen: list[int] = []
    real = bk._run_targets

    def spy(*args, **kwargs):
        seen.append(_exclusive_lock_attempt(built.root).returncode)
        return real(*args, **kwargs)

    monkeypatch.setattr(bk, "_run_targets", spy)
    _backup(built, tmp_path / "bk")
    assert seen and all(rc != 0 for rc in seen), seen
    assert _exclusive_lock_attempt(built.root).returncode == 0


def test_t11_cli_single_branch(tmp_path, monkeypatch, capsys):
    """T11: 단일 설정의 CLI. 정상, --exclude 거부, data-dir 불일치 거부, --list, --json."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    dest = tmp_path / "bk"
    dest.mkdir()

    assert bk._main(["--to", str(dest)]) == 0
    assert len(_set_dirs(dest)) == 1
    capsys.readouterr()

    assert bk._main(["--to", str(dest), "--exclude", "billing.db"]) == 1
    assert "--exclude" in capsys.readouterr().err
    assert len(_set_dirs(dest)) == 1

    other = tmp_path / "other"
    other.mkdir()
    assert bk._main(["--data-dir", str(other), "--to", str(dest)]) == 1
    assert _MISMATCH_PHRASE in capsys.readouterr().err

    assert bk._main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "single.db" in listing and "billing.db" in listing and "graph.db" not in listing

    assert bk._main(["--to", str(dest), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["set_dir"]).is_dir() and "single_db_recovery" in payload


def test_t11b_function_checks_data_dir(tmp_path, monkeypatch):
    """T11b: 함수 직접 호출도 같은 문구로 거부하고 그 디렉터리에 write.lock이 생기지 않는다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    dest = tmp_path / "bk"
    dest.mkdir()
    with pytest.raises(bk.BackupError, match=_MISMATCH_PHRASE):
        bk.backup_single_root(other, dest, settings=_single_settings(built.root))
    assert os.listdir(other) == []


def test_t12_backup_ids_differ(tmp_path, monkeypatch):
    """T12: 두 번 백업하면 backup_id가 서로 다르고 database와 billing 이름이 다르다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    first = _backup(built, tmp_path / "bk").manifest["single_db_recovery"]
    second = _backup(built, tmp_path / "bk").manifest["single_db_recovery"]
    assert first["backup_id"] != second["backup_id"]
    assert first["database"] != first["billing"]


def test_t13_backup_does_not_checkpoint_the_live_file(tmp_path, monkeypatch):
    """T13: -wal 크기 > 0 전제 아래 백업이 root의 single.db 바이트와 -wal 크기를 바꾸지 않는다."""
    import hashlib

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    live = sqlite3.connect(str(built.root / "single.db"))
    try:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE zz_t13(a INTEGER)")
        live.executemany("INSERT INTO zz_t13 VALUES (?)", [(i,) for i in range(2000)])
        live.commit()
        wal = built.root / "single.db-wal"
        assert wal.stat().st_size > 0
        before_sha = hashlib.sha256((built.root / "single.db").read_bytes()).hexdigest()
        before_wal = wal.stat().st_size
        before_names = set(os.listdir(built.root))
        outcome = _backup(built, tmp_path / "bk")
        assert hashlib.sha256((built.root / "single.db").read_bytes()).hexdigest() == before_sha
        assert wal.stat().st_size == before_wal
        allowed = {"single.db-wal", "single.db-shm", "write.lock", "billing.db.lock"}
        assert set(os.listdir(built.root)) - before_names <= allowed
        assert _set_counts(outcome.set_dir, "single.db").get("zz_t13") == 2000
    finally:
        live.close()


def test_t14_missing_billing_refused(tmp_path, monkeypatch):
    """T14: billing.db가 없으면 거부한다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    os.unlink(built.root / "billing.db")
    dest = tmp_path / "bk"
    with pytest.raises(bk.BackupError, match="billing.db"):
        _backup(built, dest)
    assert _set_dirs(dest) == [] and _staging_dirs(dest) == []


def test_t15_pre_publish_verification(tmp_path, monkeypatch):
    """T15: 매니페스트의 backup_id가 31자이면 게시 전 검증이 거부하고 게시된 세트가 없다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    real = bk._write_manifest

    def broken(staging, outcome, **kwargs):
        recovery = dict(kwargs.get("recovery") or {})
        recovery["backup_id"] = recovery["backup_id"][:31]
        kwargs["recovery"] = recovery
        return real(staging, outcome, **kwargs)

    monkeypatch.setattr(bk, "_write_manifest", broken)
    dest = tmp_path / "bk"
    with pytest.raises(bk.BackupError):
        _backup(built, dest)
    assert _set_dirs(dest) == [] and _staging_dirs(dest) == []


def test_t16_exceptions_become_backup_error(tmp_path, monkeypatch):
    """T16: single.db가 nlink 2이면 ValueError가 아니라 BackupError이고 root는 그대로다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    os.link(built.root / "single.db", built.root / "single.db.extra-link")
    before = _names(built.root)
    with pytest.raises(bk.BackupError, match="one link"):
        _backup(built, tmp_path / "bk")
    assert _names(built.root) == before
