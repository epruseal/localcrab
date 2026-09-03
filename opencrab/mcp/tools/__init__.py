"""
MCP Tool Definitions for OpenCrab / LocalCrab.

Each tool is a plain function decorated with ``@tool(name, schema)``
(see ``_registry.py``), which registers it into a single source-of-truth
registry. ``TOOLS`` / ``TOOL_SCHEMAS`` / ``_TOOL_FUNCTIONS`` / ``dispatch_tool``
/ ``UnknownToolError`` are all derived from that registry at import time.

Exposed tools: the single source of truth is the ``@tool`` registrations in
the handler submodules (graph.py / query.py / pack.py / schema.py /
harness.py / catalog.py), pinned by the golden snapshot in
tests/test_tool_registry_contract.py. Enumerating them here rotted twice
(#146, #201), so this docstring deliberately carries no count or list —
query a live server via tools/list or the ``tool_search`` tool (#135).

── R9 패키지 분해 완료 (Stage 10 — 물리 분할 컷오버) ─────────────────────────
핸들러 본체는 이제 이 파일이 아니라 graph.py / query.py / pack.py / schema.py /
harness.py / catalog.py 서브모듈에 물리적으로 산다. 이 파일(``__init__.py``)은
공유 플러밍(``_get_context`` / ``_context`` 딕셔너리와 그 초기화 락 /
``_clean_str`` / ``_clean_meta`` / 락 헬퍼 / ``WRITE_TOOLS`` / BillingHooks
배선)만 소유하고,
서브모듈 임포트로 ``@tool`` 데코레이터를 실행시켜 등록을 트리거한 뒤, 모든
공개·과거-임포트 가능 이름을 재노출한다.

**mock.patch 네임스페이스 바인딩**: ``patch("opencrab.mcp.tools.<name>")``은
"그 이름이 물리적으로 정의된 모듈"이 아니라 "그 이름을 속성으로 가진 모듈
객체"(여기서는 이 패키지 자체)를 몽키패치한다. 서브모듈의 핸들러 함수 바디가
``_get_context``/``_clean_str``/``_clean_meta``/``content_pack_list`` 등을
호출할 때 모듈 최상단에서 미리 임포트해두면, 그 이름은 서브모듈 자신의
(패치 불가능한) 전역에 영구 바인딩되어버려 이 패키지 속성에 걸린 패치를 보지
못한다. 그래서 각 핸들러 바디는 함수 스코프에서
``from opencrab.mcp.tools import _get_context, ...`` 형태로 호출 시점마다
새로 임포트한다 — 이 패키지 모듈 객체의 "현재" 속성(패치되어 있으면 패치된
Mock)을 읽으므로 패치가 그대로 유효하다. 49개의 기존 ``_get_context`` 패치와
``content_pack_list`` 패치 8개(tests/test_mcp.py ×2, tests/
test_tools_handlers_direct.py ×6) 모두 이 방식으로 무변경 유지된다(직접
재현·확인함).

**chroma.lock 소유(#140)**: 이 계층은 더 이상 ``chroma.lock`` 을 잡지 않는다.
잡는 주체가 MCP 하나뿐이면 REST·CLI·마이그레이션이 같은 persist 경로를 잠금 없이
열어 배제가 성립하지 않았다. 이제 로컬 모드 ``ChromaStore`` 인스턴스가 자기 수명
동안 자기 핸들로 보유하므로, ``make_vector_store`` 를 지나는 모든 진입점이 자동으로
편입된다.

**컨텍스트 초기화 직렬화(#192)**: ``_get_context`` 는 최초 호출에서만 스토어를
만들고, 그 초기화를 모듈 레벨 ``_context_init_lock`` 으로 직렬화한다. 락이 없으면
두 스레드가 동시에 최초 호출할 때 각자 팩토리를 실행해 같은 백엔드를 가리키는
인스턴스가 2벌 생기고, 어댑터의 인스턴스 레벨 락이 그 둘 사이에서는 아무것도
직렬화하지 못한다. 프로세스 **간** 초기화 경합은 여기 소관이 아니라
``write.lock`` / ``chroma.lock`` 소관이다(#140, #141).

**TOOLS 등록 순서**: 골든 스냅샷(tests/test_tool_registry_contract.py) 순서는
그래프→쿼리→그래프→쿼리→팩→스키마→팩→하네스로 모듈 경계를 넘나들며
인터리빙되어 있어, 단순 "임포트 순서 × 파일 내 정의 순서"로는 재현 불가능하다
(각 모듈은 임포트 시 통째로 top-to-bottom 실행되므로). 대신 ``_registry.tool()``
에 명시적 ``order=`` 인자를 추가해 각 핸들러가 자신의 골든 인덱스를 직접
선언한다 — 물리적 파일 위치·임포트 순서와 무관하게 TOOLS 출력 순서를 보장한다.

query_bm25, ontology_rebac_check, ontology_extract, ontology_ingest,
workflow_create_run/advance, approval_request, identity_*(5),
canonicalize_*(2), promotion_*(4), billing_*(2) — 실사용 이력 0 / MCP
비노출이던 휴면 코드는 삭제됨(git history에 보존). 필요 시 git log로 복원.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from opencrab.locking import file_lock, lock_data_dir

from ._registry import _REGISTRY, build_tools
from ._registry import AccessTier as AccessTier
from ._registry import ForbiddenArgumentError as ForbiddenArgumentError
from ._registry import UnknownToolError as UnknownToolError
from ._registry import dispatch_tool as _registry_dispatch_tool
from ._registry import tools_for_principal as tools_for_principal

logger = logging.getLogger(__name__)

# chroma PersistentClient는 chromadb 공식상 단일 프로세스 전용이다("not process-safe for
# concurrent writers sharing the same local persistence path"; thread-safe도 단일 프로세스 내에서만
# 보증). 즉 여러 serve가 같은 persist 경로를 공유하는 것은 공식 미지원이며, chroma.lock/
# write.lock 은 그 위에 opencrab이 직접 얹은 커스텀 안전층이다(공식 보증 아님). 정공법은 단일
# `chroma run` 서버 + HttpClient. 출처: cookbook.chromadb.dev/core/{system_constraints,clients}.
#
# #140: chroma.lock 의 공유 락(LOCK_SH)은 더 이상 이 계층이 잡지 않는다. 잡는 주체가 MCP
# 하나뿐이면 REST·CLI·마이그레이션이 같은 persist 경로를 잠금 없이 열어 배제가 성립하지
# 않았다. 이제 ChromaStore 가 로컬 모드 인스턴스 수명 동안 자기 핸들로 보유하므로,
# make_vector_store 를 지나는 모든 진입점이 자동으로 편입된다.


def _lock_data_dir() -> str:
    """락 파일(chroma.lock/write.lock)을 둘 데이터 디렉터리 경로.

    os.environ.get() 직독을 get_settings() 보다 우선한다 — get_settings()는
    lru_cache라 테스트가 실행 도중 monkeypatch한 LOCAL_DATA_DIR을 못 보는 stale
    캐시 문제가 있다(env 직독은 매 호출 즉시 반영). 환경변수 미설정 시에만
    get_settings().local_data_dir(HOME 파생 기본값 포함)로 폴백한다.

    CI 러너처럼 .env가 없어 기본 디렉터리가 실제로 아직 존재하지 않는 경우를
    대비해, 반환 전 os.makedirs(exist_ok=True)로 생성을 보장한다(락 파일 open()이
    FileNotFoundError로 죽는 것을 방지).
    """
    return lock_data_dir()


# WRITE_TOOLS (names of tools that mutate the stores) is computed further down,
# once every handler submodule has registered via @tool(..., writes=True) — see
# that assembly for the full rationale of *why* write tools need serialising.


@contextmanager
def _write_lock():
    """Hold an exclusive cross-process lock for the duration of a write tool.

    LOCK ORDER (#140): chroma.lock comes before write.lock anywhere both are
    taken, because a chroma-backed server holds chroma.lock for its whole
    lifetime and takes write.lock repeatedly inside it.

    dispatch_tool inverts that on one path: the FIRST write tool call builds
    the context inside this lock, and building it takes chroma.lock. The
    inversion is bounded, not removed -- the chroma side times out
    (CHROMA_LOCK_TIMEOUT) and releases this lock, and the migration's own
    write.lock wait is bounded too, so neither side hangs. See
    scripts/migrate_to_local.py:_migrate_vectors_locked for why removing it
    structurally is a follow-up rather than part of this change.
    """
    with file_lock("write.lock", _lock_data_dir()):
        yield


def _clean_str(s: str) -> str:
    """Strip surrogate characters introduced by Windows MCP pipeline encoding."""
    if not isinstance(s, str):
        return str(s)
    return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize metadata dict — remove surrogates from string values."""
    result: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, str):
            result[_clean_str(k)] = _clean_str(v)
        elif isinstance(v, dict):
            result[_clean_str(k)] = _clean_meta(v)
        else:
            result[_clean_str(k)] = v
    return result


