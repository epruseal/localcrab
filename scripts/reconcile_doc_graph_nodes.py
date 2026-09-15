#!/usr/bin/env python3
"""그래프-문서 스토어 노드 키 집합을 진단하고 additive 하게 치유한다 (#317).

PR #316(#55)이 ontology_get_node/ontology_list_nodes 를 그래프 스토어 하나로만
읽게 고정한 뒤, 문서 스토어에만 있는 노드는 어떤 조회 표면에도 보이지 않게
됐다. 이 도구는 두 스토어의 노드 키 집합을 진단하고, 결정된 방향으로만
치유한다: 그래프 전용 노드는 기본 --apply 로 문서에 역채움하고, 문서 전용
노드는 --apply --promote-doc-only 를 명시해야만 그래프로 승격한다.

이 도구가 약속하지 않는 것: 엣지 재조정, 벡터 슬롯 재조정(#305), 비문자열
pack_id 자체의 정합(#222/#226, 다만 그 값이 죽지 않고 치유 불가로 분류되는
것은 책임진다), 자동 롤백(백업 세트로부터의 복원은 운영자의 수동 절차).

## 진단 (3절)

두 스토어에서 각각 (node_id -> node_type) 매핑을 얻어 먼저 해당하는 범주부터
순서대로 분류한다: 문서 전용, 그래프 전용, 양쪽 존재/타입 일치(정상 대응,
대조군, 어떤 목록에도 넣지 않는다), 양쪽 존재/타입 불일치(정체성 충돌). 문서
스토어의 기본키는 (space, node_id) 복합키이므로 같은 node_id 가 서로 다른
space 로 중복 존재할 수 있다. 그래프에 대응 행이 있고 그중 하나라도 그래프의
space 와 다르면 정체성 충돌로, 그래프에 대응 행이 없으면 치유 불가로
분류한다(어느 space 행을 승격해야 하는지 판단할 근거가 없다). 이 조건에
해당하는 node_id 건수는 진단이 끝난 뒤 하나의 정수로 집계해 요약에 노출한다
(0 건이어도 명시적으로 0 을 찍는다 -- 줄이 없는 것과 0 이 찍힌 것은 다른
뜻이다).

## 승격 입력 계약 (4-2절, 쟁점1)

문서 전용 노드 승격은 예약 키/id 불일치 같은 조건을 손으로 재검사하지 않고
opencrab.common.graph_identity.prepare_node() 를 직접 호출해 그 결과를
그대로 신뢰한다. 이 호출에서 나는 예외는 GraphPropertyValidationError 하나로
좁히지 않고 Exception 전체를 잡는다(KeyboardInterrupt/SystemExit 는 그대로
전파한다) -- prepare_node() 가 내부적으로 부르는 canonical_json_bytes() 는
CPython 의 정수-문자열 변환 자릿수 제한처럼 GraphPropertyValidationError
밖의 예외도 던질 수 있고, 손 재구현 사전검사를 두지 않기로 한 취지 자체가
"이 호출 지점 하나를 신뢰한다"는 것이므로 예외 타입을 좁히면 그 취지가
무너진다. prepare_node() 가 검사하지 않는 유일한 gap(pack_id 가 문자열인지)
은 이 호출 이전에 좁게 별도로 검사한다 -- normalize_edge_properties() 가
엣지의 pack_id 에 이미 적용하는 조건을 노드 쪽에 옮긴 것뿐이다.

## 경쟁 조건과 additive-only 보장 (5-2절)

그래프 전용 역채움은 create_node_doc_if_absent()(ON CONFLICT ... DO
NOTHING)를 쓴다: upsert_node_doc() 은 무조건 덮어쓰기라서, 진단 이후 다른
프로세스가 만든 문서 행을 이 도구가 재구성한 옛 속성으로 덮어써 데이터를
잃을 수 있다. 문서 전용 승격은 그래프 쪽 upsert_node() 의 기존 삽입-후-무시
계약을 그대로 쓴다(이미 이 계약을 쓰므로 추가 작업이 필요 없다). 두 방향
모두 진단 시점과 적용 시점 사이의 변경을 덮어쓰지 않고 "동시 변경으로
건너뜀"으로 보고한다.

## FTS 백필과 무변경 약속의 정확한 범위 (5-1절, 쟁점3)

LocalSQLDocStore.__init__ 은 doc_sources_fts 색인 백필(doc_nodes/graph_nodes
와 무관, doc_sources 만 대상)을 조건부(n_fts == 0 and n_src > 0)로 수행할 수
있다. 이 도구가 실제로 보장하는 것은 "이 도구 직전의 초기화가 그 백필을 이미
채운 스토어에는 백필이 다시 일어나지 않는다"이며, 이 도구 자신은 어느
경우든 doc_nodes/graph_nodes 에 어떤 쓰기도 하지 않는다(진단 실행, --apply
없이).

## 알려진 한계 (3절, 투명 공개, 이 이슈의 수정 대상이 아님)

그래프 쪽 진단은 기존 inspect_graph_identity() 를 그대로 쓴다. 이 메서드는
graph_nodes/graph_edges 전 테이블을 LIMIT 없이 한 번에 메모리에 올리는 기존
계약(마이그레이션 계획 수립 등 다른 호출자가 이미 쓴다)을 그대로 물려받는다.
그래프 쪽 노드 수가 매우 크면 이 진단 단계에서 그래프 쪽 메모리 사용량이
커질 수 있다. 문서 쪽은 iter_node_identities() 로 이 문제를 해소하지만,
그래프 쪽 해소는 이 공유 마이그레이션 인프라를 바꾸는 별도 작업이 필요하므로
범위 밖이다.

EXIT CODES:
    0 성공(치유 불가/건너뜀 행이 있어도 도구 자체는 정상 종료), 2 사용법 오류,
    3 재조정 자체가 거부됨(그래프 schema_state 가 legacy/partial, apply
    시점의 fresh, 또는 GraphReadCapabilityUnavailable), 4 백업 실패, 5 실행
    기록 파일 쓰기 실패(이미 끝난 스토어 쓰기는 되돌리지 않는다).

Usage:
    python scripts/reconcile_doc_graph_nodes.py --local-data-dir /path/to/data
    python scripts/reconcile_doc_graph_nodes.py --local-data-dir ... --apply \\
        --backup-to /path/to/backup-dest
    python scripts/reconcile_doc_graph_nodes.py --local-data-dir ... --apply \\
        --promote-doc-only --backup-to /path/to/backup-dest --record-to run.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opencrab.common.graph_identity import (
    GraphReadCapabilityUnavailable,
    GraphSchemaMigrationRequired,
    NodeIdentityConflict,
    prepare_node,
    thaw_json,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_BACKUP = 4
EXIT_RECORD_WRITE_FAILED = 5

# 치유 불가 사유 코드 (5-6절 하위 사유별 집계에 쓰는 정본 문자열).
REASON_SPACE_ID_NONE = "space_id_none"
REASON_PROPERTY_ERROR = "property_error"
REASON_NORMALIZATION_ISSUE = "normalization_issue"
REASON_NODE_TYPE_MISSING = "node_type_missing_or_non_string"
REASON_PACK_ID_NON_STRING = "pack_id_non_string"
REASON_PROMOTION_REJECTED = "promotion_rejected"
REASON_PROMOTION_REJECTED_AT_APPLY = "promotion_rejected_at_apply"
REASON_DUP_SPACE_NO_GRAPH = "duplicate_space_no_graph_match"


@dataclass
class GraphOnlyRow:
    node_id: str
    node_type: str
    space_id: str | None
    pack_id: str | None
    healable: bool
    reason: str | None = None
    raw_row: Any = None
    """진단 시점에 이미 읽어 둔 ``LegacyNodeRow``. 치유는 이 값의
    ``normalized_properties`` 를 그대로 쓰고(4-1절), 별도로 다시 조회하지
    않는다(6절, "치유 대상 소수 행에 대해서만 기존 단건 조회 메서드를
    그대로 쓴다" -- 그래프 쪽은 이미 진단이 들고 있는 이 값 자체가 그
    단건 조회에 해당한다)."""


@dataclass
class DocOnlyRow:
    node_id: str
    node_type: Any
    space: str | None
    healable: bool
    reason: str | None = None


@dataclass
class ConflictRow:
    node_id: str
    reason: str
    graph_space: str | None = None
    doc_spaces: tuple[str, ...] = ()


@dataclass
class HealResult:
    node_id: str
    space: str | None
    direction: Literal["backfill", "promotion"]
    outcome: Literal["healed", "skipped_exists", "skipped_conflict", "skipped_rejected"]
    reason: str | None = None


@dataclass
class DiagnosisReport:
    schema_state: str
    graph_only: list[GraphOnlyRow] = field(default_factory=list)
    doc_only: list[DocOnlyRow] = field(default_factory=list)
    conflicts: list[ConflictRow] = field(default_factory=list)
    duplicate_space_count: int = 0

    def graph_only_healable(self) -> list[GraphOnlyRow]:
        return [r for r in self.graph_only if r.healable]

    def doc_only_healable(self) -> list[DocOnlyRow]:
        return [r for r in self.doc_only if r.healable]


class ReconciliationRejectedError(RuntimeError):
    """그래프 백엔드/schema_state 가 재조정 자체를 거부할 때."""


# ---------------------------------------------------------------------------
# 진단 (3절)
# ---------------------------------------------------------------------------


def diagnose(graph_store: Any, doc_store: Any) -> DiagnosisReport:
    """두 스토어의 노드 키 집합을 대사해 :class:`DiagnosisReport` 를 만든다.

    이 함수는 어떤 스토어에도 쓰기를 하지 않는다. 문서 전용 후보의 승격
    가능 여부(4-2절)까지 여기서 미리 판정하는 이유는, --apply 없이도
    "치유 불가" 건수가 정확해야 한다는 이 도구의 존재 이유(2-4절, 3절)
    때문이다.
    """
    inventory = graph_store.inspect_graph_identity()
    schema_state = inventory.schema_state
    if schema_state in ("legacy", "partial"):
        raise ReconciliationRejectedError(
            f"schema_state={schema_state!r} 에서는 node_id 전역 유일성 전제가 "
            "성립하지 않을 수 있어 재조정을 거부한다."
        )

    graph_by_id: dict[str, Any] = {row.key.node_id: row for row in inventory.nodes}

    doc_by_id: dict[str, list[tuple[str, str]]] = {}
    for space, node_id, node_type in doc_store.iter_node_identities():
        doc_by_id.setdefault(node_id, []).append((space, node_type))

    report = DiagnosisReport(schema_state=schema_state)
    all_ids = set(graph_by_id) | set(doc_by_id)

    for node_id in all_ids:
        graph_row = graph_by_id.get(node_id)
        doc_rows = doc_by_id.get(node_id)

        if graph_row is not None and doc_rows is None:
            _classify_graph_only(node_id, graph_row, report)
        elif graph_row is None and doc_rows is not None:
            _classify_doc_only(node_id, doc_rows, doc_store, report)
        else:
            _classify_both(node_id, graph_row, doc_rows, report)

    return report


def _classify_graph_only(node_id: str, graph_row: Any, report: DiagnosisReport) -> None:
    pack_id = graph_row.pack_id
    if graph_row.property_error is not None:
        report.graph_only.append(
            GraphOnlyRow(
                node_id, graph_row.key.node_type, graph_row.space_id, pack_id, False, REASON_PROPERTY_ERROR, graph_row
            )
        )
        return
    if graph_row.normalization_issues:
        report.graph_only.append(
            GraphOnlyRow(
                node_id,
                graph_row.key.node_type,
                graph_row.space_id,
                pack_id,
                False,
                REASON_NORMALIZATION_ISSUE,
                graph_row,
            )
        )
        return
    if graph_row.space_id is None:
        report.graph_only.append(
            GraphOnlyRow(node_id, graph_row.key.node_type, None, pack_id, False, REASON_SPACE_ID_NONE, graph_row)
        )
        return
    report.graph_only.append(
        GraphOnlyRow(node_id, graph_row.key.node_type, graph_row.space_id, pack_id, True, None, graph_row)
    )


def _classify_doc_only(
    node_id: str, doc_rows: list[tuple[str, str]], doc_store: Any, report: DiagnosisReport
) -> None:
    if len(doc_rows) > 1:
        report.duplicate_space_count += 1
        report.doc_only.append(DocOnlyRow(node_id, None, None, False, REASON_DUP_SPACE_NO_GRAPH))
        return

    space, node_type = doc_rows[0]
    if not isinstance(node_type, str) or not node_type:
        report.doc_only.append(DocOnlyRow(node_id, node_type, space, False, REASON_NODE_TYPE_MISSING))
        return

    doc_full = doc_store.get_node_doc(space, node_id)
    properties = doc_full["properties"] if doc_full is not None else {}
    pack_id = properties.get("pack_id") if isinstance(properties, dict) else None
    if isinstance(properties, dict) and "pack_id" in properties and pack_id is not None and not isinstance(pack_id, str):
        report.doc_only.append(DocOnlyRow(node_id, node_type, space, False, REASON_PACK_ID_NON_STRING))
        return

    try:
        prepare_node(node_type=node_type, node_id=node_id, properties=properties, space_id=space)
    except Exception as exc:  # noqa: BLE001 - 쟁점1: prepare_node() 호출 지점 하나만 신뢰한다.
        reason = f"{REASON_PROMOTION_REJECTED}: {type(exc).__name__}: {exc}"
        report.doc_only.append(DocOnlyRow(node_id, node_type, space, False, reason))
        return

    report.doc_only.append(DocOnlyRow(node_id, node_type, space, True))


def _classify_both(
    node_id: str, graph_row: Any, doc_rows: list[tuple[str, str]], report: DiagnosisReport
) -> None:
    doc_spaces = tuple(space for space, _ in doc_rows)
    mismatched = [space for space, _ in doc_rows if space != graph_row.space_id]
    if mismatched:
        if len(doc_rows) > 1:
            report.duplicate_space_count += 1
        report.conflicts.append(ConflictRow(node_id, "space_mismatch", graph_row.space_id, doc_spaces))
        return

    # (space, node_id) 는 문서 쪽 유니크키이므로 여기 도달하면 doc_rows 는
    # 정확히 한 건이다(모두 graph_row.space_id 와 같아야 하는데, 같은
    # space 로 두 번 존재할 수는 없다).
    _, node_type = doc_rows[0]
    if node_type != graph_row.key.node_type:
        report.conflicts.append(ConflictRow(node_id, "node_type_mismatch", graph_row.space_id, doc_spaces))
        return
    # 정상 대응: 어떤 목록에도 넣지 않는다(대조군).


# ---------------------------------------------------------------------------
# 치유 (4-1절 역채움, 4-2절 승격, 5-2절 경쟁 조건)
# ---------------------------------------------------------------------------


def heal(
    graph_store: Any,
    doc_store: Any,
    report: DiagnosisReport,
    *,
    promote_doc_only: bool,
) -> list[HealResult]:
    """진단이 healable 로 표시한 행만 additive 하게 치유한다.

    호출 직전 schema_state 를 다시 확인하는 것은 이 함수의 책임이 아니라
    호출자(main)의 책임이다(5-3절, "fresh" 거부는 apply 진입 자체를 막는다).
    """
    results: list[HealResult] = []
    for row in report.graph_only_healable():
        results.append(_backfill_one(doc_store, row))
    if promote_doc_only:
        for row in report.doc_only_healable():
            results.append(_promote_one(graph_store, doc_store, row))
    return results


def _backfill_one(doc_store: Any, row: GraphOnlyRow) -> HealResult:
    """그래프 전용 노드를 문서 스토어에 역채움한다 (4-1절).

    진단 단계가 이미 읽어 둔 ``LegacyNodeRow.normalized_properties`` 를 그대로
    쓰고 그래프를 다시 조회하지 않는다(6절, 기존 단건 조회 재사용). 쟁점2:
    ``normalized_properties`` 는 중첩 값을 ``FrozenDict``/``tuple`` 로 얼려
    담으므로 ``dict()`` 얕은 변환이 아니라 ``thaw_json()`` 으로 재귀 복원한다.
    """
    props = thaw_json(row.raw_row.normalized_properties)
    if row.pack_id is not None:
        props["pack_id"] = row.pack_id
    outcome = doc_store.create_node_doc_if_absent(row.space_id, row.node_type, row.node_id, props)
    if outcome == "created":
        return HealResult(row.node_id, row.space_id, "backfill", "healed")
    return HealResult(row.node_id, row.space_id, "backfill", "skipped_exists")


def _promote_one(graph_store: Any, doc_store: Any, row: DocOnlyRow) -> HealResult:
    """문서 전용 노드를 그래프로 승격한다.

    진단 시점에 이미 ``prepare_node()``로 승격 가능 여부를 확인했더라도,
    진단과 적용 사이에 문서 속성이 바뀌면 이 재호출이 새로 예외를 던질 수
    있다(쟁점1과 같은 근거를 적용 지점에도 적용한다). 이 예외를 넓게 잡지
    않으면 한 행의 검증 실패가 나머지 행 전체 처리를 막고 도구를 비정상
    종료시킨다.
    """
    doc_full = doc_store.get_node_doc(row.space, row.node_id)
    properties = doc_full["properties"] if doc_full is not None else {}
    try:
        node_type, props, effective_space, _digest = prepare_node(
            node_type=row.node_type, node_id=row.node_id, properties=properties, space_id=row.space
        )
    except Exception as exc:  # noqa: BLE001 - 쟁점1과 같은 근거를 적용 지점에도 적용한다.
        reason = f"{REASON_PROMOTION_REJECTED_AT_APPLY}: {type(exc).__name__}: {exc}"
        return HealResult(row.node_id, row.space, "promotion", "skipped_rejected", reason)
    try:
        graph_store.upsert_node(node_type, row.node_id, props, effective_space)
    except NodeIdentityConflict:
        return HealResult(row.node_id, row.space, "promotion", "skipped_conflict", "concurrent_graph_write")
    return HealResult(row.node_id, row.space, "promotion", "healed")


# ---------------------------------------------------------------------------
# 실행 기록 파일 (5-5절)
# ---------------------------------------------------------------------------


class RecordWriter:
    """실행 기록을 JSON Lines(원소 하나 = 한 줄)로 즉시 append 한다.

    5-5절이 요구하는 것은 "크래시가 나도 그때까지 무엇을 했는지 아는 것"
    이므로, 전량을 메모리에 모았다가 끝에 한 번에 쓰는 단일 JSON 배열은 이
    요구를 구조적으로 만족하지 못한다(끝에 한 번 쓰다가 죽으면 전체를
    잃는다). 각 행의 쓰기 결과가 확정되는 즉시 파일에 한 줄을 append 하고
    flush+fsync 하는 JSON Lines 형식으로 이 요구를 만족한다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def append(self, result: HealResult) -> None:
        line = json.dumps(
            {
                "node_id": result.node_id,
                "space": result.space,
                "direction": result.direction,
                "outcome": result.outcome,
                "reason": result.reason,
            },
            ensure_ascii=False,
        )
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# 출력 형식 (5-6절)
# ---------------------------------------------------------------------------


