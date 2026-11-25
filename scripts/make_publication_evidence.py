#!/usr/bin/env python3
"""병합 입력 백업 세트의 게시 증거 파일(13개 키)을 만든다 (#710).

`scripts/merge_sqlite_stores.py`의 게시 모드(`--evidence-file`)가 요구하는 증거 파일을
세트의 `MANIFEST.json`에서 만든다. 키와 규칙은 docs/issue-671-single-db-conversion.md 8.2절이 정한다.
규칙 판정은 병합 도구의 `_publication_evidence`가 정본이다. 이 도구는 만든 증거를 그 함수로 검사하고
통과하지 못하면 파일을 만들지 않는다.

사용법:
    python scripts/make_publication_evidence.py <세트 디렉터리> --out <증거 파일> \\
        --preparer-id <이름> --frozen-from <UTC 시각> --packs-checked-at <UTC 시각> \\
        --frozen-until <UTC 시각>

시각은 오프셋이 명시된 UTC ISO 문자열이다(예: 2026-01-01T00:00:00+00:00). 동결 구간은 세트 백업의
잠금 구간(`lock.acquired_at` 부터 `lock.released_at`)을 품어야 하고 팩 확인 시각도 그 안에 있어야 한다.
`writer_stopped`, `billing_same_snapshot`은 참으로, `packs_observation`은 `absent`로 고정한다.
이 값은 운영자가 정지 상태를 실제로 확인한 뒤에만 이 도구를 돌린다는 전제의 선언이다.
출력 파일이 이미 있으면 덮어쓰지 않고 실패한다.

종료 코드: 0 성공. 2 입력 오류나 병합 도구의 증거 규칙 위반(출력 파일을 만들지 않는다).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def _merge_module():
    path = Path(__file__).resolve().with_name("merge_sqlite_stores.py")
    spec = importlib.util.spec_from_file_location("merge_sqlite_stores", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_inside(path: Path, directory: Path) -> bool:
    target = Path(os.path.realpath(path))
    root = Path(os.path.realpath(directory))
    return target == root or root in target.parents


def build_evidence(manifest_raw: bytes, args: argparse.Namespace) -> dict:
    manifest = json.loads(manifest_raw)
    if not isinstance(manifest, dict):
        raise ValueError("MANIFEST.json의 최상위가 객체가 아니다")
    lock = manifest.get("lock")
    if not isinstance(lock, dict) or "acquired_at" not in lock or "released_at" not in lock:
        raise ValueError("MANIFEST.json에 lock 구간이 없다(정지 상태 백업 세트가 아니다)")
    return {
        "version": 1,
        "snapshot_id": manifest.get("set_id"),
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "preparer_id": args.preparer_id,
        "frozen_from": args.frozen_from,
        "frozen_until": args.frozen_until,
        "writer_stopped": True,
        "billing_same_snapshot": True,
        "packs_observation": "absent",
        "backup_at": manifest.get("created_at"),
        "packs_checked_at": args.packs_checked_at,
        "capture_from": lock["acquired_at"],
        "capture_until": lock["released_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="병합 입력 세트의 게시 증거 파일을 만든다.")
    parser.add_argument("set_dir", type=Path, help="백업 세트 디렉터리(MANIFEST.json이 있다)")
    parser.add_argument("--out", type=Path, required=True, help="만들 증거 파일(없어야 한다)")
    parser.add_argument("--preparer-id", required=True, help="증거를 만든 사람이나 절차의 이름")
    parser.add_argument("--frozen-from", required=True, help="쓰기 정지 시작(UTC)")
    parser.add_argument("--packs-checked-at", required=True, help="팩 부재 확인 시각(UTC)")
    parser.add_argument("--frozen-until", required=True, help="쓰기 정지 끝(UTC)")
    args = parser.parse_args(argv)
    try:
        if _is_inside(args.out, args.set_dir):
            raise ValueError("--out은 백업 세트 디렉터리 안에 둘 수 없다(병합 도구가 목록 밖 파일을 거부한다)")
        manifest_raw = (args.set_dir / "MANIFEST.json").read_bytes()
        evidence = build_evidence(manifest_raw, args)
        evidence_raw = (json.dumps(evidence, indent=2) + "\n").encode()
        merge = _merge_module()
        merge._publication_evidence(manifest_raw, evidence_raw)
        # 임시 파일에 쓰고 링크해서 도중 실패가 부분 파일을 남기지 않게 하고 기존 파일을 덮어쓰지 않는다.
        partial = args.out.with_name(args.out.name + ".partial")
        created = False
        try:
            with open(partial, "xb") as handle:
                created = True  # 이 실행이 만든 파일만 지운다(남의 .partial은 건드리지 않는다).
                handle.write(evidence_raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(partial, args.out)
        finally:
            if created:
                os.unlink(partial)
    except (OSError, ValueError, RuntimeError) as exc:
        # MergeError와 decode_binding_json의 오류는 RuntimeError, JSON 오류는 ValueError다.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"evidence written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
