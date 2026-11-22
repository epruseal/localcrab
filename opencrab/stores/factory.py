"""
Store factory — returns the right backend based on STORAGE_MODE setting.

Usage:
    from opencrab.stores.factory import make_graph_store, make_vector_store, ...
    graph  = make_graph_store(settings)
    vector = make_vector_store(settings)
    docs   = make_doc_store(settings)
    sql    = make_sql_store(settings)
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.config import Settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _get_pg_engine(url: str) -> Any:
    """PG-unified(storage_mode=="pg") 모드에서 4스토어(sql/vector/doc/graph)가
    공유할 SQLAlchemy 엔진을 URL당 1회만 생성해 캐시한다(단일 커넥션 풀).

    lru_cache 를 쓰는 이유: make_graph_store/make_vector_store/make_doc_store/
    make_sql_store 는 기존 시그니처(settings 단일 인자)를 그대로 유지해야 하므로
    프로세스 전역 상태를 팩토리 함수 사이에서 명시적으로 전달할 방법이 없다.
    URL 을 캐시 키로 쓰면 같은 POSTGRES_URL 로 호출되는 모든 make_* 가 같은
    Engine 인스턴스를 받는다(다른 URL 이면 별도 엔진 — 테스트에서 여러 DSN을
    쓰는 경우에도 안전). 프로세스 수명 동안 유지되며, 각 스토어는
    dsn_or_engine 이 Engine 인스턴스이면 close() 에서 dispose 하지 않는다
    (owns_engine=False — 다른 스토어가 계속 쓰는 풀을 끊지 않기 위함).
    """
    from sqlalchemy import create_engine

    from opencrab.stores._pg_url import normalize_pg_url

    return create_engine(normalize_pg_url(url), pool_pre_ping=True)


# 단일 SQLite 모드(#671). 경로별 공유 파일 등록부이며 모듈 잠금이 지킨다. 닫힌 객체는 남겨 두고
# 다음 호출이 새 객체로 바꾼다. 소유자 없는 객체는 호출 쪽이 닫는다(_build_on_single_file).
_SINGLE_FILES: dict[str, Any] = {}
_SINGLE_FILES_LOCK = threading.RLock()


def _single_mode_checks(settings: Settings) -> str:
    """단일 모드 설정을 검사하고 single.db 경로를 돌려준다. 파일을 만지기 전에 거부한다."""
    if settings.storage_mode != "local":
        raise RuntimeError("single SQLite requires STORAGE_MODE=local")
    if settings.vector_backend_resolved != "sqlite-vec":
        raise RuntimeError("single SQLite requires the sqlite-vec vector backend")
    path = settings.single_sqlite_file
    message = "single SQLite requires LOCAL_DATA_DIR to be the database directory"
    if not os.path.isabs(path):
        raise RuntimeError(message)
    root = os.path.dirname(path)
    # MCP 쓰기 잠금은 환경변수 LOCAL_DATA_DIR을 우선하고 저장소 쓰기 잠금은 DB 디렉터리를 쓴다.
    # 둘이 갈라지면 write.lock이 서로 다른 파일이 되므로 둘 다 root와 같아야 한다.
    if os.path.realpath(settings.local_data_dir) != root:
        raise RuntimeError(message)
    env_dir = os.environ.get("LOCAL_DATA_DIR")
    if env_dir:
        from opencrab.config import expand_user_path

        if os.path.realpath(expand_user_path(env_dir)) != root:
            raise RuntimeError(message)
    return path


def _open_single_file(path: str) -> Any:
    """경로의 열린 공유 객체를 돌려준다. 없으면 만들고 결속을 검증한 뒤 등록한다."""
    from opencrab.stores._single_file import SharedSqliteFile

    with _SINGLE_FILES_LOCK:
        shared = _SINGLE_FILES.get(path)
        if shared is not None and not shared._closed:
            return shared
        shared = SharedSqliteFile(path, checked=True)
        try:
            root = os.path.dirname(path)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                shared.validate_binding(root, root_fd, root_fd, "ledger.json", "manifest.json", "marker.json")
            finally:
                os.close(root_fd)
        except BaseException:
            shared.close()
            raise
        _SINGLE_FILES[path] = shared
        return shared


def _build_on_single_file(shared: Any, build: Any) -> Any:
    """저장소를 만든다. 생성이 실패하고 소유자가 없으면 공유 객체를 닫아 수명 잠금을 푼다."""
    try:
        return build()
    except BaseException:
        with shared.lock:
            if not shared._users and not shared._closed:
                shared.close()
        raise


def make_graph_store(settings: Settings) -> Any:
    """Return PGGraphStore (pg), the disabled Kuzu facade (kuzu), LocalGraphStore (local),
    or Neo4jStore (docker).

    kuzu 모드는 Ladybug qualification이 끝날 때까지 capability-negative다.
    RPi5 16KB 페이지 커널의 madvise 문제는 ladybug>=0.18에서 해결됐지만
    (LadybugDB/ladybug#527), production 경로를 열려면 transaction owner와
    node/edge 원자적 CAS를 별도로 검증해야 한다.
    """
    if getattr(settings, "single_sqlite_file", ""):
        from opencrab.stores.local_graph_store import LocalGraphStore

        path = _single_mode_checks(settings)
        shared = _open_single_file(path)
        return _build_on_single_file(shared, lambda: LocalGraphStore(db_path=path, shared=shared))
    if settings.storage_mode == "pg":
        from opencrab.stores.pg_graph_store import PGGraphStore

        engine = _get_pg_engine(settings.postgres_url)
        return PGGraphStore(engine)
    elif settings.storage_mode == "kuzu":
        from opencrab.stores.kuzu_graph_store import KuzuUnavailableGraphStore

        db_path = os.path.join(settings.local_data_dir, "graph.kuzu")
        return KuzuUnavailableGraphStore(db_path=db_path)
    elif settings.is_local:
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = os.path.join(settings.local_data_dir, "graph.db")
        return LocalGraphStore(db_path=db_path)
    else:
        from opencrab.stores.neo4j_store import Neo4jStore

        return Neo4jStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )


def _make_kure_embedding_function(settings: Settings) -> Any:
    """Assemble the KURE embedding function (primary=OpenAI 호환 서버(들) /
    fallback=로컬 GGUF). 공유 헬퍼 — Chroma(openai 분기)·sqlite-vec·(추후
    pgvector)가 동일 임베딩 경로를 재사용한다. 임베딩은 벡터 스토어 백엔드와
    무관하게 동일하다.

    settings.openai_api_bases 가 콤마로 여러 URL 을 담고 있으면 엔드포인트별로
    OpenAIEmbeddingFunction 을 하나씩 만들어 리스트로 ResilientEmbeddingFunction
    에 전달한다 — 순서대로 시도되는 체인(첫 URL 이 죽어도 다음 URL 을 우선
    시도한 뒤에야 GGUF 폴백으로 내려간다). 단일 URL(기본값)이면 리스트 길이 1
    이라 기존 동작과 동일하다.

    local_gguf_path 가 비어있으면 llamacpp_embedding._ensure_local_gguf() 가
    KURE-v1-Q8_0 을 자동 다운로드. 모든 원격이 장애일 때 폴백으로 사용됨.
    """
    from opencrab.stores.llamacpp_embedding import LlamaCppEmbeddingFunction
    from opencrab.stores.openai_embedding import OpenAIEmbeddingFunction
    from opencrab.stores.resilient_embedding import ResilientEmbeddingFunction

    primary_efs = [
        OpenAIEmbeddingFunction(
            api_base=api_base,
            model=settings.openai_embed_model,
            dim=settings.embed_dim,
            timeout=settings.openai_timeout,
            api_key=settings.openai_api_key,
        )
        for api_base in settings.openai_api_bases
    ]
    fallback_ef = LlamaCppEmbeddingFunction(
        gguf_path=settings.local_gguf_path,
        dim=settings.embed_dim,
    )
    # 호출당 실패한 원격 시도 시간 상한: 시간 초과의 두 배(primary 둘이 모두
    # 시간 초과까지 가는 최악을 허용). 시간 초과가 양수가 아니면 예산을 끈다
    # (예산은 양수여야 한다). 근거: docs/issue-581-embed-backoff.md
    return ResilientEmbeddingFunction(
        primary=primary_efs,
        fallback=fallback_ef,
        attempt_budget=(2 * settings.openai_timeout if settings.openai_timeout > 0 else None),
    )


def make_vector_store(settings: Settings) -> Any:
    """벡터 스토어 백엔드를 VECTOR_BACKEND(resolved) 로 선택해 반환.

    VECTOR_BACKEND 명시 설정이 없으면 vector_backend_resolved 가 조건부 기본값을
    고른다(local 운영 + EMBEDDING_BACKEND=openai → "sqlite-vec", 그 외 → "chroma").
      "chroma"     : ChromaDB. EMBEDDING_BACKEND 로 EF 분기(기존 동작 100% 보존).
      "sqlite-vec" : sqlite-vec(vec0). 앱이 KURE EF 로 직접 임베딩 후 INSERT.
                     4스토어를 단일 SQLite WAL 규율로 통일(Chroma 제약 제거).
      "pgvector"   : pgvector(PostgreSQL 확장). STORAGE_MODE=pg 이면 sql/doc/graph
                     와 동일 공유 SQLAlchemy 엔진을 주입받는다. storage_mode!="pg"
                     여도 VECTOR_BACKEND=pgvector 를 명시하면 벡터만 PG 를 쓸 수
                     있다(벡터만 PG로 보내는 조합. 이 경우 postgres_url 로 자체 엔진 생성).

    설계: docs/vector-backends.md §2. 임베딩은 백엔드와 무관하게 동일.
    한국어 검색 품질: minilm MRR 0.285 vs KURE-v1 1.000.
    """
    single_path = _single_mode_checks(settings) if getattr(settings, "single_sqlite_file", "") else ""
    backend = settings.vector_backend_resolved

    if backend == "sqlite-vec":
        # sqlite-vec 는 앱측 임베딩이 필수이고 KURE(1024d) 를 표준으로 쓴다. minilm 은
        # Chroma 내장 ONNX EF 라 앱 직접 호출 경로가 없어(차원도 384 불일치) 지원하지 않는다.
        # 명확한 설정 오류로 안내(막연한 차원 불일치 크래시 대신).
        if settings.embedding_backend != "openai":
            raise ValueError(
                "VECTOR_BACKEND=sqlite-vec 는 EMBEDDING_BACKEND=openai (KURE 1024d) 가 필요합니다. "
                f"현재 EMBEDDING_BACKEND={settings.embedding_backend!r}. "
                "KURE EF 로 앱측 임베딩 후 vec0 에 INSERT 하므로 minilm(384d)은 미지원입니다."
            )
        # VECTOR_ANN 유효성 검증: ""(off, 기본) / "binary"(2단계 양자화, docs/vector-backends.md §4.1)만 허용.
        # 잘못된 값은 막연한 런타임 오류 대신 기동 시 명확한 설정 오류로 안내.
        if settings.vector_ann not in ("", "binary"):
            raise ValueError(
                f"Unknown VECTOR_ANN: {settings.vector_ann!r} "
                "(유효값: 미설정(off) 또는 'binary')"
            )
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        if single_path:
            shared = _open_single_file(single_path)

            def build() -> Any:
                return SqliteVecStore(
                    db_path=single_path,
                    embedding_function=_make_kure_embedding_function(settings),
                    dim=settings.embed_dim,
                    collection_name=settings.vector_collection,
                    ann=settings.vector_ann,
                    ann_coarse_k=settings.vector_ann_coarse_k,
                    shared=shared,
                )

            return _build_on_single_file(shared, build)

        db_path = os.path.join(settings.local_data_dir, settings.vector_db_file)
        ef = _make_kure_embedding_function(settings)
        return SqliteVecStore(
            db_path=db_path,
            embedding_function=ef,
            dim=settings.embed_dim,
            collection_name=settings.vector_collection,
            ann=settings.vector_ann,
            ann_coarse_k=settings.vector_ann_coarse_k,
        )

    if backend == "pgvector":
        from opencrab.stores.pg_vector_store import PgVectorStore

        # EMBEDDING_BACKEND guard mirrors the sqlite-vec branch above: pgvector
        # stores raw vectors (no internal EF), so the app-side KURE EF is
        # required — minilm has no app-callable EF (Chroma-internal ONNX only).
        if settings.embedding_backend != "openai":
            raise ValueError(
                "VECTOR_BACKEND=pgvector 는 EMBEDDING_BACKEND=openai (KURE 1024d) 가 필요합니다. "
                f"현재 EMBEDDING_BACKEND={settings.embedding_backend!r}. "
                "KURE EF 로 앱측 임베딩 후 pgvector 테이블에 INSERT 하므로 minilm(384d)은 미지원입니다."
            )
        # storage_mode=="pg" 이면 sql/doc/graph 와 동일 공유 엔진. 아니면
        # (VECTOR_BACKEND=pgvector 명시 + local 모드 등) 벡터 전용 DSN 으로
        # PgVectorStore 가 자체 엔진을 생성한다(dsn_or_engine=str 경로).
        dsn_or_engine: Any = (
            _get_pg_engine(settings.postgres_url)
            if settings.storage_mode == "pg"
            else settings.postgres_url
        )
        ef = _make_kure_embedding_function(settings)
        return PgVectorStore(
            dsn_or_engine,
            embedding_function=ef,
            dim=settings.embed_dim,
            collection_name=settings.embed_collection,
            ef_search=settings.pg_ef_search,
        )

    if backend != "chroma":
        raise ValueError(f"Unknown VECTOR_BACKEND: {backend!r}")

    # backend == "chroma"
    from opencrab.stores._single_file import reject_single_root
    from opencrab.stores.chroma_store import ChromaStore

    # chroma 디렉터리와 chroma.lock은 처음 쓸 때 만들어진다. 단일 root를 받으면 만들기 전에 거부한다(B11).
    reject_single_root(settings.local_data_dir)
    chroma_path = os.path.join(settings.local_data_dir, "chroma")

    if settings.embedding_backend == "openai":
        # OpenAI 호환 서버 백엔드: GPU 주력 + 로컬 GGUF 폴백
        ef = _make_kure_embedding_function(settings)
        return ChromaStore(
            host=settings.chroma_host,
            port=settings.chroma_port,
            collection_name=settings.embed_collection,
            local_mode=settings.is_local,
            local_path=chroma_path,
            embedding_function=ef,
            # #140: pass the caller's setting through. Without it ChromaStore
            # falls back to the lru_cached global get_settings(), so a caller
            # that built its own Settings waits the process default instead.
            lock_timeout=settings.chroma_lock_timeout,
        )

    # 기존 경로: EMBEDDING_BACKEND=local 또는 미설정
    # ChromaDB 기본 EF (minilm, 384d) 사용. 기존 동작 100% 보존.
    return ChromaStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        collection_name=settings.chroma_collection,
        local_mode=settings.is_local,
        local_path=chroma_path,
        lock_timeout=settings.chroma_lock_timeout,  # see the branch above (#140)
    )


def make_doc_store(settings: Settings) -> Any:
    """Return LocalSQLDocStore (local/kuzu), PgDocStore (pg), or MongoStore (docker).

    WHY LocalSQLDocStore INSTEAD OF LocalDocStore (JSON):
        list_nodes(limit=50000) is called on every BM25 cache rebuild (i.e.
        every query).  LocalDocStore._load() deserialises the entire JSON file
        on each call — O(N) — so a 10× data growth means a 10× slower hot
        path with no way to offset it.  LocalSQLDocStore issues a single
        SELECT … LIMIT query, which SQLite satisfies with an O(k) range scan
        and never reads rows beyond the limit.

    WHY LocalDocStore IS KEPT (not removed):
        Legacy callers that instantiate LocalDocStore directly (e.g. migration
        scripts, unit tests written before this switch) must continue to work.
        Removing the import here does not delete the class; leaving it avoids
        a confusing ImportError if someone still references it.

    WHY db_path = LOCAL_DATA_DIR / "doc_store.db":
        Keeps the SQLite file in the same directory as graph.db and
        opencrab.db, so a single LOCAL_DATA_DIR backup captures all local
        state.  A fixed filename ("doc_store.db") makes the path predictable
        for operators and migration tooling.
    """
    if getattr(settings, "single_sqlite_file", ""):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        path = _single_mode_checks(settings)
        shared = _open_single_file(path)
        return _build_on_single_file(shared, lambda: LocalSQLDocStore(path, shared=shared))
    if settings.is_local:
        # LocalDocStore (JSON) → LocalSQLDocStore (SQLite).
        # See module docstring in local_sql_doc_store.py for full rationale.
        from pathlib import Path

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        db_path = Path(settings.local_data_dir) / "doc_store.db"
        return LocalSQLDocStore(str(db_path))
    elif settings.storage_mode == "pg":
        from opencrab.stores.pg_doc_store import PgDocStore

        engine = _get_pg_engine(settings.postgres_url)
        return PgDocStore(engine)
    else:
        from opencrab.stores.mongo_store import MongoStore

        return MongoStore(uri=settings.mongodb_uri, db_name=settings.mongodb_db)


def make_sql_store(settings: Settings) -> Any:
    """local과 kuzu는 SQLite를 쓰고 pg는 공유 PostgreSQL 엔진을 쓴다."""
    from opencrab.stores.sql_store import SQLStore

    if getattr(settings, "single_sqlite_file", ""):
        path = _single_mode_checks(settings)
        shared = _open_single_file(path)
        return _build_on_single_file(shared, lambda: SQLStore(url=f"sqlite:///{path}", shared=shared))
    url = settings.postgres_url if settings.storage_mode in ("docker", "pg") else settings.sqlite_url
    engine = _get_pg_engine(url) if settings.storage_mode == "pg" else None
    return SQLStore(url=url, engine=engine)


def make_auth_sql_store(settings: Settings) -> Any:
    """인증 조회용 SQL 저장소를 연다. 조회할 것이 없다고 확정되면 None을 돌려준다(비단일 모드만).

    인증 경로 셋(require_local_principal, maybe_bootstrap_on_empty, HTTP 토큰 조회)이 이 함수 하나를
    지난다. 모두 읽기만 하므로 create_tables=False로 연다. DDL을 돌리면 write.lock과 새 파일이 생긴다.

    단일 모드는 공유 파일 위에서 열고 결속 검증을 거친다. 열기 단계의 오류(RuntimeError, ValueError,
    OSError, sqlite3.Error)는 SingleFileUnavailableError로 바꿔 올린다. HTTP 조회가 이 예외를 503으로
    답하게 하려는 것이다. 비가용 저장소는 닫고 같은 예외를 올려, 호출자에게 공유 파일의 소유자가 새지
    않게 한다. 비단일 모드는 옛 규칙 그대로다. 로컬 모드는 opencrab.db가 파일로 없으면 None이다(연결이
    파일을 만들기 때문이다). 비가용 저장소는 그대로 돌려준다.
    """
    import sqlite3

    from opencrab.stores._single_file import SingleFileUnavailableError, sqlite_busy_or_locked
    from opencrab.stores.sql_store import SQLStore

    if getattr(settings, "single_sqlite_file", ""):
        try:
            path = _single_mode_checks(settings)
            shared = _open_single_file(path)
            store = _build_on_single_file(
                shared, lambda: SQLStore(url=f"sqlite:///{path}", create_tables=False, shared=shared)
            )
            if not getattr(store, "available", False):
                store.close()
                raise SingleFileUnavailableError("single SQLite store is not available")
            return store
        except SingleFileUnavailableError:
            raise
        except sqlite3.Error as exc:
            reason = "store-busy" if sqlite_busy_or_locked(exc) else "store-unavailable"
            logger.warning("single SQLite auth store open failed (%s): %s", reason, exc)
            raise SingleFileUnavailableError(str(exc), reason=reason) from exc
        except (RuntimeError, ValueError, OSError) as exc:
            reason = "store-busy" if isinstance(exc, TimeoutError) else "store-unavailable"
            logger.warning("single SQLite auth store open failed (%s): %s", reason, exc)
            raise SingleFileUnavailableError(str(exc), reason=reason) from exc
    if settings.is_local:
        from pathlib import Path

        if not Path(settings.local_data_dir, "opencrab.db").is_file():
            return None
        return SQLStore(url=settings.sqlite_url, create_tables=False)
    return SQLStore(url=settings.postgres_url, create_tables=False)


def make_billing_sql_store(settings: Settings, sql_store: Any) -> Any:
    """Return the SQLStore ``BillingHooks`` should use.

    issue #105: billing_events used to share `sql_store`'s file/engine with
    ontology_nodes/impact_records/lever_simulations — tables written under
    the cross-process write.lock. SQLite's default (non-WAL) journal mode
    takes a whole-FILE write lock, so an unlocked billing insert could block
    behind, or lose to, an unrelated long write (e.g. bulk pack_ingest) for
    as long as that write took — see ``opencrab.billing.hooks``'s module
    docstring for the full analysis of why WAL and retry-with-backoff both
    fail to fix this and only file separation does.

    PG/docker mode: `sql_store` is returned as-is. PostgreSQL uses row-level
    locking, not a whole-file lock, so this contention never existed there —
    no reason to open a second connection.

    Local/kuzu (SQLite) mode: a new SQLStore on its own file, `billing.db`,
    next to graph.db/doc_store.db/opencrab.db in the same LOCAL_DATA_DIR
    (fixed filename, no env var — same convention as doc_store.db; nobody
    has asked to configure this path). It starts EMPTY — a pre-#105 install's
    old rows are deliberately left where they are, in opencrab.db's
    billing_events; see ``opencrab.billing.hooks``'s module docstring
    ("NO AUTOMATIC MIGRATION") for why an automatic copy was tried and
    reverted, and what to do if that history is ever needed. Built with
    `create_tables=False`: SQLStore normally also creates the generic
    ontology_nodes/ontology_edges/impact_records/lever_simulations/
    rebac_policies schema on connect, which billing.db has no use for
    (BillingHooks._ensure_tables() creates billing_events itself) and would
    otherwise sit there as 5 confusingly-empty tables in a file meant to
    hold exactly one.
    """
    if getattr(settings, "single_sqlite_file", ""):
        # 결속을 마친 열린 SQL 저장소가 아니면 billing 파일을 만들지 않는다. 호출 순서에 기대지 않는다.
        path = settings.single_sqlite_file
        shared = getattr(sql_store, "shared_file", None)
        with _SINGLE_FILES_LOCK:
            bound = _SINGLE_FILES.get(path)
        if shared is None or shared is not bound or shared._closed:
            raise RuntimeError("single SQLite billing requires the bound SQL store")
        from opencrab.stores.sql_store import SQLStore

        # billing.db는 root 안 파일이다. 공유 객체 없이 여는 이 한 호출만 root 검사를 건너뛴다(B11).
        return SQLStore(
            url=f"sqlite:///{os.path.dirname(path)}/billing.db", create_tables=False, allow_single_root=True
        )

    if settings.storage_mode in ("docker", "pg"):
        return sql_store

    from pathlib import Path

    from opencrab.stores.sql_store import SQLStore

    db_path = Path(settings.local_data_dir) / "billing.db"
    return SQLStore(url=f"sqlite:///{db_path}", create_tables=False)