def format_report(report: DiagnosisReport, heal_results: list[HealResult] | None = None) -> str:
    lines: list[str] = []
    reason_counts: dict[str, int] = {}
    for row in report.graph_only:
        if not row.healable:
            reason_counts[row.reason or "unknown"] = reason_counts.get(row.reason or "unknown", 0) + 1
    for row in report.doc_only:
        if not row.healable:
            key = (row.reason or "unknown").split(":", 1)[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1

    n_unhealable = sum(1 for r in report.graph_only if not r.healable) + sum(
        1 for r in report.doc_only if not r.healable
    )

    lines.append("=== 재조정 진단 요약 ===")
    lines.append(f"문서 전용: {len(report.doc_only)}건")
    lines.append(f"그래프 전용: {len(report.graph_only)}건 (역채움 가능 {len(report.graph_only_healable())}건)")
    lines.append(f"정체성 충돌: {len(report.conflicts)}건")
    lines.append(f"치유 불가: {n_unhealable}건")
    for reason, count in sorted(reason_counts.items()):
        lines.append(f"  - {reason}: {count}건")
    lines.append(f"node_id 의 space 중복: {report.duplicate_space_count}건")

    if heal_results is not None:
        healed_backfill = sum(1 for r in heal_results if r.direction == "backfill" and r.outcome == "healed")
        healed_promotion = sum(1 for r in heal_results if r.direction == "promotion" and r.outcome == "healed")
        skipped_exists = sum(1 for r in heal_results if r.outcome == "skipped_exists")
        skipped_conflict = sum(1 for r in heal_results if r.outcome == "skipped_conflict")
        skipped_rejected = sum(1 for r in heal_results if r.outcome == "skipped_rejected")
        lines.append("=== 적용 결과 ===")
        lines.append(f"역채움됨: {healed_backfill}건")
        lines.append(f"승격됨: {healed_promotion}건")
        lines.append(f"건너뜀(이미 존재): {skipped_exists}건")
        lines.append(f"건너뜀(동시 충돌): {skipped_conflict}건")
        lines.append(f"건너뜀(적용 거부): {skipped_rejected}건")
        for r in heal_results:
            if r.outcome != "healed":
                lines.append(f"  [{r.direction}] node_id={r.node_id} space={r.space} outcome={r.outcome} reason={r.reason}")

    lines.append("=== 상세 ===")
    for row in report.doc_only:
        lines.append(f"[문서 전용] node_id={row.node_id} space={row.space} healable={row.healable} reason={row.reason}")
    for row in report.graph_only:
        lines.append(
            f"[그래프 전용] node_id={row.node_id} space_id={row.space_id} healable={row.healable} reason={row.reason}"
        )
    for row in report.conflicts:
        lines.append(
            f"[정체성 충돌] node_id={row.node_id} reason={row.reason} graph_space={row.graph_space} doc_spaces={row.doc_spaces}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local-data-dir", default=None, metavar="D", help="local 모드 데이터 디렉터리")
    p.add_argument("--pg-url", default=None, metavar="URL", help="pg 모드 접속 문자열")
    p.add_argument("--storage-mode", choices=["local", "pg"], default=None, help="기본값: --pg-url 유무로 판단")
    p.add_argument("--apply", action="store_true", help="진단만 하지 않고 실제로 치유한다")
    p.add_argument("--promote-doc-only", action="store_true", help="문서 전용 노드를 그래프로 승격한다")
    p.add_argument("--backup-to", default=None, metavar="DEST", help="--apply 전 backup_data_dir() 백업 대상 경로")
    p.add_argument("--skip-backup", action="store_true", help="백업을 명시적으로 생략한다")
    p.add_argument("--record-to", default=None, metavar="PATH", help="실행 기록 파일 경로(기본: 타임스탬프 기반 파일명)")
    return p.parse_args(argv)


def _build_stores(args: argparse.Namespace) -> tuple[Any, Any, str, str]:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_doc_store, make_graph_store

    storage_mode = args.storage_mode or ("pg" if args.pg_url else "local")
    kwargs: dict[str, Any] = {"STORAGE_MODE": storage_mode}
    if args.local_data_dir:
        kwargs["LOCAL_DATA_DIR"] = args.local_data_dir
    if args.pg_url:
        kwargs["POSTGRES_URL"] = args.pg_url
    settings = Settings(**kwargs)
    return make_graph_store(settings), make_doc_store(settings), storage_mode, settings.local_data_dir


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.apply and not (args.backup_to or args.skip_backup):
        print("--apply 는 --backup-to 또는 --skip-backup 을 요구한다.", file=sys.stderr)
        return EXIT_USAGE

    graph_store, doc_store, storage_mode, local_data_dir = _build_stores(args)

    if storage_mode == "pg" and args.backup_to and not args.skip_backup:
        print(
            "STORAGE_MODE=pg 에서는 backup_data_dir() 이 실제 데이터를 보호하지 "
            "않는다 -- --skip-backup 동의가 추가로 필요하다.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        report = diagnose(graph_store, doc_store)
    except ReconciliationRejectedError as exc:
        print(f"재조정 거부: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except GraphReadCapabilityUnavailable as exc:
        print(f"이 백엔드는 재조정을 지원하지 않는다: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except GraphSchemaMigrationRequired as exc:
        print(f"재조정 거부: {exc}", file=sys.stderr)
        return EXIT_REJECTED

    if not args.apply:
        print(format_report(report))
        return EXIT_OK

    if report.schema_state == "fresh":
        print("schema_state=fresh 에서는 --apply 를 거부한다.", file=sys.stderr)
        return EXIT_REJECTED

    if args.backup_to:
        from opencrab.stores.backup import BackupError

        try:
            from opencrab.stores.backup import backup_data_dir

            backup_data_dir(local_data_dir, dest_dir=args.backup_to)
        except BackupError as exc:
            print(f"백업 실패, 어떤 쓰기도 하지 않았다: {exc}", file=sys.stderr)
            return EXIT_BACKUP

    # 5-3절: --apply 직전 schema_state 를 다시 확인한다(진단과 적용 사이의
    # 변경을 잡기 위함, "다시 확인" 이 요구하는 신선한 재조회).
    try:
        recheck = graph_store.inspect_graph_identity()
    except GraphReadCapabilityUnavailable as exc:
        print(f"이 백엔드는 재조정을 지원하지 않는다: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    if recheck.schema_state != "target":
        print(f"schema_state={recheck.schema_state!r} 에서는 적용할 수 없다.", file=sys.stderr)
        return EXIT_REJECTED

    record_path = Path(args.record_to) if args.record_to else Path(f"reconcile_run_{int(time.time())}.jsonl")
    writer = RecordWriter(record_path)
    record_write_failed = False
    heal_results: list[HealResult] = []
    try:
        for row in report.graph_only_healable():
            result = _backfill_one(doc_store, row)
            heal_results.append(result)
            try:
                writer.append(result)
            except OSError as exc:
                record_write_failed = True
                extra = " 이 구성에는 백업도 이 기록도 없다." if storage_mode == "pg" and args.skip_backup else ""
                print(f"실행 기록 파일 쓰기 실패 (이미 완료된 쓰기는 되돌리지 않는다): {exc}.{extra}", file=sys.stderr)
        if args.promote_doc_only:
            for row in report.doc_only_healable():
                result = _promote_one(graph_store, doc_store, row)
                heal_results.append(result)
                try:
                    writer.append(result)
                except OSError as exc:
                    record_write_failed = True
                    extra = " 이 구성에는 백업도 이 기록도 없다." if storage_mode == "pg" and args.skip_backup else ""
                    print(f"실행 기록 파일 쓰기 실패 (이미 완료된 쓰기는 되돌리지 않는다): {exc}.{extra}", file=sys.stderr)
    finally:
        writer.close()

    print(f"실행 기록 파일: {record_path}")
    print(format_report(report, heal_results))
    return EXIT_RECORD_WRITE_FAILED if record_write_failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
