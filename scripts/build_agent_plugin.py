#!/usr/bin/env python3
"""Agent Plugin 빌드 씬 CLI (이슈 #247: 릴리스 세트 checksum + compatibility report).

저장소 루트를 스크립트 위치로 자동 탐지하고 packaging/agent-plugin/tools/build.py 의
build_release() 를 호출해 릴리스 세트(staged 디렉터리 + 패키지 사이드카 + 결정론 아카이브
+ compat report + RELEASE.SHA256SUMS)를 만든다. 실패 시 비제로 종료.

사용:
  python scripts/build_agent_plugin.py [--out DIST_DIR]              # 빌드 + 게시
  python scripts/build_agent_plugin.py --verify [--out DIST_DIR]     # 빌드 없이 검증만
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.build import (  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)
    BuildError,
    build_release,
    verify_release,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/verify the localcrab Agent Plugin release set.")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "dist"),
        help="출력 디렉터리(기본: <repo_root>/dist).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="빌드하지 않고 --out 디렉터리의 기존 릴리스 세트만 검증한다.",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out)

    if args.verify:
        try:
            verify_release(out_dir)
        except BuildError as exc:
            print(f"release verification failed:\n{exc}", file=sys.stderr)
            return 1
        print(f"release verified: {out_dir}")
        return 0

    try:
        staged_root, version = build_release(REPO_ROOT, out_dir)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    # v20 [T1]: 게시된 manifest 를 다시 읽지 않는다. build_release() 가 게시 시 확정한
    # version 을 그대로 받아 쓰므로 이 구간에 파일시스템 판독이 0회이고, 출력 경로는 실제
    # 게시물과 구성적으로 일치한다(사후 대조로 확인하는 방식이 아니다). 재판독하던 판본은
    # 판독 오류·디코딩 오류·구조 손상·형식은 안전하나 불일치하는 version 을 전부 떠안았다.
    sidecar_path = out_dir / f"{staged_root.name}.SHA256SUMS"
    archive_path = out_dir / f"localcrab-plugin-{version}.tar.gz"
    report_path = out_dir / f"localcrab-plugin-{version}.COMPATIBILITY.md"
    release_path = out_dir / f"localcrab-plugin-{version}.RELEASE.SHA256SUMS"
    print(f"plugin package: {staged_root}")
    print(f"sha256sums:     {sidecar_path}")
    print(f"archive:        {archive_path}")
    print(f"compat report:  {report_path}")
    print(f"release sums:   {release_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
