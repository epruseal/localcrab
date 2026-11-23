"""단일 SQLite root를 백업 세트로 되돌리는 복원 명령(#671 B6).

사용법: python -m opencrab.stores.single_restore SET_DIR [--apply]

기본은 dry-run이다. 점검과 계획 줄만 출력하고 root에 아무것도 쓰지 않는다. --apply만 root를 바꾼다.
세트는 opencrab.stores.backup.backup_single_root가 만든 것이어야 한다.

적용 순서(설계 5판 8절):
  S1 독점 수명 잠금(timeout 0)과 write.lock
  S2 세트 두 파일을 root 안 임시 이름으로 복사하고 sha256을 대조한다
  S3 live single.db에 보존 링크를 건다(이 시점부터 마지막 rename까지 nlink가 2라 factory 경유 열기가 막힌다)
  S4 곁파일 여섯(-wal, -shm, -journal)을 보존 이름으로 옮긴다
  S5 billing.db에 보존 링크를 걸고 임시 파일을 그 이름 위로 rename한다
  S6 single.db 임시 파일을 live 이름 위로 rename한다
  S7 root 디렉터리를 fsync한다
  S8 잠금 아래에서 immutable integrity_check와 표별 행 수를 세트 기준과 대조한다
  S9 잠금을 푼다
  S10 결속 검증(hold_single_file)

옛 파일과 곁파일은 지우지 않고 ".restore-saved-<id>" 이름으로 남긴다. 서비스 정지와 fuser 확인은 운영 절차의
몫이다. 코드는 factory 경유 열람자의 부재만 독점 수명 잠금으로 확인한다.

종료 코드: 0 전부 통과, 1 사전 점검 거부와 잠금 획득 거부(root 그대로), 2 적용 중 오류(root가 바뀌었을 수 있다),
3 이전 적용 잔재 감지(root 그대로).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import secrets
import shutil
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencrab.locking import acquire_file_lock, release_file_lock, write_lock
from opencrab.stores.backup import MANIFEST_NAME, BackupError, _hash_file, verify_backup_set

_SINGLE = "single.db"
_BILLING = "billing.db"
_LIFETIME_LOCK = "single.db.lifetime.lock"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# 곁파일 대상 여섯. 순서가 S4의 이동 순서다.
_SIDECARS = tuple(f"{base}{suffix}" for base in (_SINGLE, _BILLING) for suffix in _SIDECAR_SUFFIXES)
_RESIDUE_PATTERNS = tuple(f"{base}*.restore-{kind}-*" for base in (_SINGLE, _BILLING) for kind in ("tmp", "saved"))
_WRITE_LOCK_TIMEOUT = 5.0

RC_OK = 0
RC_REFUSED = 1
RC_FAILED = 2
RC_RESIDUE = 3


class _RefusedError(Exception):
    """사전 점검이나 S1과 S2의 거부. root는 그대로다."""

    def __init__(self, message: str, rc: int = RC_REFUSED) -> None:
        super().__init__(message)
        self.rc = rc


@dataclass
class _Plan:
    root: Path
    set_dir: Path
    restore_id: str
    files: dict[str, tuple[Path, str]]  # 이름 -> (세트 안 경로, 매니페스트 sha256)
    baseline: dict[str, dict[str, int]]  # 이름 -> 세트 쪽 표별 행 수(P6)

    def tmp_path(self, name: str) -> Path:
        return self.root / f"{name}.restore-tmp-{self.restore_id}"

    def saved_path(self, name: str) -> Path:
        return self.root / f"{name}.restore-saved-{self.restore_id}"


def _print(message: str) -> None:
    print(message, flush=True)


def _immutable_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _table_counts(path: Path) -> dict[str, int]:
    """가상표를 뺀 일반 표의 행 수(그림자 표는 일반 표로 센다). 읽기 전용 immutable로 연다."""
    conn = sqlite3.connect(_immutable_uri(path), uri=True)
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


def _integrity_problem(path: Path) -> str | None:
    """immutable integrity_check. 통과하면 None, 아니면 원인 문자열을 돌려준다(손상 쪽은 DatabaseError를 올린다)."""
    try:
        conn = sqlite3.connect(_immutable_uri(path), uri=True)
        try:
            rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    return None if rows == ["ok"] else "; ".join(rows[:3])


def _read_manifest(set_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((set_dir / MANIFEST_NAME).read_text())
    except (OSError, ValueError) as exc:
        raise _RefusedError(f"매니페스트를 읽지 못했다: {exc}") from exc
    block = manifest.get("single_db_recovery")
    if not isinstance(block, dict):
        raise _RefusedError("세트에 single_db_recovery 블록이 없다. 단일 모드 백업 세트가 아니다")
    return manifest


def _check_binding(root: Path, set_single: Path) -> None:
    """P3: 세트 single.db의 결속 메타데이터가 root의 대장과 현재 경로에 맞는지 본다."""
    from opencrab.stores._single_file import load_single_file_binding

    conn = sqlite3.connect(_immutable_uri(set_single), uri=True)
    try:
        rows = conn.execute("SELECT root, database_path, db_uuid, generation FROM single_file_metadata").fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise _RefusedError("세트 single.db의 메타데이터 행이 하나가 아니다")
    metadata = dict(zip(("root", "database", "db_uuid", "generation"), rows[0], strict=True))
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        load_single_file_binding(
            str(root), str(root / _SINGLE), root_fd, root_fd, "ledger.json", "manifest.json", "marker.json", metadata
        )
    finally:
        os.close(root_fd)


def _precheck(set_dir: Path, settings: Any) -> _Plan:
    """사전 점검 전량(P1~P6). 아무것도 쓰지 않는다."""
    from opencrab.stores.factory import _single_mode_checks

    if not getattr(settings, "single_sqlite_file", ""):
        raise _RefusedError("단일 SQLite 설정(SINGLE_SQLITE_FILE)이 없다")
    root = Path(os.path.dirname(_single_mode_checks(settings)))
    for name in (_SINGLE, _BILLING):
        if not (root / name).is_file() or (root / name).is_symlink():
            raise _RefusedError(f"root에 일반 파일 {name}이 없다: {root}")

    residue = sorted(name for name in os.listdir(root) if any(fnmatch.fnmatchcase(name, p) for p in _RESIDUE_PATTERNS))
    if residue:
        raise _RefusedError(
            "이전 적용의 잔재 이름이 root에 있다. 확인해 치우거나 옮긴 뒤 다시 실행한다: " + ", ".join(residue),
            RC_RESIDUE,
        )

    verify_backup_set(set_dir)
    manifest = _read_manifest(set_dir)
    block = manifest["single_db_recovery"]
    names = (block.get("database"), block.get("billing"))
    if names != (_SINGLE, _BILLING):
        raise _RefusedError(f"세트 블록의 파일 이름이 단일 모드 규약과 다르다: {names}")
    files: dict[str, tuple[Path, str]] = {}
    for name in names:
        meta = manifest.get("files", {}).get(name)
        if not isinstance(meta, dict) or not meta.get("sha256"):
            raise _RefusedError(f"매니페스트에 {name}의 sha256이 없다")
        files[name] = (set_dir / name, meta["sha256"])

    _check_binding(root, files[_SINGLE][0])

    need = sum(path.stat().st_size for path, _ in files.values())
    free = shutil.disk_usage(root).free
    if free < need:
        raise _RefusedError(f"root 파일시스템 여유가 모자란다: 필요 {need}바이트, 여유 {free}바이트")

    baseline = {name: _table_counts(path) for name, (path, _) in files.items()}
    restore_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    return _Plan(root, set_dir, restore_id, files, baseline)


def _copy_file(src: Path, dst: Path) -> None:
    """src를 dst로 복사하고 fsync한다. dst는 새로 만든다(이미 있으면 실패)."""
    with open(src, "rb") as reader, open(dst, "xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def _sha_matches(path: Path, expected: str) -> bool:
    return _hash_file(path) == expected


def _stage_copies(plan: _Plan) -> None:
    """S2: 세트 두 파일을 root 안 임시 이름으로 복사하고 sha256을 대조한다. 실패하면 임시를 지우고 올린다."""
    try:
        for name, (src, expected) in plan.files.items():
            tmp = plan.tmp_path(name)
            _copy_file(src, tmp)
            if not _sha_matches(tmp, expected):
                raise _RefusedError(f"임시 복사본의 sha256이 매니페스트와 다르다: {tmp.name}")
    except BaseException:
        for name in plan.files:
            plan.tmp_path(name).unlink(missing_ok=True)
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_replaced(root: Path, baseline: dict[str, dict[str, int]]) -> list[str]:
    """S8: live 두 파일의 immutable integrity_check와 표별 행 수를 세트 기준과 대조한다. 어긋난 항목 목록을 돌려준다."""
    problems: list[str] = []
    for name, expected in baseline.items():
        path = root / name
        reason = _integrity_problem(path)
        if reason is not None:
            problems.append(f"{name}: integrity_check 실패({reason})")
            continue
        counts = _table_counts(path)
        if counts != expected:
            diff = sorted(t for t in set(counts) | set(expected) if counts.get(t) != expected.get(t))
            problems.append(f"{name}: 행 수가 세트와 다르다({', '.join(diff)})")
    return problems


def _names_of_this_apply(plan: _Plan) -> list[str]:
    """이 적용 id의 보존 이름과 임시 이름. 사람이 확인해 치울 대상이다."""
    suffixes = (f".restore-saved-{plan.restore_id}", f".restore-tmp-{plan.restore_id}")
    return sorted(n for n in os.listdir(plan.root) if n.endswith(suffixes))


def _swap_files(plan: _Plan) -> None:
    """S3~S7. 순서가 곧 안전 규칙이다: single.db 보존 링크가 가장 먼저, single.db rename이 가장 나중이다."""
    root = plan.root
    suffix = f".restore-saved-{plan.restore_id}"
    os.link(root / _SINGLE, plan.saved_path(_SINGLE))  # S3
    for name in _SIDECARS:  # S4
        path = root / name
        if os.path.lexists(path):
            os.rename(path, root / (name + suffix))
    os.link(root / _BILLING, plan.saved_path(_BILLING))  # S5
    os.rename(plan.tmp_path(_BILLING), root / _BILLING)
    os.rename(plan.tmp_path(_SINGLE), root / _SINGLE)  # S6
    _fsync_dir(root)  # S7


def _replace_under_locks(plan: _Plan, say: Callable[[str], None]) -> int:
    try:
        _stage_copies(plan)
    except Exception as exc:
        say(f"거부: 임시 복사 단계에서 실패했다(root는 그대로다): {exc}")
        return RC_REFUSED
    try:
        _swap_files(plan)
        problems = _check_replaced(plan.root, plan.baseline)  # S8
    except Exception as exc:
        say(f"오류: 교체 도중 실패했다: {exc}")
        say("중간 상태다. 이 적용의 보존 이름과 임시 이름을 사람이 확인한다: " + ", ".join(_names_of_this_apply(plan)))
        return RC_FAILED
    if problems:
        say("복원 파일이 세트와 다르다: " + "; ".join(problems))
        say("변경은 되돌리지 않았다. 이 적용의 보존 이름과 임시 이름을 보고 사람이 판단한다: " + ", ".join(_names_of_this_apply(plan)))
        return RC_FAILED
    return RC_OK


def _verify_binding(settings: Any, say: Callable[[str], None]) -> int:
    """S10: 두 잠금을 푼 뒤 결속 검증을 한다."""
    from opencrab.stores.factory import hold_single_file

    try:
        with hold_single_file(settings):
            pass
    except Exception as exc:
        say(f"결속 검증 실패: {exc}")
        return RC_FAILED
    return RC_OK


def _apply(plan: _Plan, settings: Any, say: Callable[[str], None]) -> int:
    root = str(plan.root)
    try:
        lifetime = acquire_file_lock(_LIFETIME_LOCK, root, timeout=0)  # S1
    except TimeoutError as exc:
        say(f"거부: 수명 잠금을 쥔 프로세스가 있다. 서비스와 임시 인스턴스를 멈춘 뒤 다시 한다: {exc}")
        return RC_REFUSED
    except (OSError, RuntimeError) as exc:
        say(f"거부: 수명 잠금을 얻지 못했다(root는 그대로다): {exc}")
        return RC_REFUSED
    try:
        try:
            with ExitStack() as stack:
                # 획득만 try로 감싼다. 본문과 해제의 오류는 root가 바뀐 뒤일 수 있어 아래 바깥 except가 rc 2로 낸다.
                try:
                    stack.enter_context(write_lock(root, timeout=_WRITE_LOCK_TIMEOUT))
                except (OSError, RuntimeError) as exc:
                    say(f"거부: write.lock을 잡지 못했다(root는 그대로다): {exc}")
                    return RC_REFUSED
                rc = _replace_under_locks(plan, say)  # S2~S8
        except (OSError, RuntimeError) as exc:
            say(
                f"오류: write.lock 구간에서 실패했다. root가 바뀌었을 수 있다. "
                f"적용 id {plan.restore_id}의 보존 이름과 임시 이름을 확인한다: {exc}"
            )
            return RC_FAILED
    finally:
        release_file_lock(lifetime)  # S9
    if rc != RC_OK:
        return rc
    rc = _verify_binding(settings, say)  # S10
    if rc == RC_OK:
        say("복원 완료: 결속 검증을 통과했다. 보존 이름은 사람이 확인한 뒤 치운다: " + ", ".join(_names_of_this_apply(plan)))
    return rc


def restore_single(
    set_dir: str | Path,
    settings: Any,
    *,
    apply: bool = False,
    on_event: Callable[[str], None] | None = None,
) -> int:
    """백업 세트로 단일 root의 두 파일을 되돌린다. 기본은 dry-run이다. 종료 코드를 돌려준다."""
    say = on_event or _print
    try:
        plan = _precheck(Path(set_dir), settings)
    except _RefusedError as exc:
        say(f"거부: {exc}")
        return exc.rc
    except (BackupError, RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        say(f"거부: {exc}")
        return RC_REFUSED
    say(f"사전 점검 통과: root={plan.root.name}, 세트={plan.set_dir.name}, 적용 id={plan.restore_id}")
    say("계획: 임시 복사 -> single.db 보존 링크 -> 곁파일 여섯 이동 -> billing.db 교체 -> single.db 교체 -> 점검 -> 결속 검증")
    if not apply:
        say("dry-run이다. root를 바꾸지 않았다. 적용하려면 --apply를 붙인다.")
        return RC_OK
    return _apply(plan, settings, say)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opencrab.stores.single_restore",
        description="백업 세트로 단일 SQLite root의 single.db와 billing.db를 되돌린다. 기본은 dry-run이다.",
    )
    parser.add_argument("set_dir", help="backup_single_root가 만든 세트 디렉터리")
    parser.add_argument("--apply", action="store_true", help="root를 실제로 바꾼다(기본은 점검과 계획 출력만)")
    args = parser.parse_args(argv)
    try:
        from opencrab.config import get_settings

        settings = get_settings()
    except Exception as exc:
        print(f"ERROR: 설정을 읽지 못했다: {exc}", file=sys.stderr)
        return RC_REFUSED
    try:
        return restore_single(args.set_dir, settings, apply=args.apply)
    except Exception as exc:
        # 예상하지 못한 예외가 미처리 종료 코드 1(거부)로 읽히지 않게 한다. KeyboardInterrupt와 SystemExit은 덮지 않는다.
        message = f"ERROR: 예상하지 못한 실패다(apply={args.apply}). apply=True면 root가 바뀌었을 수 있다: {exc}"
        print(message, file=sys.stderr)
        return RC_FAILED


if __name__ == "__main__":  # pragma: no cover - 하위 프로세스 시험에서 실행한다
    raise SystemExit(main())
