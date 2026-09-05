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

import json
import logging
from collections.abc import Callable
from typing import Any

from opencrab.stores._graph_common import IDENT_RE as _IDENT_RE
from opencrab.stores._json import dump_props
from opencrab.stores._sql_dialect import POSTGRES
from opencrab.stores._vector_base import (
    default_metadatas,
    embed_and_validate,
    generate_add_ids,
    generate_upsert_ids,
    reject_batch_pack_conflicts,
    reject_foreign_slot_writes,
    slot_owner,
    validate_import_records,
    validate_lengths,
)

logger = logging.getLogger(__name__)


def _to_pgvector_literal(vec: list[float]) -> str:
    """pgvector 텍스트 입력 형식 ``'[v1,v2,...]'``로 직렬화.

    psycopg2용 ``pgvector.psycopg2.register_vector`` 어댑터에 의존하지 않고
    문자열을 ``::vector``로 캐스트하는 이유: SQLAlchemy ``text()`` 바인드는
    임의 파이썬 객체 어댑터를 자동 등록하지 않으므로, 텍스트 캐스트가 드라이버/
    엔진 주입 방식(공유 엔진 포함) 어디서나 이식성 있게 동작한다.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _from_pgvector_literal(literal: str) -> list[float]:
    """Parse pgvector's ``'[v1,v2,...]'`` text output back to floats.

    NOT ``json.loads``: pgvector renders negative zero as ``-0``, which JSON
    parses as the integer ``0`` and loses the sign (measured). Per-token
    ``float()`` keeps it -- ``float("-0")`` is ``-0.0``. Subnormals survive
    either way. Callers only ever see finite values here, because
    ``validate_import_records`` rejects anything else on the way in.
    """
    body = literal.strip()[1:-1].strip()
    if not body:
        return []
    return [float(token) for token in body.split(",")]


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

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return embed_and_validate(self._ef, self._dim, texts)

    def add_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """텍스트 청크 추가(콘텐츠+시각 해시 ID, 생략 시). 중복 id는 PK 위반으로
        raise — ChromaStore(경고 후 skip)와의 의도적 차이, SqliteVecStore와 동일
        분기(add=엄격, upsert=갱신)."""
        self._require_available()
        if not texts:
            return []
        if ids is None:
            ids = generate_add_ids(texts)
        metadatas = default_metadatas(texts, metadatas)
        validate_lengths(texts, metadatas, ids)
        # 배치 전체를 먼저 직렬화해 비유한 metadata 를 여기서 거부한다(issue #82
        # 리뷰 후속) — 임베딩 호출은 되돌릴 수 없는 비용(외부 서비스 호출)일 수
        # 있으므로, 그 비용을 치르기 전에 배치 전체가 저장 가능한지 판정한다.
        serialized_metadata = [dump_props(meta) for meta in metadatas]
        vectors = self._embed(texts)

        from sqlalchemy import text

        sql = text(
            f"INSERT INTO {self._table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES (:node_id, :pack_id, (:embedding)::vector, :document, (:metadata)::jsonb)"
        )
        with self._engine.begin() as conn:
            for _id, txt, meta, vec, meta_json in zip(
                ids, texts, metadatas, vectors, serialized_metadata
            ):
                conn.execute(
                    sql,
                    {
                        "node_id": _id,
                        "pack_id": slot_owner(meta),
                        "embedding": _to_pgvector_literal(vec),
                        "document": txt,
                        "metadata": meta_json,
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
        UPSERT를 지원).

        소유권 [#197]: 소유된 슬롯은 그 소유자만 다시 쓴다. 두 층이 강제한다.

        층 1 은 선검사다. 배치 전체를 적용 전에 판정하고 호출자가 보는 오류를
        만든다. 층 2 는 ``DO UPDATE`` 자신의 ``WHERE`` 술어다. 선검사의 SELECT 는
        기본 READ COMMITTED 에서 잠금 없이 읽으므로 그 뒤 다른 트랜잭션이 슬롯
        소유자를 바꿀 수 있다. ``ON CONFLICT`` 는 충돌 행을 잠근 채 술어를
        평가하므로 그 창이 닫힌다. 술어가 거짓이면 그 문장의 ``rowcount`` 가 0 이
        되고, 여기서 그것을 위반으로 읽어 예외를 낸다. 실측(PostgreSQL 16.14,
        psycopg2): 신규 삽입, 같은 팩 갱신, 미소유 슬롯 인수는 전부 1 이고 교차
        팩 시도만 0 이다 — 0 은 소유권 위반 말고 다른 원인으로 나오지 않는다.

        술어가 `IS NULL` 을 먼저 보는 이유: `pack_id` 컬럼은 NOT NULL 이 아니고, SQL
        에서 `NULL = ''` 은 거짓이 아니라 NULL 이다. 그것을 안 보면 저장된 값이
        NULL 인 행에서 술어 전체가 NULL 로 평가돼 갱신이 안 되고, 층 1 이 같은 행을
        미소유로 읽어 통과시킨 것과 판정이 갈린다(층 1 은 `slot_owner` 를 거쳐
        None 과 빈 문자열과 부재를 한 상태로 접는다). 이 저장소의 쓰기 경로는 전부
        빈 문자열을 넣으므로 NULL 행은 외부 기록에만 생기지만, 두 층이 갈리는 것
        자체가 계약 결함이라 술어 쪽을 층 1 에 맞춘다.
        """
        self._require_available()
        if not texts:
            return []
        if ids is None:
            ids = generate_upsert_ids(texts)
        metadatas = default_metadatas(texts, metadatas)
        validate_lengths(texts, metadatas, ids)
        reject_batch_pack_conflicts(ids, metadatas)
        # 배치 전체를 먼저 직렬화한다(issue #82 리뷰 후속) — 그래야 이 뒤의
        # 임베딩 호출과 소유권 확인 SELECT(둘 다 되돌릴 수 없거나 비용이 드는
        # 작업)가 저장 불가능한 배치에 대해 헛수고로 실행되지 않는다.
        serialized_metadata = [dump_props(meta) for meta in metadatas]
        vectors = self._embed(texts)

        from sqlalchemy import text

        sql = text(
            f"INSERT INTO {self._table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES (:node_id, :pack_id, (:embedding)::vector, :document, (:metadata)::jsonb) "
            "ON CONFLICT (node_id) DO UPDATE SET "
            "pack_id = EXCLUDED.pack_id, embedding = EXCLUDED.embedding, "
            "document = EXCLUDED.document, metadata = EXCLUDED.metadata "
            f"WHERE {self._table}.pack_id IS NULL "
            f"OR {self._table}.pack_id = '' "
            f"OR {self._table}.pack_id = EXCLUDED.pack_id"
        )
        with self._engine.begin() as conn:
            reject_foreign_slot_writes(ids, metadatas, self._slot_owners(conn, ids))
            for _id, txt, meta, vec, meta_json in zip(
                ids, texts, metadatas, vectors, serialized_metadata
            ):
                result = conn.execute(
                    sql,
                    {
                        "node_id": _id,
                        "pack_id": slot_owner(meta),
                        "embedding": _to_pgvector_literal(vec),
                        "document": txt,
                        "metadata": meta_json,
                    },
                )
                if result.rowcount == 0:
                    # 층 2. 선검사를 지나친 경쟁만 여기 온다 — 같은 예외 형태로
                    # 감싸 호출자가 층을 구분하지 않게 한다. 이 예외는
                    # `engine.begin()` 안이라 배치 전체를 롤백시킨다.
                    raise ValueError(
                        "upsert_texts: refusing to take over a slot already "
                        f"attributed to a different pack ({_id!r}); the row "
                        "changed attribution between the ownership check and "
                        "this write"
                    )
        return ids

    def _slot_owners(self, conn, ids: list[str]) -> dict[str, str]:
        """``node_id`` -> 그 슬롯을 소유한 ``pack_id``. 행이 있는 id 만 담는다.

        바인드 하나로 배열을 넘긴다(``_sql_dialect.in_string_array``). 배치 크기는
        호출자가 정하므로 id 당 바인드 하나로 펼치면 상한에 걸린다. 행이 없는 id 는
        결과에 없고, 게이트는 그 부재를 "새 슬롯" 으로 읽는다.
        """
        from sqlalchemy import text

        frag, transform = POSTGRES.in_string_array("node_id", ":slot_ids")
        rows = conn.execute(
            text(f"SELECT node_id, pack_id FROM {self._table} WHERE {frag}"),  # noqa: S608
            {"slot_ids": transform(list(ids))},
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def delete(self, ids: list[str]) -> None:
        self._require_available()
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
        self._require_available()
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
        self._require_available()
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
        self._require_available()
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

    # ------------------------------------------------------------------
    # 팩 단위 raw export/import (#200)
    # ------------------------------------------------------------------

    def export_pack_vectors(self, pack_id: str) -> list[dict[str, Any]]:
        """이 팩이 소유한 벡터 전량(임베딩 포함). 계약은 ``_vector_base.py`` 의
        "pack-scoped raw vector export/import" 절 참고.

        팩 술어는 ``pack_id`` 컬럼 — ``pack/load.py`` 의 ``_live_vec_ids`` /
        ``pack_live_counts`` 가 쓰는 것과 같다. LIMIT 을 걸지 않는다: 소비자
        (``pack_fork``)는 전량이 필요하고, 잘린 export 는 조용히 불완전한 사본을
        만든다.

        metadata 는 이 스토어의 ``query``/``get_by_id`` 와 같은 방어를 쓴다
        (드라이버가 jsonb 를 dict 로 주지 않는 경우 대비).
        """
        self._require_available()
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT node_id, embedding::text AS embedding, document, metadata "
                    f"FROM {self._table} WHERE pack_id = :pack"
                ),
                {"pack": pack_id},
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            meta = row.metadata
            if not isinstance(meta, dict):
                meta = json.loads(meta) if meta else {}
            records.append(
                {
                    "id": row.node_id,
                    "embedding": _from_pgvector_literal(row.embedding),
                    "document": row.document,
                    "metadata": meta,
                }
            )
        return records

    def import_vectors(
        self, records: list[dict[str, Any]], *, pack_id: str
    ) -> list[str]:
        """export 한 레코드를 재임베딩 없이 ``pack_id`` 로 넣는다.

        ADD 의미론 — 이미 있는 ``node_id`` 는 PK 위반으로 raise 한다(node_id 는
        팩과 무관한 전역 키라, 존재한다는 것은 남의 팩 슬롯이라는 뜻이다).
        ``engine.begin()`` 단일 트랜잭션이므로 중간 충돌은 전량 롤백된다.
        INSERT 는 ``add_texts`` 와 같은 문장이고 임베딩만 레코드에서 온다 —
        ``_embed`` 를 타지 않는다.
        """
        self._require_available()
        clean = validate_import_records(records, pack_id=pack_id, dim=self._dim)
        if not clean:
            return []
        # 트랜잭션을 열기 전에 배치 전체를 직렬화한다(issue #82 리뷰 후속) —
        # `validate_import_records` 는 metadata 값을 검사하지 않으므로(모듈
        # docstring), 이 직렬화가 비유한값을 잡는 첫 지점이다. 여기서 미리
        # 하지 않으면 앞선 레코드가 이미 INSERT 된 뒤에야 뒤쪽 레코드에서
        # 예외가 나고(롤백되어 손상은 없지만 헛수고인 왕복이 남는다).
        serialized_metadata = [dump_props(record["metadata"]) for record in clean]
        from sqlalchemy import text

        sql = text(
            f"INSERT INTO {self._table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES (:node_id, :pack_id, (:embedding)::vector, :document, (:metadata)::jsonb)"
        )
        with self._engine.begin() as conn:
            for record, meta_json in zip(clean, serialized_metadata):
                meta = record["metadata"]
                conn.execute(
                    sql,
                    {
                        "node_id": record["id"],
                        "pack_id": slot_owner(meta),
                        "embedding": _to_pgvector_literal(record["embedding"]),
                        "document": record["document"],
                        "metadata": meta_json,
                    },
                )
        return [record["id"] for record in clean]

    def count(self) -> int:
        if not self._available:
            return 0
        from sqlalchemy import text

        with self._engine.connect() as conn:
            row = conn.execute(text(f"SELECT count(*) FROM {self._table}")).fetchone()
        return int(row[0]) if row is not None else 0


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
                    elif key == "pack_id":
                        # issue #147 §3.4(c): pack_id's $in is where
                        # readable_pack_ids(principal) — every non-private
                        # pack in the deployment, unbounded by anything the
                        # caller controls — actually lands, via
                        # _build_chroma_where. The generic per-value `IN
                        # (:w1, :w2, ...)` branch below would bind one
                        # parameter per pack; at deployment scale that risks
                        # the same "too many bind parameters" failure mode
                        # the graph/doc stores' `in_string_array` conversion
                        # exists to avoid (see _sql_dialect.py). One array
                        # bind (`= ANY(CAST(:w AS text[]))`) sidesteps it
                        # regardless of scope size — reusing
                        # POSTGRES.in_string_array for the SQL text keeps
                        # this in sync with the graph/doc stores' identical
                        # array-bind form instead of hand-duplicating it.
                        name = bind([str(v) for v in operand])
                        frag, _transform = POSTGRES.in_string_array(expr, name)
                        clauses.append(frag)
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
