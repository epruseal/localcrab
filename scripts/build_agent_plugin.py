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
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.build import BuildError, build_release, verify_release  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)


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
        staged_root = build_release(REPO_ROOT, out_dir)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    sidecar_path = out_dir / f"{staged_root.name}.SHA256SUMS"
    print(f"plugin package: {staged_root}")
    print(f"sha256sums:     {sidecar_path}")

    manifest = json.loads((staged_root / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    archive_path = out_dir / f"localcrab-plugin-{version}.tar.gz"
    report_path = out_dir / f"localcrab-plugin-{version}.COMPATIBILITY.md"
    release_path = out_dir / f"localcrab-plugin-{version}.RELEASE.SHA256SUMS"
    print(f"archive:        {archive_path}")
    print(f"compat report:  {report_path}")
    print(f"release sums:   {release_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
