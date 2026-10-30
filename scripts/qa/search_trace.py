"""정제 합성 검색 기록을 검증하고 텍스트 시간선으로 재생한다."""
from __future__ import annotations

import argparse
from pathlib import Path

from opencrab.observability.search_trace import read_trace, timeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        print(timeline(read_trace(args.root, expected_mode="ledger"), expected_mode="ledger"))
    except (ValueError, OSError, KeyError, TypeError):
        print("TRACE: incomplete or invalid")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
