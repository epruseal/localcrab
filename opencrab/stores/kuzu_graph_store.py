"""
Kùzu/Ladybug 그래프 스토어의 capability facade다. 런타임 패키지는 ladybug
(KùzuDB가 리브랜딩된 이름, https://github.com/LadybugDB/ladybug)이며
Database/Connection API는 kuzu와 동일하지만, transaction owner와 원자적
CAS가 qualification되기 전에는 production 경로에서 접근하지 않는다.
클래스명·STORAGE_MODE="kuzu" 값은 공개 인터페이스 하위호환을 위해 유지한다.

현재 factory는 ``KuzuUnavailableGraphStore``를 반환한다. 이 모듈은 optional
패키지를 import하거나 DB 경로를 만들지 않고, 읽기와 쓰기에 capability 예외를
낸다. qualification 전에는 활성화할 구현이 없다.

요구 버전: ladybug>=0.18. RPi5 aarch64 (CONFIG_PAGE_SIZE_16KB=y) 환경에서
구버전(kuzu 0.11.3)은 buffer manager가 4KB 단위 madvise를 호출해 EINVAL로
조용히 죽었다(LD_PRELOAD=madv_noop.so 우회 필요). 이 버그는
LadybugDB/ladybug#526으로 보고되어 #527("Handle larger OS page sizes in VM
eviction")로 수정되었고 v0.18.0(2026-07-01)에 포함되어, LD_PRELOAD 우회 없이
동작한다.
"""

from __future__ import annotations

import re
from typing import Any

from opencrab.common.graph_identity import (
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphWriteCapabilityUnavailable,
)
from opencrab.stores._json import parse_props as _parse  # noqa: F401 - compatibility export


class KuzuGraphStore:
    """Disabled direct constructor until an atomic Ladybug writer exists."""

    def __init__(self, db_path: str, buffer_pool_size: int = 256 * 1024 * 1024) -> None:
        # Keep this before any optional import, filesystem access, or driver
        # capability probe.  A missing transaction owner is an unavailable
        # write path, never a best-effort fallback.
        raise GraphWriteCapabilityUnavailable("graph write capability unavailable")

    @staticmethod
    def _query_has_write(query: str) -> bool:
        if not isinstance(query, str):
            return True
        code = re.sub(r"(?s)/\*.*?\*/|//[^\n]*|--[^\n]*|(['\"]).*?\1", " ", query)
        return bool(re.search(
            r"\b(?:CREATE|MERGE|SET|REMOVE|DELETE|DETACH|DROP|ALTER|COPY|LOAD|FOREACH|CALL|BEGIN|COMMIT|ROLLBACK|USE)\b|;",
            code,
            re.I,
        ))

    def run_cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._query_has_write(query):
            raise GraphQueryWriteRejected("graph query write rejected")
        raise GraphReadCapabilityUnavailable("graph read capability unavailable")

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        """Keep the historical class surface without enabling a read path."""
        raise GraphReadCapabilityUnavailable("graph read capability unavailable")


class KuzuUnavailableGraphStore:
    """Zero-access production facade for the unqualified Kùzu backend."""

    _WRITE_NAMES = frozenset({
        "upsert_node", "update_node", "upsert_nodes_batch", "update_nodes_batch",
        "reclassify_node", "migrate_graph_identity",
        "upsert_edge", "update_edge", "upsert_edges_batch", "update_edges_batch",
        "delete_node", "delete_edge", "backfill_pack_provenance", "ensure_constraints",
        "hydrate_evidence",
    })

    def inspect_graph_identity(self):
        """Inventory is unavailable until the Ladybug read capability is qualified."""
        raise GraphReadCapabilityUnavailable("graph read capability unavailable")

    def __init__(self, db_path: str | None = None) -> None:
        # Retain the configured location for diagnostics and compatibility
        # with callers that inspect store paths. Merely storing a string does
        # not import Ladybug or touch the filesystem.
        self._db_path = db_path

    @property
    def available(self) -> bool:
        return False

    @property
    def schema_state(self) -> str:
        return "disabled"

    def close(self) -> None:
        return None

    def ping(self) -> bool:
        return False

    def run_cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if KuzuGraphStore._query_has_write(query):
            raise GraphQueryWriteRejected("graph query write rejected")
        raise GraphReadCapabilityUnavailable("graph read capability unavailable")

    def __getattr__(self, name: str):
        exc = GraphWriteCapabilityUnavailable if name in self._WRITE_NAMES else GraphReadCapabilityUnavailable
        def unavailable(*args: Any, **kwargs: Any):
            raise exc("graph write capability unavailable" if exc is GraphWriteCapabilityUnavailable else "graph read capability unavailable")
        return unavailable
