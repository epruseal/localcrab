"""#671 B6: 단일 모드 복원(opencrab.stores.single_restore) 시험.

설계 6판 9절의 T20~T41이 정본이다. 합성 root는 tests/test_factory_single_file.py의 보조 함수를
재사용한다. 모든 시험은 합성 데이터만 쓰고 라이브 경로를 쓰지 않는다.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_factory_single_file import (
    _DIM,
    _authorized_and_pointed,
    _exclusive_lock_attempt,
    _open_entries,
    _plain_table_counts,
    _single_settings,
    _write_ledger_state,
)
from tests.test_factory_single_file import (
    _fresh_single_registry as single_registry,  # noqa: F401  (autouse 고정물이며 시험이 레지스트리를 받는다)
)
from tests.test_single_backup import _backup, _leave_wal_frames

_VOLATILE = {"write.lock", "single.db.lifetime.lock", "billing.db.lock"}
_BINDING_REFUSAL = "file must be regular and have one link"


@pytest.fixture(autouse=True)
def _no_failed_shared_files():
    """H2: 앞선 시험의 실패 잔재가 뒤 시험의 거부를 가리지 않게 한다."""
    from opencrab.stores import _single_file as sf

    assert dict(sf._failed_shared_files) == {}
    yield


def _world(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    return built, _single_settings(built.root)


def _make_set(built, tmp_path, name: str = "bk") -> Path:
    return _backup(built, tmp_path / name).set_dir


def _restore(set_dir, settings, *, apply=None):
    """apply를 None으로 두면 키워드를 넘기지 않아 함수의 기본값(dry-run)을 시험한다."""
    from opencrab.stores import single_restore as sr

    lines: list[str] = []
    kwargs = {} if apply is None else {"apply": apply}
    rc = sr.restore_single(set_dir, settings, on_event=lines.append, **kwargs)
    return rc, "\n".join(lines)


def _add_rows(root: Path, rows: int = 50) -> None:
    """백업 뒤 쓰기를 모사한다. 두 파일에 표를 새로 만들고 행을 넣은 뒤 연결을 닫는다(체크포인트까지 간다)."""
    for name in ("single.db", "billing.db"):
        conn = sqlite3.connect(str(root / name))
        try:
            conn.execute("CREATE TABLE zz_after(a INTEGER)")
            conn.executemany("INSERT INTO zz_after VALUES (?)", [(i,) for i in range(rows)])
            conn.commit()
        finally:
            conn.close()


def _live_counts(path: Path) -> dict[str, int]:
    """root 판독은 immutable이 아닌 일반 열기다(D12). 남은 -wal 재생과 -journal 되감기가 드러난다."""
    conn = sqlite3.connect(str(path))
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"
            )
        ]
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    """root 아래 모든 파일의 sha256과 디렉터리 이름. 잠금 파일은 뺀다."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root) + "/"] = ""
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if rel not in _VOLATILE:
                out[rel] = _sha(Path(dirpath) / f)
    return out


def _saved(root: Path) -> list[str]:
    return sorted(n for n in os.listdir(root) if ".restore-saved-" in n)


def _temps(root: Path) -> list[str]:
    return sorted(n for n in os.listdir(root) if ".restore-tmp-" in n)


def _child_ok(code: str, root: Path) -> bool:
    return subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True).returncode == 0


_WRITE_LOCK_CODE = (
    "import sys; from opencrab.locking import write_lock\n"
    "with write_lock(sys.argv[1], timeout=0):\n    pass\n"
)
_SHARED_LIFETIME_CODE = (
    "import sys; from opencrab.locking import acquire_file_lock, release_file_lock\n"
    "release_file_lock(acquire_file_lock('single.db.lifetime.lock', sys.argv[1], shared=True, timeout=0))\n"
)


def _locks_free(root: Path) -> bool:
    return _exclusive_lock_attempt(root).returncode == 0 and _child_ok(_WRITE_LOCK_CODE, root)


