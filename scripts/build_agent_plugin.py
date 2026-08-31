#!/usr/bin/env python3
"""Agent Plugin 빌드 씬 CLI.

저장소 루트를 스크립트 위치로 자동 탐지하고 packaging/agent-plugin/tools/build.py
의 build() 를 호출한다. 실패 시 비제로 종료.

사용: python scripts/build_agent_plugin.py [--out DIST_DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.build import BuildError, build  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the localcrab Agent Plugin package.")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "dist"),
        help="출력 디렉터리(기본: <repo_root>/dist).",
    )
    args = parser.parse_args()
    out_dir = Path(args.out)

    try:
        staged_root = build(REPO_ROOT, out_dir)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    sidecar_path = out_dir / f"{staged_root.name}.SHA256SUMS"
    print(f"plugin package: {staged_root}")
    print(f"sha256sums:     {sidecar_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
