"""
pgvector store adapter (PostgreSQL-unified backend, (B) 경로).

Drop-in replacement for :class:`ChromaStore` (and sibling to
:class:`SqliteVecStore`, A 경로) that keeps the vector index in the same
PostgreSQL instance as sql/doc/graph stores in PG-unified 모드. See
``docs/pgvector-migration-plan.md`` §3.1-§3.5 / §4.1-B for the original design;
스키마/인덱스 파라미터는 프리플라이트 실측(HNSW global p95 6.44ms)으로 아래와 같이
확정했다.

WHY A SEPARATE STORE (not an embedding-function swap):
    pgvector는 SqliteVecStore와 마찬가지로 *벡터 스토어 백엔드*이지 임베딩 백엔드가
    아니다. Chroma는 텍스트를 받아 내부에서 임베딩했지만, pgvector 테이블에는
    원시 벡터를 저장하므로 앱이 KURE(ResilientEmbeddingFunction)로 직접 계산한
    벡터를 INSERT한다. 임베딩 경로는 스토어와 무관하게 동일 — 바뀌는 것은
    저장/검색 백엔드뿐이다.

CONTRACT PARITY (ChromaStore, chroma_store.py / SqliteVecStore, sqlite_vec_store.py):
    동일 공개 메서드/시그니처/반환/가드. ``query``는 ``id/document/metadata/distance``
    키를 가진 dict 리스트를 distance 오름차순으로 반환한다(distance = pgvector
    ``<=>`` cosine distance = 1 - cos, sqlite-vec/Chroma와 동일 계약).
    ID 규칙(sha256 16자, add=시간 salt/upsert=content-deterministic)도 동일.

SCHEMA / INDEX (프리플라이트 실증 완료, 2026-07 — 이대로 구현):
    ``CREATE TABLE {collection} (node_id TEXT PRIMARY KEY, pack_id TEXT,
    embedding vector(dim) NOT NULL, document TEXT, metadata JSONB)``.
    ``pack_id``는 전용 컬럼 + btree 인덱스로 분리(JSONB GIN은 실증상 이점 없어
    미채택) — where 필터의 pack_id 등가/멤버십은 이 컬럼으로 푸시다운하고,
    나머지 키는 ``metadata ->> 'key'`` JSONB 조건으로 번역한다(§ query 참고).
    ANN 인덱스는 HNSW(``vector_cosine_ops``, ``m=16, ef_construction=64``) —
    쿼리 시 세션 파라미터 ``hnsw.ef_search``(생성자 인자, 기본 500 — 179k 전량
    실측에서 150은 recall 0.9370로 게이트 미달, 500이 0.9600/p95 24.6ms)를 SET한다. HNSW 빌드가 이 RPi 환경의 좁은
    /dev/shm(64MB)에서 병렬 빌드로 실패하는 것을 프리플라이트에서 확인했으므로,
    인덱스 생성 직전 ``maintenance_work_mem='512MB'``/``max_parallel_maintenance_workers=0``
    을 SET한 뒤 CREATE INDEX한다(ensure-schema에서 1회, 세션 단위이므로 부작용 없음).

WHY NOT BINARY 2-STAGE (SqliteVecStore §3.7과 달리 여기서는 불필요):
    HNSW 인덱스가 이미 전역 검색을 실측 p95 6.44ms로 처리하므로(sqlite-vec의
    브루트포스 868ms 문제가 애초에 없음), 별도의 sign-bit 2단계 근사 경로를
    둘 이유가 없다 — HNSW 자체가 이미 서브선형 ANN이다.

CONCURRENCY (SQLAlchemy 엔진 풀 + Postgres MVCC):
    sqlite-vec/LocalSQLDocStore와 달리 스레드-로컬 커넥션이나 앱 레벨 write
    lock을 두지 않는다 — SQLAlchemy 커넥션 풀이 스레드 간 커넥션을 안전하게
    분배하고, Postgres는 MVCC로 동시 트랜잭션을 격리한다(INSERT ... ON CONFLICT
    는 원자적 단문). 스토어 자체 락은 두지 않는 것이 설계 의도(과도한 직렬화로
    PG의 동시성 이점을 죽이지 않기 위함) — 필요 시 커넥션 풀 크기만 조정한다.

WHERE 번역 (Chroma dict → SQL, sqlite-vec과의 차이):
    sqlite-vec은 vec0의 컬럼 제약(16개, IN 미지원) 때문에 KNN 이후 Python
    후처리 필터를 썼지만, pgvector는 JSONB를 SQL WHERE 절에 직접 넣을 수 있어
    **필터를 쿼리 자체에 완전히 푸시다운**한다(2단계 후처리 불필요, 더 단순하고
    정확 — LIMIT n 이후 필터링으로 결과가 n개 미만이 되는 sqlite-vec류 엣지
    케이스가 구조적으로 없음). ``pack_id``는 전용 컬럼, 나머지 키는
    ``metadata ->> :key`` 텍스트 비교 — SQL 3-값 논리(NULL 비교는 항상
    unknown/false)가 Chroma의 "존재하지 않는 메타 키는 매치 실패" 규칙과 자연히
    일치하므로 sqlite_vec_store.py의 ``_MISSING`` sentinel 같은 별도 처리가
    필요 없다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _to_pgvector_literal(vec: list[float]) -> str:
    """pgvector 텍스트 입력 형식 ``'[v1,v2,...]'``로 직렬화.

    psycopg2용 ``pgvector.psycopg2.register_vector`` 어댑터에 의존하지 않고
    문자열을 ``::vector``로 캐스트하는 이유: SQLAlchemy ``text()`` 바인드는
    임의 파이썬 객체 어댑터를 자동 등록하지 않으므로, 텍스트 캐스트가 드라이버/
    엔진 주입 방식(공유 엔진 포함) 어디서나 이식성 있게 동작한다.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorStore:
    """pgvector(PostgreSQL 확장) 어댑터 — ChromaStore/SqliteVecStore 공개 계약 미러."""

    def __init__(
        self,
        dsn_or_engine: Any,
        embedding_function: Callable[[list[str]], list[list[float]]],
        dim: int = 1024,
        collection_name: str = "opencrab_vectors_kure",
        ef_search: int = 500,
    ) -> None:
        """
        Parameters
        ----------
        dsn_or_engine:
            PostgreSQL DSN 문자열(``postgresql://user:pass@host:port/db``) 또는
            이미 생성된 SQLAlchemy ``Engine``. 후자는 factory가 sql/vector/doc
            스토어에 동일 엔진(동일 커넥션 풀)을 공유 주입하는 경로(§3.5)를
            지원하기 위함이다.
        embedding_function:
            앱측 임베딩 콜러블 ``(list[str]) -> list[list[float]]``
            (ResilientEmbeddingFunction / KURE). REQUIRED — Chroma와 달리
            내부 EF가 없다.
        dim:
            벡터 차원(KURE=1024). 테이블은 ``vector(dim)``으로 선언되고, 차원이
            다른 벡터를 쓰면 Postgres가 거부한다.
        collection_name:
            벡터 테이블명. SQL 식별자 검증(``_IDENT_RE``)을 통과해야 한다(테이블명이
            f-string으로 SQL에 보간되므로 인젝션 방지).
        ef_search:
            쿼리 시 세션 파라미터 ``hnsw.ef_search`` 값(recall/속도 트레이드오프
            노브). 기본 500 — 179k 전량 실측에서 150은 recall 0.9370로 게이트
            미달, 500이 recall 0.9600/p95 24.6ms(ef 곡선: vector-backends.md §4.2).
        """
        if embedding_function is None:
            raise ValueError("PgVectorStore requires an embedding_function.")
        if not _IDENT_RE.match(collection_name):
            raise ValueError(f"Unsafe collection_name: {collection_name!r}")
        self._ef = embedding_function
        self._dim = int(dim)
        self._table = collection_name
        self._ef_search = int(ef_search)
        self._engine: Any = None
        self._owns_engine = False
        self._available = False
        self._init_engine(dsn_or_engine)
        if self._available:
            self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection / lifecycle
    # ------------------------------------------------------------------

    def _init_engine(self, dsn_or_engine: Any) -> None:
        try:
            from sqlalchemy import text
            from sqlalchemy.engine import Engine

            if isinstance(dsn_or_engine, str):
                from sqlalchemy import create_engine

                # pool_pre_ping: 풀에 있던 커넥션이 idle 중 끊겼을 때(장수 커넥션
                # 풀의 흔한 실패 모드) 다음 체크아웃에서 조용히 재연결 — 공유 엔진
                # 주입 시에도 이 스토어가 만든 엔진에 한해 켠다.
                self._engine = create_engine(dsn_or_engine, pool_pre_ping=True)
                self._owns_engine = True
            elif isinstance(dsn_or_engine, Engine):
                self._engine = dsn_or_engine
                self._owns_engine = False
            else:
                raise TypeError(
                    "dsn_or_engine must be a DSN string or SQLAlchemy Engine, "
                    f"got {type(dsn_or_engine)!r}"
                )
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self._available = True
        except Exception as exc:  # pragma: no cover - init failure path
            logger.warning("PgVectorStore connect failed: %s", exc)
            self._available = False

    def _ensure_schema(self) -> None:
        """멱등 스키마 생성 — extension/table/pack_id btree/HNSW. 최초 사용 시 1회."""
        from sqlalchemy import text

        try:
            with self._engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {self._table} ("
                        "node_id TEXT PRIMARY KEY, "
                        "pack_id TEXT, "
                        f"embedding vector({self._dim}) NOT NULL, "
                        "document TEXT, "
                        "metadata JSONB"
                        ")"
                    )
                )
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_pack_id_idx "
                        f"ON {self._table} (pack_id)"
                    )
                )
            # HNSW 빌드는 별도 트랜잭션 — /dev/shm 64MB 환경에서 병렬 빌드가 실패하는
            # 것을 프리플라이트로 확인했으므로, 빌드 직전 세션 파라미터로 회피한다.
            # SET(세션 단위, SET LOCAL 아님)은 커밋되면 커넥션에 남지만 이 커넥션은
            # 풀로 반환 후 재사용돼도 무해(다음 DDL에도 동일하게 안전한 값).
            with self._engine.begin() as conn:
                conn.execute(text("SET maintenance_work_mem = '512MB'"))
                conn.execute(text("SET max_parallel_maintenance_workers = 0"))
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_hnsw_idx "
                        f"ON {self._table} USING hnsw (embedding vector_cosine_ops) "
                        "WITH (m=16, ef_construction=64)"
                    )
                )
            logger.info(
                "PgVectorStore initialised (table=%s, dim=%d, ef_search=%d)",
                self._table,
                self._dim,
                self._ef_search,
            )
        except Exception as exc:  # pragma: no cover - schema init failure path
            logger.warning("PgVectorStore schema init failed: %s", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        if not self._available:
            return False
        try:
            from sqlalchemy import text

            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        # 공유 엔진(factory가 sql/vector/doc에 동일 엔진을 주입한 경우, §3.5)은
        # 이 스토어가 소유하지 않으므로 dispose하지 않는다 — 다른 스토어가 계속
        # 쓰는 풀을 여기서 끊으면 안 된다. 이 스토어가 직접 create_engine한
        # 경우(DSN 문자열 생성자 경로)에만 dispose한다.
        if self._owns_engine and self._engine is not None:
            try:
                self._engine.dispose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._ef(list(texts))
        for vec in vectors:
            if len(vec) != self._dim:
                raise RuntimeError(
                    f"Embedding dim {len(vec)} != table dim {self._dim}."
                )
        return vectors

    def add_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """텍스트 청크 추가(콘텐츠+시각 해시 ID, 생략 시). 중복 id는 PK 위반으로
        raise — ChromaStore(경고 후 skip)와의 의도적 차이, SqliteVecStore와 동일
        분기(add=엄격, upsert=갱신)."""
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        if not texts:
            return []
        if ids is None:
            ids = [
                hashlib.sha256(f"{t}{time.time_ns()}".encode()).hexdigest()[:16]
                for t in texts
            ]
        if metadatas is None:
            metadatas = [{} for _ in texts]
        if len(ids) != len(texts) or len(metadatas) != len(texts):
            raise ValueError("texts, metadatas, and ids must have the same length.")
        vectors = self._embed(texts)

        from sqlalchemy import text

        sql = text(
            f"INSERT INTO {self._table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES (:node_id, :pack_id, (:embedding)::vector, :document, (:metadata)::jsonb)"
        )
        with self._engine.begin() as conn:
            for _id, txt, meta, vec in zip(ids, texts, metadatas, vectors):
                conn.execute(
                    sql,
                    {
                        "node_id": _id,
                        "pack_id": str(meta.get("pack_id", "")),
                        "embedding": _to_pgvector_literal(vec),
                        "document": txt,
                        "metadata": json.dumps(meta),
                    },
                )
        return ids

    def upsert_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """업서트(콘텐츠 결정적 ID, 생략 시) — ``INSERT ... ON CONFLICT (node_id)
        DO UPDATE``로 원자적 갱신(vec0의 DELETE-then-INSERT와 달리 PG는 네이티브
        UPSERT를 지원)."""
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        if not texts:
            return []
        if ids is None:
            ids = [hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts]
        if metadatas is None:
            metadatas = [{} for _ in texts]
        if len(ids) != len(texts) or len(metadatas) != len(texts):
            raise ValueError("texts, metadatas, and ids must have the same length.")
        vectors = self._embed(texts)

        from sqlalchemy import text

        sql = text(
            f"INSERT INTO {self._table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES (:node_id, :pack_id, (:embedding)::vector, :document, (:metadata)::jsonb) "
            "ON CONFLICT (node_id) DO UPDATE SET "
            "pack_id = EXCLUDED.pack_id, embedding = EXCLUDED.embedding, "
            "document = EXCLUDED.document, metadata = EXCLUDED.metadata"
        )
        with self._engine.begin() as conn:
            for _id, txt, meta, vec in zip(ids, texts, metadatas, vectors):
                conn.execute(
                    sql,
                    {
                        "node_id": _id,
                        "pack_id": str(meta.get("pack_id", "")),
                        "embedding": _to_pgvector_literal(vec),
                        "document": txt,
                        "metadata": json.dumps(meta),
                    },
                )
        return ids

    def delete(self, ids: list[str]) -> None:
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        if not ids:
            return
        from sqlalchemy import text

        params: dict[str, Any] = {}
        names = []
        for i, _id in enumerate(ids):
            pname = f"id{i}"
            params[pname] = _id
            names.append(f":{pname}")
        with self._engine.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {self._table} WHERE node_id IN ({', '.join(names)})"),
                params,
            )

    def reset_collection(self) -> None:
        """컬렉션 비우기(파괴적). DROP이 아닌 DELETE — 동시 리더가 "no such
        table" 갭을 절대 관측하지 않는다(SqliteVecStore reset_collection과 동일
        원칙). Postgres MVCC 하에서 트랜잭션 단위 원자성이 보장되므로 앱 레벨
        락 없이도 부분 상태가 노출되지 않는다."""
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        from sqlalchemy import text

        with self._engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self._table}"))
        logger.info("PgVectorStore: table '%s' reset.", self._table)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """시맨틱 KNN 검색. id/document/metadata/distance(cosine distance = 1-cos)
        키를 가진 dict 리스트를 distance 오름차순으로 반환 — ChromaStore/
        SqliteVecStore와 동일 계약. where 필터는 SQL WHERE 절로 완전히
        푸시다운되므로(모듈 docstring 참고) sqlite-vec류 "필터 후 n개 미만" 엣지
        케이스가 구조적으로 없다."""
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        if n_results <= 0:
            return []
        qvec = self._embed([query_text])[0]
        qlit = _to_pgvector_literal(qvec)

        where_sql, params = _build_where_sql(where)
        sql = (
            f"SELECT node_id, document, metadata, "
            f"embedding <=> (:qvec)::vector AS distance FROM {self._table} "
        )
        if where_sql is not None:
            sql += f"WHERE {where_sql} "
        sql += "ORDER BY distance LIMIT :limit"
        params = dict(params)
        params["qvec"] = qlit
        params["limit"] = int(n_results)

        from sqlalchemy import text

        with self._engine.connect() as conn:
            # SET은 파라미터 바인딩을 지원하지 않는 유틸리티 커맨드라 리터럴로
            # 보간한다 — self._ef_search는 생성자에서 int() 캐스트되어 안전.
            conn.execute(text(f"SET hnsw.ef_search = {self._ef_search}"))
            rows = conn.execute(text(sql), params).fetchall()

        hits: list[dict[str, Any]] = []
        for row in rows:
            meta = row.metadata
            if not isinstance(meta, dict):
                meta = json.loads(meta) if meta else {}
            hits.append(
                {
                    "id": row.node_id,
                    "document": row.document,
                    "metadata": meta,
                    "distance": float(row.distance),
                }
            )
        return hits

    def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        if not self._available:
            raise RuntimeError("PgVectorStore is not available.")
        from sqlalchemy import text

        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT node_id, document, metadata FROM {self._table} "
                    "WHERE node_id = :id"
                ),
                {"id": doc_id},
            ).fetchone()
        if row is None:
            return None
        meta = row.metadata
        if not isinstance(meta, dict):
            meta = json.loads(meta) if meta else {}
        return {"id": row.node_id, "document": row.document, "metadata": meta}

    def count(self) -> int:
        if not self._available:
            return 0
        try:
            from sqlalchemy import text

            with self._engine.connect() as conn:
                row = conn.execute(text(f"SELECT count(*) FROM {self._table}")).fetchone()
            return int(row[0]) if row is not None else 0
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# where-clause translation (Chroma dict -> parameterized SQL fragment)
# ---------------------------------------------------------------------------


