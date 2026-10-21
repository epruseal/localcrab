#!/usr/bin/env python3
"""노드 id가 데이터 디렉터리 아래 파일 경로인 노드를 읽기 전용으로 골라 승인 ID 파일을 만든다.

적재 파이프라인의 중간 산출물 파일이 문서 노드가 된 경우(#520)에 정리 도구의
`--approved-ids` 입력을 만든다. graph.db를 `mode=ro`로만 열고 쓰기와 락을 하지 않는다.
이 도구는 승인하지 않는다. 출력 파일을 사람이 검토한 뒤 정리 도구 dry-run에 넘긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_REFUSED = 2


class SelectionRefusedError(RuntimeError):
    """도구가 파일을 쓰기 전에 중단해야 할 조건을 나타낸다."""


def _prefix_bounds(prefix: str) -> tuple[str, str]:
    """node_id 기본키 범위 조회용 [하한, 상한) 구간이다. 상한은 접두의 마지막 문자를 1 올린 값이다."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def select_ids(data_dir: Path, pack_id: str, node_type: str, basenames: set[str], path_prefix: str | None = None) -> list[str]:
    """graph.db에서 조건에 맞는 node_id를 정렬해 돌려준다.

    조건: node_id가 path_prefix로 시작하고, node_type과 properties.pack_id가 같고,
    파일 이름이 basenames에 든다. path_prefix의 기본값은 데이터 디렉터리와 구분자이다.
    node_id 기본키의 범위 조회이므로 전체 스캔이 아니다.
    """
    prefix = path_prefix if path_prefix is not None else str(data_dir.resolve()) + os.sep
    if not prefix or not basenames:
        raise SelectionRefusedError("a path prefix and at least one basename are required")
    low, high = _prefix_bounds(prefix)
    conn = sqlite3.connect(f"{(data_dir / 'graph.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT node_id,node_type,properties FROM graph_nodes WHERE node_id>=? AND node_id<? ORDER BY node_id",
            (low, high),
        )
        selected = []
        for node_id, row_type, properties in rows:
            if row_type != node_type or os.path.basename(node_id) not in basenames:
                continue
            try:
                row_pack = json.loads(properties).get("pack_id")
            except (TypeError, ValueError, AttributeError):
                continue
            if row_pack == pack_id:
                selected.append(node_id)
        return selected
    finally:
        conn.close()


def write_approved_ids(output: Path, pack_id: str, node_type: str, ids: list[str]) -> str:
    """정리 도구가 읽는 승인 ID 파일을 새로 쓰고 ID 집합의 sha256을 돌려준다. 있는 파일은 덮어쓰지 않는다."""
    body = json.dumps({"pack_id": pack_id, "node_type": node_type, "ids": ids}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SelectionRefusedError("output file already exists") from exc
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    # 정리 도구 manifest의 selection.approved_ids_sha256과 같은 값이다(정렬한 id 목록의 canonical JSON sha256).
    return hashlib.sha256(json.dumps(sorted(ids), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local-data-dir", required=True)
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--node-type", required=True)
    parser.add_argument("--basename", action="append", required=True, help="후보 파일 이름. 여러 번 줄 수 있다.")
    parser.add_argument("--path-prefix", help="기본값은 데이터 디렉터리 경로와 구분자이다.")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir, output = Path(args.local_data_dir), Path(args.output)
    try:
        if output.exists():
            raise SelectionRefusedError("output file already exists")
        ids = select_ids(data_dir, args.pack_id, args.node_type, set(args.basename), args.path_prefix)
        if not ids:
            print("no node matched", file=sys.stderr)
            return EXIT_NO_MATCH
        digest = write_approved_ids(output, args.pack_id, args.node_type, ids)
    except (SelectionRefusedError, sqlite3.Error) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    per_name: dict[str, int] = {}
    for node_id in ids:
        name = os.path.basename(node_id)
        per_name[name] = per_name.get(name, 0) + 1
    print(json.dumps({"count": len(ids), "ids_sha256": digest, "per_basename": per_name}, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