def _set_copy(set_dir: Path, tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(set_dir, target)
    return target


def _rewrite_manifest_hashes(set_dir: Path) -> None:
    """세트 파일을 바꾼 뒤 매니페스트의 크기와 sha256을 맞춘다(verify_backup_set을 통과시키려는 시험용 조작)."""
    from opencrab.stores.backup import _hash_file

    path = set_dir / "MANIFEST.json"
    manifest = json.loads(path.read_text())
    for rel, meta in manifest["files"].items():
        if rel != "MANIFEST.json":
            meta["size"] = (set_dir / rel).stat().st_size
            meta["sha256"] = _hash_file(set_dir / rel)
    for entry in manifest.get("entries", []):
        rel = entry.get("path") or entry.get("destination")
        if rel in manifest["files"]:
            entry["size"] = manifest["files"][rel]["size"]
            if "sha256" in entry:
                entry["sha256"] = manifest["files"][rel]["sha256"]
    path.write_text(json.dumps(manifest))


def _other_generation_set(set_dir: Path, tmp_path: Path) -> Path:
    """세대가 다른 세트: single_file_metadata의 generation을 올리고 매니페스트를 맞춘다."""
    other = _set_copy(set_dir, tmp_path, "other-gen")
    conn = sqlite3.connect(str(other / "single.db"))
    try:
        conn.execute("UPDATE single_file_metadata SET generation = generation + 1")
        conn.commit()
    finally:
        conn.close()
    for extra in ("single.db-wal", "single.db-shm"):
        assert not (other / extra).exists()
    _rewrite_manifest_hashes(other)
    return other


def test_t20_dry_run_is_default_and_writes_nothing(tmp_path, monkeypatch):
    """T20: 기본은 dry-run이다. root 이름 집합과 sha256이 그대로고 계획 줄이 나온다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    before = _snapshot(built.root)
    rc, out = _restore(set_dir, settings)
    assert rc == 0, out
    assert "dry-run" in out and "계획" in out
    assert _snapshot(built.root) == before
    assert _saved(built.root) == [] and _temps(built.root) == []


def test_t21_round_trip_keeps_old_files_under_saved_names(tmp_path, monkeypatch):
    """T21: 백업 뒤 행을 더하고 복원하면 두 파일 행 수가 백업 시점이다. 보존 이름에는 백업 이후 값이 남는다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    at_backup = {n: _plain_table_counts(set_dir / n) for n in ("single.db", "billing.db")}
    _add_rows(built.root)
    after = {n: _plain_table_counts(built.root / n) for n in ("single.db", "billing.db")}
    assert after != at_backup
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    for name in ("single.db", "billing.db"):
        assert _live_counts(built.root / name) == at_backup[name]
    saved = _saved(built.root)
    for name in ("single.db", "billing.db"):
        kept = [n for n in saved if n.startswith(name + ".restore-saved-")]
        assert len(kept) == 1, saved
        assert _plain_table_counts(built.root / kept[0]) == after[name]


@pytest.mark.parametrize("variant", ["wal", "journal"])
def test_t22_sidecars_are_moved_not_replayed(tmp_path, monkeypatch, variant):
    """T22: 남은 -wal 재생과 billing.db-journal 되감기가 복원 결과를 되돌리지 않는다."""
    built, settings = _world(tmp_path, monkeypatch)
    if variant == "journal":
        conn = sqlite3.connect(str(built.root / "billing.db"))
        conn.execute("CREATE TABLE zz_b(a INTEGER, b TEXT)")
        conn.executemany("INSERT INTO zz_b VALUES (?, ?)", [(i, "y" * 50) for i in range(100)])
        conn.commit()
        conn.close()
    set_dir = _make_set(built, tmp_path)
    at_backup = {n: _plain_table_counts(set_dir / n) for n in ("single.db", "billing.db")}
    if variant == "wal":
        _leave_wal_frames(built.root)
        assert (built.root / "single.db-wal").stat().st_size > 0
    else:
        conn = sqlite3.connect(str(built.root / "billing.db"))
        conn.executemany("INSERT INTO zz_b VALUES (?, ?)", [(i, "z" * 200) for i in range(1000, 6000)])
        conn.commit()
        conn.close()
        crash = (
            "import sqlite3, sys, os\n"
            "c = sqlite3.connect(sys.argv[1] + '/billing.db', isolation_level=None)\n"
            "c.execute('PRAGMA cache_size=5')\n"
            "c.execute('BEGIN')\n"
            "c.executemany('INSERT INTO zz_b VALUES (?, ?)', [(i, 'x' * 200) for i in range(100000, 130000)])\n"
            "os._exit(0)\n"
        )
        assert _child_ok(crash, built.root)
        assert (built.root / "billing.db-journal").stat().st_size > 0
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    for name in ("single.db", "billing.db"):
        assert _live_counts(built.root / name) == at_backup[name]
    conn = sqlite3.connect(str(built.root / "billing.db"))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()
    names = _saved(built.root)
    expected = "single.db-wal.restore-saved-" if variant == "wal" else "billing.db-journal.restore-saved-"
    assert any(n.startswith(expected) for n in names), names


_HOLD_CODE = (
    "import sys\n"
    "from opencrab.config import Settings\n"
    "from opencrab.stores.factory import hold_single_file\n"
    "root = sys.argv[1]\n"
    f"s = Settings(STORAGE_MODE='local', LOCAL_DATA_DIR=root, SINGLE_SQLITE_FILE=root + '/single.db',"
    f" EMBEDDING_BACKEND='openai', EMBED_DIM={_DIM})\n"
    "with hold_single_file(s):\n"
    "    print('READY', flush=True)\n"
    "    sys.stdin.readline()\n"
)


def test_t23_busy_lifetime_lock_refuses_apply(tmp_path, monkeypatch):
    """T23: 하위 프로세스가 hold_single_file을 쥔 동안 --apply가 rc 1로 거부된다. 해제 뒤에는 성공한다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_CODE, str(built.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        before = _snapshot(built.root)
        rc, out = _restore(set_dir, settings, apply=True)
        assert rc == 1, out
        assert _snapshot(built.root) == before
    finally:
        child.communicate("\n", timeout=30)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out


def test_t24_other_generation_set_is_refused(tmp_path, monkeypatch):
    """T24: 세대가 다른 세트는 사전 점검에서 거부되고 root는 그대로다."""
    built, settings = _world(tmp_path, monkeypatch)
    other = _other_generation_set(_make_set(built, tmp_path), tmp_path)
    before = _snapshot(built.root)
    rc, out = _restore(other, settings, apply=True)
    assert rc == 1, out
    assert "임시 복사" not in out
    assert _snapshot(built.root) == before


def test_t25_damaged_set_is_refused_by_verification(tmp_path, monkeypatch):
    """T25: 한 바이트를 바꾼 세트는 verify 단계에서 거부된다(S2의 sha 대조가 아니다)."""
    built, settings = _world(tmp_path, monkeypatch)
    damaged = _set_copy(_make_set(built, tmp_path), tmp_path, "damaged")
    data = bytearray((damaged / "single.db").read_bytes())
    data[5000] ^= 0xFF
    (damaged / "single.db").write_bytes(bytes(data))
    before = _snapshot(built.root)
    rc, out = _restore(damaged, settings, apply=True)
    assert rc == 1, out
    assert "임시 복사" not in out
    assert _snapshot(built.root) == before


def test_t26_set_without_block_is_refused(tmp_path, monkeypatch):
    """T26: single_db_recovery 블록이 없는 세트(다섯 파일 형태)는 거부된다."""
    built, settings = _world(tmp_path, monkeypatch)
    plain = _set_copy(_make_set(built, tmp_path), tmp_path, "plain")
    manifest = json.loads((plain / "MANIFEST.json").read_text())
    del manifest["single_db_recovery"]
    (plain / "MANIFEST.json").write_text(json.dumps(manifest))
    before = _snapshot(built.root)
    rc, out = _restore(plain, settings, apply=True)
    assert rc == 1, out
    assert _snapshot(built.root) == before


@pytest.mark.parametrize("planted", ["single.db.restore-saved-20200101T000000Z-abcdef", "billing.db.restore-tmp-x"])
def test_t27_previous_run_residue_is_refused(tmp_path, monkeypatch, planted):
    """T27: 이전 적용의 보존 이름이나 임시 이름이 root에 있으면 rc 3이고 맞은 이름을 출력한다. root는 그대로다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    (built.root / planted).write_bytes(b"residue")
    before = _snapshot(built.root)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 3, out
    assert planted in out
    assert _snapshot(built.root) == before


def test_t28_truncated_copy_is_refused_and_root_untouched(tmp_path, monkeypatch):
    """T28: S2 복사가 잘려 sha256이 다르면 임시를 지우고 rc 1이다. root는 그대로다."""
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    real = sr._copy_file

    def truncating(src, dst):
        real(src, dst)
        with open(dst, "r+b") as fh:
            fh.truncate(max(fh.seek(0, 2) - 100, 0))

    monkeypatch.setattr(sr, "_copy_file", truncating)
    before = _snapshot(built.root)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 1, out
    assert _snapshot(built.root) == before
    assert _temps(built.root) == []
    assert _locks_free(built.root)


def _injected_rename(monkeypatch, match, on_fail=None):
    """원본 이름이 match(이름)를 만족하는 os.rename만 OSError로 실패시킨다(호출 순번이 아니라 이름으로 고른다)."""
    real = os.rename

    def wrapper(src, dst, *args, **kwargs):
        if match(os.path.basename(str(src))):
            if on_fail is not None:
                on_fail()
            raise OSError(errno.EIO, "injected rename failure")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", wrapper)


def _injected_dir_fsync(monkeypatch):
    real = os.fsync

    def wrapper(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        return real(fd)

    monkeypatch.setattr(os, "fsync", wrapper)


_POINTS = ["after_s3", "inside_s4", "after_s5_link", "after_s5", "after_s6"]


def _hold_refusal(settings) -> BaseException:
    from opencrab.stores import factory

    with pytest.raises(ValueError) as caught:
        with factory.hold_single_file(settings):
            pass
    return caught.value


@pytest.mark.parametrize("point", _POINTS)
def test_t29_interruption_points(tmp_path, monkeypatch, single_registry, point):  # noqa: F811
    """T29: 8절 표의 지점마다 상태와 거부를 단언하고, 사람이 치운 뒤 재실행하면 완료한다."""
    built, settings = _world(tmp_path, monkeypatch)
    root = built.root
    set_dir = _make_set(built, tmp_path)
    at_backup = {n: _plain_table_counts(set_dir / n) for n in ("single.db", "billing.db")}
    _add_rows(root)
    _leave_wal_frames(root)
    # H1: 세계 준비의 마지막 rw 연결 종료가 곁파일을 지우지 않았다.
    assert (root / "single.db-wal").exists() and (root / "single.db-shm").exists()
    after = {n: _plain_table_counts(root / n) for n in ("single.db", "billing.db")}

    def present_before_s4():
        assert (root / "single.db-wal").exists() and (root / "single.db-shm").exists()

    with monkeypatch.context() as injected:
        if point == "after_s3":
            _injected_rename(injected, lambda n: n == "single.db-wal", present_before_s4)
        elif point == "inside_s4":
            _injected_rename(injected, lambda n: n == "single.db-shm", lambda: None)
        elif point == "after_s5_link":
            _injected_rename(injected, lambda n: n.startswith("billing.db.restore-tmp-"))
        elif point == "after_s5":
            _injected_rename(injected, lambda n: n.startswith("single.db.restore-tmp-"))
        else:
            _injected_dir_fsync(injected)
        rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 2, out
    assert _locks_free(root), "S9: 오류 뒤에도 두 잠금이 풀렸다"
    assert _open_entries(single_registry) == []
    # V-B6-5: rc 2 출력이 이 적용의 보존 이름과 임시 이름을 전부 말한다(사람이 치우지 않으면 재실행이 rc 3으로 막힌다).
    names_now = _saved(root) + _temps(root)
    if point != "after_s6":
        assert _temps(root), "임시 이름이 남은 지점에서 이름 단언이 실제로 임시 이름을 본다"
    for name in names_now:
        assert name in out, (name, out)

    live = root / "single.db"
    saved = _saved(root)
    if point == "after_s6":
        assert os.lstat(live).st_nlink == 1
        assert _sha(live) == _sha(set_dir / "single.db")
        assert _temps(root) == []
        assert any(n.startswith("single.db.restore-saved-") for n in saved), saved
        from opencrab.stores import factory

        with factory.hold_single_file(settings):
            pass
        assert _open_entries(single_registry) == []
    else:
        assert os.lstat(live).st_nlink == 2
        err = _hold_refusal(settings)
        assert _BINDING_REFUSAL in str(err)
        assert _plain_table_counts(live) == after["single.db"]
    if point == "after_s3":
        assert (root / "single.db-wal").exists() and (root / "single.db-shm").exists()
    if point == "inside_s4":
        assert any(n.startswith("single.db-wal.restore-saved-") for n in saved), saved
        assert (root / "single.db-shm").exists()
    if point == "after_s5_link":
        assert os.lstat(root / "billing.db").st_nlink == 2
        assert _plain_table_counts(root / "billing.db") == after["billing.db"]
    if point == "after_s5":
        assert _plain_table_counts(root / "billing.db") == at_backup["billing.db"]
    # 사람이 보존 이름과 임시 이름을 치운 뒤 재실행하면 처음 상태에서 완료한다.
    for name in _saved(root) + _temps(root):
        (root / name).unlink()
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    for name in ("single.db", "billing.db"):
        assert _live_counts(root / name) == at_backup[name]


def test_t30_restore_leaves_everything_else_alone(tmp_path, monkeypatch):
    """T30: 두 파일과 곁파일 밖의 root 내용(데이터 디렉터리, 대장, manifest, 표식)은 그대로다."""
    built, settings = _world(tmp_path, monkeypatch)
    root = built.root
    for rel in ("openclaw-conversations/automation/state.json", "packs/p/a.txt", "datasets/d/b.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x:" + rel)
    set_dir = _make_set(built, tmp_path)
    _add_rows(root)

    def others():
        return {
            k: v
            for k, v in _snapshot(root).items()
            if not (k.startswith(("single.db", "billing.db")) and ".lifetime" not in k)
        }

    before = others()
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    assert others() == before
    assert "ledger.json" in before and "manifest.json" in before and "marker.json" in before


def test_t31_post_check_catches_a_different_valid_file(tmp_path, monkeypatch):
    """T31: sha 검사를 우회해 유효하지만 다른 내용의 파일이 들어와도 S8의 행 수 대조가 rc 2로 잡는다."""
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    alt = tmp_path / "alt"
    alt.mkdir()
    for name in ("single.db", "billing.db"):
        shutil.copyfile(built.root / name, alt / name)
    monkeypatch.setattr(sr, "_copy_file", lambda src, dst: shutil.copyfile(alt / src.name, dst))
    monkeypatch.setattr(sr, "_sha_matches", lambda path, expected: True)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 2, out
    assert "복원 파일이 세트와 다르다" in out


def test_t32_binding_check_is_an_independent_layer(tmp_path, monkeypatch):
    """T32: P3를 우회한 다른 세대 세트를 적용하면 S10 결속 검증이 실패로 보고한다(rc 2)."""
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    other = _other_generation_set(_make_set(built, tmp_path), tmp_path)
    monkeypatch.setattr(sr, "_check_binding", lambda root, set_single: None)
    rc, out = _restore(other, settings, apply=True)
    assert rc == 2, out
    assert "결속 검증 실패" in out


def test_t33_temp_copies_live_beside_the_files(tmp_path, monkeypatch):
    """T33: 임시 파일의 부모가 root이고 st_dev가 root와 같다(rename이 원자적이다)."""
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    seen: list[Path] = []
    real = sr._copy_file

    def spy(src, dst):
        seen.append(Path(dst))
        real(src, dst)

    monkeypatch.setattr(sr, "_copy_file", spy)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    assert len(seen) == 2
    for dst in seen:
        assert dst.parent == built.root
        assert ".restore-tmp-" in dst.name
        assert os.stat(dst.parent).st_dev == os.stat(built.root).st_dev


def test_t34_unauthorized_root_is_refused(tmp_path, monkeypatch):
    """T34: 대장 state가 authorized가 아니면 거부되고 root는 그대로다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _write_ledger_state(built.root, "ready")
    before = _snapshot(built.root)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 1, out
    assert _snapshot(built.root) == before


def test_t35_replacement_leaves_no_link_traces(tmp_path, monkeypatch):
    """T35: 복원 뒤 live 두 파일과 보존 두 파일의 nlink가 1이고 보존 파일의 inode가 live와 다르다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    for name in ("single.db", "billing.db"):
        live = os.lstat(built.root / name)
        kept = [n for n in _saved(built.root) if n.startswith(name + ".restore-saved-")]
        assert len(kept) == 1
        saved = os.lstat(built.root / kept[0])
        assert live.st_nlink == 1 and saved.st_nlink == 1
        assert saved.st_ino != live.st_ino


def test_t36_checks_run_under_the_exclusive_lock(tmp_path, monkeypatch):
    """T36: S8 점검 시점에는 독점 수명 잠금이 쥐어져 있고 S10 결속 검증 직전에는 풀려 있다."""
    from opencrab.stores import factory
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    events: list[str] = []
    real_check = sr._check_replaced
    real_hold = factory.hold_single_file

    def check(root, baseline):
        events.append("s8-busy" if _exclusive_lock_attempt(root).returncode != 0 else "s8-free")
        return real_check(root, baseline)

    def hold(settings_arg):
        events.append("s10-free" if _child_ok(_SHARED_LIFETIME_CODE, built.root) else "s10-busy")
        return real_hold(settings_arg)

    monkeypatch.setattr(sr, "_check_replaced", check)
    monkeypatch.setattr(factory, "hold_single_file", hold)
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    assert events == ["s8-busy", "s10-free"], events


def test_cli_main_dry_run_then_apply(tmp_path, monkeypatch, capsys):
    """CLI main(): 기본은 dry-run이고 --apply만 root를 바꾼다(프로세스 안 호출. 하위 프로세스 진입점은 T40)."""
    from opencrab.stores import single_restore as sr
    from tests.test_factory_single_file import _single_env

    built, _settings = _world(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    set_dir = _make_set(built, tmp_path)
    _add_rows(built.root)
    before = _snapshot(built.root)
    assert sr.main([str(set_dir)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert _snapshot(built.root) == before
    assert sr.main([str(set_dir), "--apply"]) == 0
    assert "복원 완료" in capsys.readouterr().out
    assert _saved(built.root)


def test_t37_no_open_shared_object_remains(tmp_path, monkeypatch, single_registry):  # noqa: F811
    """T37: 복원 전에 레지스트리를 비우고, S10이 키를 다시 만들었음(이 프로세스에서 실행됨)과 열린 공유 객체 부재를 본다."""
    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    # _make_set의 백업이 같은 키를 이미 등록한다. 비우지 않으면 아래 키 단언이 복원 전부터 참이라 S10을 증명하지 못한다.
    single_registry.clear()
    key = str(built.root / "single.db")
    assert key not in single_registry
    rc, out = _restore(set_dir, settings, apply=True)
    assert rc == 0, out
    assert key in single_registry
    assert _open_entries(single_registry) == []


@pytest.mark.parametrize("variant", ["lifetime_hardlink", "write_lock_hardlink", "shared_upgrade", "body_raises"])
def test_t38_lock_acquisition_errors_and_body_errors(tmp_path, monkeypatch, variant):
    """T38: 잠금 획득의 OSError, RuntimeError는 rc 1 거부(root 그대로)다. write.lock 구간 본문의 오류는 rc 2다.

    body_raises는 "root는 그대로다"를 말하면 안 된다. 그 시점에는 교체가 이미 일어났다(B1).
    """
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    root = built.root
    set_dir = _make_set(built, tmp_path)
    _add_rows(root)
    before = _snapshot(root)
    alias = tmp_path / "lock-alias"
    held = None
    try:
        if variant == "lifetime_hardlink":
            lock = root / "single.db.lifetime.lock"
            assert lock.exists()
            os.link(lock, alias)
        elif variant == "write_lock_hardlink":
            lock = root / "write.lock"
            if not lock.exists():
                lock.write_bytes(b"")
            os.link(lock, alias)
        elif variant == "shared_upgrade":
            from opencrab.locking import acquire_file_lock

            held = acquire_file_lock("single.db.lifetime.lock", str(root), shared=True, timeout=0)
        else:
            monkeypatch.setattr(sr, "_check_replaced", lambda root_arg, baseline: ["x"])

            def listing_fails(plan):
                raise OSError(errno.EIO, "injected listing failure")

            monkeypatch.setattr(sr, "_names_of_this_apply", listing_fails)
        rc, out = _restore(set_dir, settings, apply=True)
    finally:
        if held is not None:
            from opencrab.locking import release_file_lock

            release_file_lock(held)
    if variant == "body_raises":
        assert rc == 2, out
        assert "(root는 그대로다)" not in out
        assert "root가 바뀌었을 수 있다" in out
        assert _saved(root), "교체가 실제로 일어난 뒤의 오류다"
        assert _exclusive_lock_attempt(root).returncode == 0, "S9: 수명 잠금이 풀렸다"
        return
    assert rc == 1, out
    assert "거부" in out
    expected = "write.lock을 잡지 못했다(root는 그대로다)" if variant == "write_lock_hardlink" else "수명 잠금을 얻지 못했다"
    assert expected in out
    assert _snapshot(root) == before
    assert _saved(root) == [] and _temps(root) == []
    if variant == "write_lock_hardlink":
        assert _exclusive_lock_attempt(root).returncode == 0, "S1에서 쥔 수명 잠금이 finally에서 풀렸다"


def _zero_page_that_only_integrity_check_sees(source: Path, target: Path) -> int:
    """source의 사본을 target에 쓴다. 한 쪽을 0으로 채워 표별 행 수는 읽히는데 integrity_check는 실패하게 한다.

    그런 쪽을 뒤에서부터 찾는다. 못 찾으면 시험 세계가 이 시험의 전제를 만족하지 않는 것이다(실패로 알린다).
    """
    data = source.read_bytes()
    page_size = int.from_bytes(data[16:18], "big") or 65536
    probe = target.with_name(target.name + ".probe")
    for page in range(len(data) // page_size, 1, -1):
        damaged = bytearray(data)
        damaged[(page - 1) * page_size : page * page_size] = bytes(page_size)
        probe.write_bytes(bytes(damaged))
        uri = probe.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            try:
                names = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                        "AND sql NOT LIKE 'CREATE VIRTUAL%'"
                    )
                ]
                for name in names:
                    conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
            except sqlite3.DatabaseError:
                continue
            try:
                broken = [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]
            except sqlite3.DatabaseError:
                broken = True
        finally:
            conn.close()
        if broken:
            probe.replace(target)
            return page
    probe.unlink(missing_ok=True)
    pytest.fail("행 수는 읽히고 integrity_check만 실패하는 쪽이 없다")


def test_t39_post_check_reports_integrity_failure(tmp_path, monkeypatch):
    """T39: S8의 integrity_check 실패는 \"integrity_check 실패\" 문구와 rc 2다. 행 수 대조만으로는 손상을 못 잡는다."""
    from opencrab.stores import single_restore as sr

    built, settings = _world(tmp_path, monkeypatch)
    set_dir = _make_set(built, tmp_path)
    damaged = _set_copy(set_dir, tmp_path, "zeroed")
    _zero_page_that_only_integrity_check_sees(set_dir / "single.db", damaged / "single.db")
    _rewrite_manifest_hashes(damaged)
    monkeypatch.setattr(sr, "verify_backup_set", lambda path: None)  # P2를 우회해 S8까지 보낸다
    rc, out = _restore(damaged, settings, apply=True)
    assert rc == 2, out
    assert "복원 파일이 세트와 다르다" in out
    assert "integrity_check 실패" in out


def test_t40_module_entry_point_subprocess(tmp_path, monkeypatch):
    """T40: python -m opencrab.stores.single_restore를 하위 프로세스로 부른다(dry-run, 잠금 거부, 적용)."""
    from tests.test_factory_single_file import _single_env

    built, _settings = _world(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    set_dir = _make_set(built, tmp_path)
    at_backup = {n: _plain_table_counts(set_dir / n) for n in ("single.db", "billing.db")}
    _add_rows(built.root)

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "opencrab.stores.single_restore", str(set_dir), *args],
            capture_output=True,
            text=True,
        )

    before = _snapshot(built.root)
    dry = cli()
    assert dry.returncode == 0, dry.stderr
    assert "dry-run" in dry.stdout
    assert _snapshot(built.root) == before
    alias = tmp_path / "lock-alias"
    os.link(built.root / "single.db.lifetime.lock", alias)
    try:
        refused = cli("--apply")
    finally:
        alias.unlink()
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "Traceback" not in refused.stderr
    assert "거부" in refused.stdout
    assert _snapshot(built.root) == before
    applied = cli("--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "복원 완료" in applied.stdout
    for name in ("single.db", "billing.db"):
        assert _live_counts(built.root / name) == at_backup[name]
    assert len(_saved(built.root)) == 2


def test_t41_main_converts_an_unexpected_failure(tmp_path, monkeypatch, capsys):
    """T41: restore_single 밖으로 나온 예외(여기서는 S9 해제 실패)는 CLI에서 rc 2와 apply 여부 문구가 된다."""
    from opencrab.locking import release_file_lock as real_release
    from opencrab.stores import single_restore as sr
    from tests.test_factory_single_file import _single_env

    built, _settings = _world(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    set_dir = _make_set(built, tmp_path)
    at_backup = {n: _plain_table_counts(set_dir / n) for n in ("single.db", "billing.db")}
    _add_rows(built.root)

    def failing_release(handle):
        real_release(handle)  # 잠금은 풀리고 예외만 올라온다
        raise OSError(errno.EIO, "injected release failure")

    monkeypatch.setattr(sr, "release_file_lock", failing_release)
    rc = sr.main([str(set_dir), "--apply"])
    err = capsys.readouterr().err
    assert rc == 2, err
    assert "예상하지 못한 실패" in err and "apply=True" in err and "root가 바뀌었을 수 있다" in err
    for name in ("single.db", "billing.db"):
        assert _live_counts(built.root / name) == at_backup[name]