def _build_where_sql(
    where: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Chroma ``where`` dict를 SQL WHERE 프래그먼트(및 바인드 파라미터)로 변환.

    ``pack_id``는 전용 컬럼, 그 외 키는 ``metadata ->> :key`` JSONB 텍스트
    비교로 번역한다. 존재하지 않는 메타 키는 ``metadata ->> :key``가 SQL NULL을
    반환하고, NULL과의 모든 비교는 unknown(=WHERE에서 배제)이므로 Chroma의
    "누락 키는 매치 실패" 규칙이 SQL 3-값 논리로 자동 재현된다 — sqlite-vec의
    ``_MISSING`` sentinel 같은 명시적 분기가 필요 없다. 반환값이 ``(None, {})``
    이면 필터 없음(전체 매치)."""
    if not where:
        return None, {}
    params: dict[str, Any] = {}
    counter = [0]

    def bind(val: Any) -> str:
        counter[0] += 1
        name = f"w{counter[0]}"
        params[name] = val
        return f":{name}"

    def col_expr(key: str) -> str:
        if key == "pack_id":
            return "pack_id"
        key_param = bind(key)
        return f"(metadata ->> {key_param})"

    def eval_field(key: str, cond: Any) -> str:
        expr = col_expr(key)
        is_meta = key != "pack_id"

        def bv(v: Any) -> str:
            return bind(str(v) if is_meta else v)

        if isinstance(cond, dict):
            clauses = []
            for op, operand in cond.items():
                if op == "$in":
                    if not operand:
                        clauses.append("FALSE")
                    else:
                        names = [bv(v) for v in operand]
                        clauses.append(f"{expr} IN ({', '.join(names)})")
                elif op == "$nin":
                    frag = f"{expr} IS NOT NULL"
                    if operand:
                        names = [bv(v) for v in operand]
                        frag += f" AND {expr} NOT IN ({', '.join(names)})"
                    clauses.append(frag)
                elif op == "$eq":
                    clauses.append(f"{expr} = {bv(operand)}")
                elif op == "$ne":
                    clauses.append(f"{expr} != {bv(operand)}")
                elif op == "$gt":
                    clauses.append(f"{expr} > {bv(operand)}")
                elif op == "$gte":
                    clauses.append(f"{expr} >= {bv(operand)}")
                elif op == "$lt":
                    clauses.append(f"{expr} < {bv(operand)}")
                elif op == "$lte":
                    clauses.append(f"{expr} <= {bv(operand)}")
                else:  # 미지원 연산자 -> 보수적 비매치
                    clauses.append("FALSE")
            return " AND ".join(clauses) if clauses else "TRUE"
        # 스칼라 등가
        return f"{expr} = {bv(cond)}"

    def eval_clause(clause: dict[str, Any]) -> str:
        parts = []
        for key, cond in clause.items():
            if key == "$and":
                sub = [f"({eval_clause(c)})" for c in cond] if cond else []
                parts.append(" AND ".join(sub) if sub else "TRUE")
            elif key == "$or":
                sub = [f"({eval_clause(c)})" for c in cond] if cond else []
                parts.append(" OR ".join(sub) if sub else "FALSE")
            else:
                parts.append(eval_field(key, cond))
        return " AND ".join(parts) if parts else "TRUE"

    return eval_clause(where), params