# ---------------------------------------------------------------------------
# Store / engine singletons (lazily initialised)
# ---------------------------------------------------------------------------
# These are populated by _get_context() which is called on first tool use.
# This design avoids importing heavy dependencies at module load time.

def _current_read_scope(ctx: dict[str, Any]) -> frozenset[str]:
    """The calling principal's readable pack set, for this request (#147).

    One line, but it lives here rather than being inlined in each handler
    for the same reason ``_get_context`` does: every read tool must derive
    its filter from the SAME place, and a handler that grew its own
    variation is how "this one entry point forgot to scope" happens.

    Deliberately not cached. Scope is per principal and reflects pack
    visibility at call time; a stale entry would keep serving a pack after
    it was un-published.

    Exceptions propagate. A handler that cannot determine its scope must
    fail its call -- there is no safe default here, since the two
    candidates are "show nothing" (hides the caller's own data behind what
    reads as a permission decision) and "show everything" (the fail-open
    this whole execution exists to close).
    """
    from opencrab.auth import current_principal
    from opencrab.pack.read_scope import read_scope

    return read_scope(ctx["sql"], current_principal())


_context: dict[str, Any] = {}

# Serialises the *initialisation* of _context (#192). Without it, two threads
# making the very first call both pass the `if _context:` check and each run
# every store factory, leaving the process with two store instances per
# backend. Each adapter's instance-level lock is per instance, so duplicated
# instances serialise nothing against each other -- which is the root cause
# the chroma-specific collection-key lock (#186) was defending a symptom of.
#
# Scope: threads within one process. Cross-process initialisation is a
# separate concern owned by write.lock / chroma.lock (#140, #141).
_context_init_lock = threading.Lock()

