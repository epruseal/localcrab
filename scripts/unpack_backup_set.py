#!/usr/bin/env python3
"""백업 세트에서 DB 다섯 파일만 sha256 대조해 새 디렉터리로 푼다 (#710).

단일 DB 전환의 병합 원본 세트는 DB 다섯 파일(`graph.db`, `doc_store.db`, `vectors.db`,
`opencrab.db`, `billing.db`)만 든 디렉터리를 다시 백업해 만든다(docs/issue-671-single-db-conversion.md 8.9절 2단계).
정지 상태 백업 세트의 `MANIFEST.json`은 `files`에 세트 아래 모든 파일을 적는다. 삭제 저널 같은 하위 디렉터리의
파일과 심볼릭 링크 항목(`{"symlink": true}`, 크기와 sha256이 없다)도 든다. 모든 항목을 복사하려는 단순 스크립트는
이 항목에서 실패한다. 이 도구는 이름이 정해진 다섯 DB만 복사하고 나머지는 건너뛴 이름만 출력한다.

사용법:
    python scripts/unpack_backup_set.py <세트 디렉터리> <대상 디렉터리>

대상 디렉터리는 없어야 한다(상위 디렉터리는 있어야 한다). 세트는 읽기만 하고 바꾸지 않는다.
복사마다 원본과 사본의 크기와 sha256을 `MANIFEST.json` 값과 대조하고 사본을 fsync한다.

종료 코드: 0 성공. 1 복사나 대조 실패(이 실행이 만든 대상 디렉터리는 지운다).
2 사용법과 사전 점검 실패(MANIFEST 읽기 실패, 다섯 DB 중 항목이나 파일 부재, 대상 디렉터리가 이미 있음).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

_CHUNK = 1 << 20
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _db_names() -> tuple[str, ...]:
    """병합 도구가 정한 DB 이름에 billing.db를 더한 다섯 이름. 이름의 정본은 병합 도구다."""
    path = Path(__file__).resolve().with_name("merge_sqlite_stores.py")
    spec = importlib.util.spec_from_file_location("merge_sqlite_stores", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(sorted((*module.FILES, "billing.db")))


class _UsageError(Exception):
    pass


def _read_manifest(set_dir: Path, names: tuple[str, ...]) -> tuple[dict[str, dict], list[str]]:
    try:
        manifest = json.loads((set_dir / "MANIFEST.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise _UsageError(f"MANIFEST.json를 읽지 못했다: {type(exc).__name__}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        raise _UsageError("MANIFEST.json에 files 항목이 없다")
    wanted: dict[str, dict] = {}
    for name in names:
        entry = files.get(name)
        if (not isinstance(entry, dict) or entry.get("symlink")
                or not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool)
                or not isinstance(entry.get("sha256"), str) or not _HEX64.fullmatch(entry["sha256"])):
            raise _UsageError(f"MANIFEST.json에 {name} 항목이 없거나 크기와 sha256이 없다")
        try:
            info = os.lstat(set_dir / name)
        except OSError as exc:
            raise _UsageError(f"세트에 {name} 파일이 없다") from exc
        if not stat.S_ISREG(info.st_mode):
            raise _UsageError(f"세트의 {name}이 일반 파일이 아니다")
        wanted[name] = entry
    skipped = sorted(key for key in files if key not in wanted)
    return wanted, skipped


def _copy_one(src: Path, dest: Path, entry: dict) -> str:
    """원본을 읽으며 sha256을 계산해 사본을 쓰고 fsync한 뒤 사본을 다시 읽어 대조한다."""
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd_in = os.open(src, flags)
    try:
        fd_out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            while chunk := os.read(fd_in, _CHUNK):
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd_out, view):]
            os.fsync(fd_out)
        finally:
            os.close(fd_out)
    finally:
        os.close(fd_in)
    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
        raise OSError(f"{src.name}: 원본의 크기나 sha256이 MANIFEST와 다르다")
    again = hashlib.sha256()
    with open(dest, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            again.update(chunk)
    if again.hexdigest() != entry["sha256"]:
        raise OSError(f"{src.name}: 사본의 sha256이 MANIFEST와 다르다")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="백업 세트에서 DB 다섯 파일만 sha256 대조해 푼다.")
    parser.add_argument("set_dir", type=Path, help="백업 세트 디렉터리(MANIFEST.json이 있다)")
    parser.add_argument("dest_dir", type=Path, help="새로 만들 대상 디렉터리(없어야 한다)")
    args = parser.parse_args(argv)
    try:
        wanted, skipped = _read_manifest(args.set_dir, _db_names())
        if not args.dest_dir.parent.is_dir():
            raise _UsageError("대상 디렉터리의 상위 디렉터리가 없다")
        try:
            os.mkdir(args.dest_dir, 0o700)
        except FileExistsError as exc:
            raise _UsageError("대상 디렉터리가 이미 있다") from exc
        except OSError as exc:
            raise _UsageError(f"대상 디렉터리를 만들지 못했다: {type(exc).__name__}") from exc
    except _UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        for name, entry in wanted.items():
            sha = _copy_one(args.set_dir / name, args.dest_dir / name, entry)
            print(f"{name} sha256 {sha} OK")
        dir_fd = os.open(args.dest_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        shutil.rmtree(args.dest_dir, ignore_errors=True)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("skipped: " + (", ".join(skipped) if skipped else "(없음)"))
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
