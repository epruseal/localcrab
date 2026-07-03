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

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.config import Settings


def make_graph_store(settings: Settings) -> Any:
    """Return KuzuGraphStore (kuzu), LocalGraphStore (local), or Neo4jStore (docker).

    kuzu 모드는 ladybug>=0.18 런타임(리브랜딩된 KùzuDB)을 사용한다. 과거에는
    RPi5 16KB 페이지 커널에서 구버전 kuzu의 madvise 호출이 크래시해
    LD_PRELOAD=madv_noop.so 로 우회했으나, ladybug>=0.18 이 이 문제를 자체
    처리해(LadybugDB/ladybug#527) 우회가 더 이상 필요하지 않다.
    """
    if settings.storage_mode == "kuzu":
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        db_path = os.path.join(settings.local_data_dir, "graph.kuzu")
        return KuzuGraphStore(db_path=db_path)
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
    from opencrab.stores.openai_embedding import OpenAIEmbeddingFunction
    from opencrab.stores.llamacpp_embedding import LlamaCppEmbeddingFunction
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
    return ResilientEmbeddingFunction(primary=primary_efs, fallback=fallback_ef)


def make_vector_store(settings: Settings) -> Any:
    """벡터 스토어 백엔드를 VECTOR_BACKEND(resolved) 로 선택해 반환.

    VECTOR_BACKEND 명시 설정이 없으면 vector_backend_resolved 가 조건부 기본값을
    고른다(local 운영 + EMBEDDING_BACKEND=openai → "sqlite-vec", 그 외 → "chroma").
      "chroma"     : ChromaDB. EMBEDDING_BACKEND 로 EF 분기(기존 동작 100% 보존).
      "sqlite-vec" : sqlite-vec(vec0). 앱이 KURE EF 로 직접 임베딩 후 INSERT.
                     4스토어를 단일 SQLite WAL 규율로 통일(Chroma 제약 제거).
      "pgvector"   : (예약) 미구현 — 명시적 오류.

    설계: docs/pgvector-migration-plan.md §3.6 / §9. 임베딩은 백엔드와 무관하게 동일.
    한국어 검색 품질: minilm MRR 0.285 vs KURE-v1 1.000.
    """
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
        # VECTOR_ANN 유효성 검증: ""(off, 기본) / "binary"(2단계 양자화, §3.7)만 허용.
        # 잘못된 값은 막연한 런타임 오류 대신 기동 시 명확한 설정 오류로 안내.
        if settings.vector_ann not in ("", "binary"):
            raise ValueError(
                f"Unknown VECTOR_ANN: {settings.vector_ann!r} "
                "(유효값: 미설정(off) 또는 'binary')"
            )
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

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
        raise NotImplementedError(
            "VECTOR_BACKEND=pgvector 는 아직 미구현입니다 "
            "(docs/pgvector-migration-plan.md (B) 경로)."
        )

    if backend != "chroma":
        raise ValueError(f"Unknown VECTOR_BACKEND: {backend!r}")

    # backend == "chroma"
    from opencrab.stores.chroma_store import ChromaStore

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
        )

    # 기존 경로: EMBEDDING_BACKEND=local 또는 미설정
    # ChromaDB 기본 EF (minilm, 384d) 사용. 기존 동작 100% 보존.
    return ChromaStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        collection_name=settings.chroma_collection,
        local_mode=settings.is_local,
        local_path=chroma_path,
    )


def make_doc_store(settings: Settings) -> Any:
    """Return LocalSQLDocStore (local) or MongoStore (docker).

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
    if settings.is_local:
        # LocalDocStore (JSON) → LocalSQLDocStore (SQLite).
        # See module docstring in local_sql_doc_store.py for full rationale.
        from pathlib import Path

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        db_path = Path(settings.local_data_dir) / "doc_store.db"
        return LocalSQLDocStore(str(db_path))
    else:
        from opencrab.stores.mongo_store import MongoStore

        return MongoStore(uri=settings.mongodb_uri, db_name=settings.mongodb_db)


def make_sql_store(settings: Settings) -> Any:
    """Return SQLStore with SQLite (local) or PostgreSQL (docker)."""
    from opencrab.stores.sql_store import SQLStore

    url = settings.sqlite_url if settings.is_local else settings.postgres_url
    return SQLStore(url=url)