# The thread currently running the initialisation body, or None. Only ever
# set while _context_init_lock is held, so "this marker is me" is equivalent
# to "I hold the lock" -- which is what lets _build_context() below refuse a
# caller that bypassed the lock.
#
# It holds the Thread object rather than its ident because the OS reuses a
# finished thread's ident, and a reused ident would let an unrelated thread
# read as the owner. Object identity never collides that way.
#
# As a re-entry tripwire it catches SAME-THREAD re-entry only: a factory that
# calls back into _get_context() on its own thread. No factory does that today
# (verified across opencrab/), but a plain Lock would turn a future re-entry
# into a silent deadlock, and an RLock would let the re-entrant call run the
# factories a second time -- recreating the very duplication this fix exists
# to remove. Failing fast is the only option that stays diagnosable.
#
# LIMIT: a factory that hands the work to a CHILD thread and waits for it
# still deadlocks. The child has a different identity, so it passes the guard
# and then parks on the lock the parent is holding while it waits. Detecting
# that needs initialisation ownership tracked per logical task rather than per
# thread, which is far more machinery than this defect warrants. Such a
# factory must be fixed on the factory side.
_context_init_owner: threading.Thread | None = None


def _close_all(stores: list[Any]) -> None:
    """Close every store that was built, newest first, swallowing close errors.

    Used only on the initialisation failure path: the caller is already
    unwinding an exception that describes what actually went wrong, so a
    secondary failure while tidying up must not replace it.
    """
    for store in reversed(stores):
        close = getattr(store, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to close %s during context cleanup: %s",
                           type(store).__name__, exc)


def _get_context() -> dict[str, Any]:
    """Lazily initialise LocalCrab stores and engines using the local factory.

    Initialisation is serialised so that concurrent first callers share one
    set of store instances (#192). The fast path below reads ``_context``
    without the lock; that is safe under CPython, where a global read and a
    global rebinding each execute atomically under the GIL, and the fully
    built dictionary is published in a single assignment at the end (never
    filled in place, which would expose a half-populated mapping to a
    lock-free reader). On a free-threading build the fast path would have to
    move inside the lock.
    """
    global _context, _context_init_owner
    if _context:
        return _context

    if _context_init_owner is threading.current_thread():
        raise RuntimeError(
            "reentrant _get_context() call: a store factory called back into "
            "the context initialiser on the same thread, which cannot complete "
            "while it is still running. Break the cycle in the factory."
        )

    with _context_init_lock:
        # Another thread may have finished initialising while this one waited.
        if _context:
            return _context
        try:
            # Claimed inside the try, so that an exception raised between the
            # claim and the body -- an asynchronous KeyboardInterrupt, say --
            # still clears the marker. A stale marker would let a later thread
            # that reuses this one's identity walk straight past
            # _build_context()'s ownership check.
            _context_init_owner = threading.current_thread()
            return _build_context()
        finally:
            _context_init_owner = None


def _build_context() -> dict[str, Any]:
    """Build and publish the store/engine context. The caller holds the lock.

    Split out only so the initialisation body keeps its original indentation
    and stays readable; it is not a separate entry point. On failure nothing
    is published, so the next _get_context() call retries from scratch.

    "Not a separate entry point" is enforced rather than merely documented:
    calling this directly would run every factory again and overwrite
    ``_context``, re-creating the duplication #192 exists to remove. Since the
    ownership marker is only ever set while the lock is held, requiring it to
    name this thread is the same as requiring the lock.
    """
    global _context

    if _context_init_owner is not threading.current_thread():
        raise RuntimeError(
            "_build_context() called without holding the context "
            "initialisation lock. It is internal to _get_context(); call that "
            "instead."
        )

    from opencrab.config import get_settings
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.query import HybridQuery
    from opencrab.ontology.rebac import ReBACEngine
    from opencrab.stores.factory import (
        make_billing_sql_store,
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    cfg = get_settings()

    # #140: the stores are built into a list first so a factory that raises
    # part-way through can close the ones already built. Previously a failure
    # after make_vector_store() dropped a live ChromaStore on the floor, and
    # with the chroma.lock handle now living on that instance, dropping it
    # silently would rely on refcounting to release an OS lock. Closing is
    # explicit instead.
    built: list[Any] = []

    def _make(factory: Callable[..., Any], *args: Any) -> Any:
        store = factory(*args)
        built.append(store)
        return store

    try:
        graph = _make(make_graph_store, cfg)
        vector = _make(make_vector_store, cfg)
        docs = _make(make_doc_store, cfg)
        sql = _make(make_sql_store, cfg)
        billing_sql = _make(make_billing_sql_store, cfg, sql)
    except BaseException:
        _close_all(built)
        raise

    builder = OntologyBuilder(graph, docs, sql, vec=vector)
    rebac = ReBACEngine(graph, sql)
    impact = ImpactEngine(graph, sql)
    hybrid = HybridQuery(vector, graph)

    # Attach Phase 4 dependencies to HybridQuery for BM25 + policy filter
    hybrid._doc_store = docs
    hybrid._rebac = rebac

    # Phase 5: billing hooks
    # issue #105: billing_events gets its own SQLite file in local/kuzu mode
    # (a no-op passthrough to `sql` in pg/docker mode) — see
    # make_billing_sql_store's docstring and opencrab/billing/hooks.py's
    # module docstring (including "NO AUTOMATIC MIGRATION") for why.
    from opencrab.billing.hooks import BillingHooks
    billing = BillingHooks(billing_sql)

    _context = {
        "neo4j": graph,
        "chroma": vector,
        "mongo": docs,
        "sql": sql,
        "builder": builder,
        "rebac": rebac,
        "impact": impact,
        "hybrid": hybrid,
        "billing": billing,
    }
    return _context


# ---------------------------------------------------------------------------
# Tool implementations — physically split across handler submodules.
# Importing each submodule executes its @tool(...)-decorated definitions,
# which populates _registry._REGISTRY (see that module's docstring for why
# TOOLS order is controlled by an explicit `order=` kwarg rather than by
# this import sequence).
# ---------------------------------------------------------------------------

from .catalog import tool_search  # noqa: E402
from .graph import (  # noqa: E402
    ontology_add_edge,
    ontology_add_node,
    ontology_get_node,
    ontology_list_edges,
    ontology_list_nodes,
    ontology_manifest,
)
from .harness import harness_promotion_apply  # noqa: E402
from .pack import (  # noqa: E402
    _NINE_SPACE_HINT,
    _ingest_into_pack,
    _nine_space_hint,
    _slugify,
    content_pack_list,
    pack_create,
    pack_fork,
    pack_ingest,
    pack_publish,
)
from .query import ontology_impact, ontology_lever_simulate, ontology_query  # noqa: E402
from .schema import (  # noqa: E402
    schema_pack_install,
    schema_pack_list,
    schema_pack_uninstall,
)

__all__ = [
    "TOOLS",
    "TOOL_SCHEMAS",
    "AccessTier",
    "ForbiddenArgumentError",
    "UnknownToolError",
    "WRITE_TOOLS",
    "_NINE_SPACE_HINT",
    "_TOOL_FUNCTIONS",
    "_clean_meta",
    "_clean_str",
    "_context",
    "_current_read_scope",
    "_get_context",
    "_ingest_into_pack",
    "_lock_data_dir",
    "_nine_space_hint",
    "_slugify",
    "_write_lock",
    "content_pack_list",
    "dispatch_tool",
    "harness_promotion_apply",
    "ontology_add_edge",
    "ontology_add_node",
    "ontology_get_node",
    "ontology_impact",
    "ontology_lever_simulate",
    "ontology_list_edges",
    "ontology_list_nodes",
    "ontology_manifest",
    "ontology_query",
    "pack_create",
    "pack_ingest",
    "pack_fork",
    "pack_publish",
    "schema_pack_install",
    "schema_pack_list",
    "schema_pack_uninstall",
    "tool_search",
    "tools_for_principal",
]

# ---------------------------------------------------------------------------
# Tool registry (used by the MCP server for tools/list / tools/call)
# ---------------------------------------------------------------------------
# Derived from _registry._REGISTRY, which every @tool-decorated handler in the
# submodules above populated in decoration order (positioned via `order=`).
# TOOL_SCHEMAS / _TOOL_FUNCTIONS are kept as back-compat aliases — tests and
# _legacy.py import them by name.

TOOLS: list[dict[str, Any]] = build_tools()
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {name: spec.schema for name, spec in _REGISTRY.items()}
_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {name: spec.fn for name, spec in _REGISTRY.items()}
dispatch_tool = _registry_dispatch_tool

# Tools that need the cross-process write.lock (NOT "tools that touch a store" —
# see the `writes` field docstring in _registry.py#tool; a store write whose
# file is structurally independent of every write.lock'd writer's file, so
# there is no contention for the lock to prevent in the first place, e.g.
# billing_events via ontology_query (its own billing.db — issue #105
# corrected both the earlier idempotency rationale and a later
# retry-with-backoff attempt, neither of which was the real fix), deliberately
# stays out of this set. When several MCP server processes run against the
# same data dir (e.g. the unauthenticated + authenticated HTTP instances), their
# writes must be serialised. dispatch_tool's write.lock is a *per-write* exclusive
# lock on a dedicated write.lock file — entirely separate from chroma.lock
# (LOCK_SH), which is now held for the lifetime of each local ChromaStore
# instance (#140, see opencrab/stores/chroma_store.py) and guards against the
# offline batch loader's LOCK_EX. Reads take no lock. NOTE: lockless concurrent reads across processes
# is THIS layer's design assumption, NOT a chromadb guarantee — chromadb
# officially treats multi-process PersistentClient sharing as unsupported.
# write.lock serialises the one hazard the docs name explicitly (concurrent
# writers); cross-process reads here are stale-risk (a reader's in-memory HNSW
# won't see another process's new vectors until reload), not corruption. Robust
# fix = single chroma server + HttpClient.
#
# *Derived* from each handler's `@tool(..., writes=True)` declaration (see
# _registry.tool) rather than hand-copied here. Issue #65: a hand-maintained
# WRITE_TOOLS set silently missed ontology_impact / ontology_lever_simulate,
# which persist rows via save_impact/save_simulation, so dispatch_tool never
# locked around them. Deriving it means a future write handler that forgets
# `writes=True` fails the registry contract test instead of silently
# bypassing the lock.
#
# One-time snapshot, not a live view: computed once here, after all five
# handler submodules (graph/query/pack/schema/harness) have been imported
# above and finished registering into _REGISTRY, so it correctly captures
# every built-in tool. A tool registered into _REGISTRY *after* this line
# would not appear in WRITE_TOOLS — acceptable today because nothing
# registers tools post-import except tests, which clean up their probe
# registrations in a `finally` (see
# tests/test_tool_registry_contract.py::TestWriteLockCoverage).
WRITE_TOOLS: frozenset[str] = frozenset(name for name, spec in _REGISTRY.items() if spec.writes)
