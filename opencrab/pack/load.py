"""팩 적재 계층 — `{nodes,edges,chunks}.jsonl` 을 4스토어(graph/doc/sql/vector)에 반영.

생산자(`opencrab.pack.build`)와 소비자(여기)가 서로 다른 리포에 있던 동안 아무도 둘을
대조하지 않았고, 그래서 노드 커스텀 필드 91만 건이 파일에는 있는데 라이브에는 없는 상태로
방치됐다 — 생산자는 props 를 노드 최상위에 펼쳤고 소비자는 중첩 `properties` 만 읽었다.
어느 게이트도 잡지 못했다. 계약(`schema`)·생산자(`build`)·정규화(`normalize`)·소비자(여기)를
한 패키지에 모아 빌드에서 적재까지 한 스위트로 왕복 검증할 수 있게 하는 것이 이 이관이다.

**쓰기 함수는 각자 `require_live_data()` 를 부른다.** 진입점에서 한 번 부르는 방식은
진입점을 안 거치고 이 함수들을 직접 호출하는 경로에서 통째로 빠진다(실측: 그런 호출
스크립트가 3종 있었다). 계약은 `tests/test_pack_load.py` 가 AST 로 건다.

**스토어 private `_conn` 속성 직접 접근은 0곳이다(r11 P1, #142 재리뷰).** 종전엔
`graph._conn`/`docs._conn` 을 직접 열어 sqlite 방언 SQL(`?` 위치 파라미터·`json_extract`·
`GLOB`)을 실행했다 — PG 스토어(`PGGraphStore`/`PgDocStore`)의 `_conn` 은 속성이 아니라
`@contextmanager` **메서드**라 이 모듈은 PG 모드에서 전멸했다. 지금은 두 스토어가 이미
노출하는 중립 훅(`_fetch_all`/`_fetch_one`/`_exec_write`(`:name` named 파라미터)·
`_dialect.json_get`/`_table(...)`)만 거친다 — sqlite/PG 어느 쪽에서도 이 모듈은 `_conn` 을
모른다. 재현: `tests/test_pack_load.py::TestNoRawConnAccess`(AST 스캔, vec 백엔드 판별용
`getattr(vec, "_conn", None)` 1곳만 명시 예외).

스토어 protocol(`opencrab/stores/_graph_protocol.py`)에는 여전히 삭제·카운트 API 가 없다 —
그 사실 자체는 이번 전환으로 안 바뀐다. protocol 승격은 4백엔드 전부 구현을 요구하므로
별건으로 다룬다.

**`sys.exit` 가 `incremental_finalize` 안에 3곳 있다**(증분 삭제 안전핀). 라이브러리
코드로서는 부적절하지만 이 커밋은 순수 이동이라 행동을 바꾸지 않는다. 예외로의 승격도 별건.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from opencrab.ontology.builder import OntologyBuilder, store_write_failures
from opencrab.pack.jsonl_io import iter_jsonl
from opencrab.pack.live_data import require_live_data
from opencrab.pack.normalize import (
    resolve_edge,
    transform_chunk_meta,
    transform_node,
)
from opencrab.stores._sql_dialect import SQLITE, SqlDialect

# 로거 이름은 `__name__` 이다. 이관 전에는 호출자 스크립트 파일명으로 고정돼 있었는데
# 그 이름에 의존하는 곳은 정의 자신뿐이었다(전수 grep 1건).
log = logging.getLogger(__name__)

# 스토어가 저장하면서 **자기가 채워 넣는** 키. 증분 비교에서 빼야 한다 —
# 넣지 않으면 by-pack 원본과 라이브가 영원히 다르게 보여 **매 증분마다 전량 재적재**된다.
#
# 한동안 `id` 하나만 뺐는데, 상류가 `space_id`/`properties[space]` 우선순위를 통합하면서
# `space` 도 주입하게 됐고(#125) 그 순간 동일한 행이 전부 chg 로 판정됐다.
# 이름을 하나 더 적는 대신 "스토어가 넣는 것"이라는 축으로 묶는다.
STORE_INJECTED_KEYS = frozenset({"id", "space"})


# ── 방언 중립 SQL 빌더(r11 P1, #142 재리뷰) ─────────────────────────────
#
# 이 절 전체가 `_conn` 직접 접근·sqlite 전용 SQL 을 걷어내고 두 스토어가 이미
# 노출하는 중립 훅(`_fetch_all`/`_fetch_one`/`_exec_write`(`:name`)·
# `_dialect.json_get`/`_table`)으로 전환하는 자리다. `ANCHOR_SQL`/`COUNT_SQL`/
# `COUNT_SQL_ARGC` 는 **레거시 export** 로 남는다(호출자 호환) — 지금은
# `build_anchor_sql(SQLITE)`/`build_count_sql(SQLITE)` 산출물에서 기계 파생된다.


def _json_str_eq(dialect: SqlDialect, col: str, key: str, param: str) -> str:
    """JSON 필드의 **문자열 스칼라 전용** 등가 비교 — `col->key == :param`.

    바닥 `json_extract`/`->>` 등가는 타입을 안 가린다(JSON 정수·불리언·복합값도
    텍스트로 변환돼 매치될 수 있다). `pack_id`/`source` 는 실측상 전량 문자열이라
    (129팩 대사, v6 검수 — 갈리는 사례 0건) 문자열 전용으로 좁혀도 행 집합이
    바뀌지 않는다. sqlite `json_type=... AND json_extract=...` / PG
    `jsonb_typeof=... AND ->>=...` — 두 형 모두 "이 키가 JSON 문자열이고 그 값이
    param 과 같다"만 참으로 본다. 비문자열 `pack_id`(정의된 비지원, docstring)는
    이 술어 아래서는 항상 거짓이다.
    """
    if dialect.name == "sqlite":
        return (f"json_type({col}, '$.{key}') = 'text' AND "
                f"json_extract({col}, '$.{key}') = :{param}")
    return f"jsonb_typeof({col}->'{key}') = 'string' AND {col}->>'{key}' = :{param}"


def _in_names(prefix: str, seq: list[str]) -> tuple[str, dict[str, str]]:
    """`IN (...)` 목록을 named 플레이스홀더로 전개 — `_sql_graph_base.py:717-723`
    의 `_SqlGraphStoreBase._in_placeholders`(스토어 정적 메서드)와 같은 형태다.
    그 메서드는 스토어 인스턴스에 묶여 있어 load.py(호출자)에서 직접 재사용할
    수 없다 — 이 지역 사본이 세 번째 사본으로 또 갈리지 않도록 출처를 여기 적는다.

    빈 `seq` 는 `("", {})` 를 낸다 — 현재 호출 지점은 전부 `_batched(...)` 를 거쳐
    도달하므로(`_batched([])` 는 배치를 하나도 안 낸다) 실행 시 이 분기에 닿지
    않지만, PG 는 `IN ()` 을 문법 오류로 거부하므로(sqlite 도 마찬가지) 방어적으로
    조기 반환한다.
    """
    if not seq:
        return "", {}
    names = [f"{prefix}{i}" for i in range(len(seq))]
    return ", ".join(f":{n}" for n in names), dict(zip(names, seq, strict=True))


def build_anchor_sql(dialect: SqlDialect) -> str:
    """앵커 노드 판정(F4-a) SQL — **양성 술어**(호출부가 필요시 `AND NOT (...)` 로
    감싼다. `_is_anchor_node` 의 SQL 쪽 정본, 두 방언에서 같은 판정을 낸다.

    `dataset:` 프리픽스 노드나 title-backfill 이 만든 노드는 graph 트윈이 없거나
    있어도 삭제 후보에서 빼야 한다 — 이 판정을 Python 술어(`_is_anchor_node`)와
    SQL WHERE 조각에서 각자 구현하면 갈린다.

    대소문자 함정: sqlite `LIKE` 는 ASCII 대소문자를 무시해서 `DATASET:x` 도
    앵커로 잡는데 Python `str.startswith` 는 그 행을 안 잡는다 — 그러면 graph
    축과 doc 축이 서로 다른 노드를 앵커로 본다. sqlite 는 `GLOB`(대소문자 구분)
    을 쓴다. PG 의 `LIKE` 는 (sqlite 와 달리) 기본이 대소문자 구분이라 그대로
    Python 쪽과 일치한다(v6 검수 실증: `text()` 바인드 오인 없음·이스케이프 정확).
    """
    created_by = dialect.json_get("properties", "created_by")
    prefix_pred = "node_id GLOB 'dataset:*'" if dialect.name == "sqlite" else "node_id LIKE 'dataset:%'"
    return f"({prefix_pred} OR COALESCE({created_by},'') = 'title-backfill')"


def build_count_sql(
    dialect: SqlDialect, *,
    graph_table: Callable[[str], str] | None = None,
    doc_table: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """4축 대사 카운트 SQL(named 플레이스홀더, 문자열 전용 스칼라 정책) —
    `pack_live_counts()` 의 정본. `graph_table`/`doc_table` 은 축별 테이블명
    리졸버(기본 bare = 현행과 동일한 테이블명 — PG 는 `store._table` 을 넘겨
    스키마 프리픽스를 받는다).

    파라미터 이름은 **의도적으로 재사용**한다(`:pack` 이 한 쿼리 안에 여러 번
    나올 수 있다) — sqlite3 named 스타일도 SQLAlchemy `text()` 도 같은 이름의
    반복 바인드를 지원하므로 호출자는 `{"pack": pack_name}` 하나만 넘기면 된다.
    """
    gt = graph_table or (lambda n: n)
    dt = doc_table or (lambda n: n)
    node_pred = _json_str_eq(dialect, "properties", "pack_id", "pack")
    edge_pred = _json_str_eq(dialect, "properties", "pack_id", "pack")
    # docs 는 **두 형태**로 태그돼 있다(pack_id·source) — 한쪽만 세면 조용히
    # 적게 나온다(실측: 5벌 사본 중 하나가 이 함정에 걸렸다, 2026-08-11).
    doc_pred = (f"{_json_str_eq(dialect, 'metadata', 'pack_id', 'pack')}"
                f" OR {_json_str_eq(dialect, 'metadata', 'source', 'pack')}")
    return {
        "nodes": f"SELECT COUNT(*) AS n FROM {gt('graph_nodes')} WHERE {node_pred}",
        "edges": f"SELECT COUNT(*) AS n FROM {gt('graph_edges')} WHERE {edge_pred}",
        "docs": f"SELECT COUNT(*) AS n FROM {dt('doc_sources')} WHERE {doc_pred}",
    }


def _as_json_dict(value) -> dict:
    """JSON 컬럼 값 정규화(그래프/문서 축 공용). sqlite 는 TEXT 로, PG(jsonb) 는
    드라이버가 이미 dict 로 디코드해 돌려준다 — 이 함수 하나가 양쪽을 받는다.
    `None` 은 빈 dict(컬럼 기본값 `'{}'` 과 같은 의미)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return dict(value)


_NAMED_TOKEN_RE = re.compile(r":[A-Za-z_]\w*")


def _assert_no_named_token_in_string_literals(sql: str) -> None:
    """`:name` 토큰이 SQL 문자열 리터럴(작은따옴표 구간) 안에 있으면 아래
    named→qmark 위치 전개가 리터럴 내용을 오염시킨다 — 그런 리터럴이 없는지
    정적으로 확인한다(게이트 ⑨). 이 모듈이 생성하는 SQL 은 이스케이프된
    작은따옴표를 쓰지 않으므로 단순 홀짝 분리로 충분하다.
    """
    parts = sql.split("'")
    literals = parts[1::2]  # 홀수 인덱스가 따옴표로 감싸인 구간
    for lit in literals:
        assert not _NAMED_TOKEN_RE.search(lit), (
            f"SQL 리터럴 안에 named 토큰이 있다 — named→qmark 전개가 리터럴을 깬다: {lit!r}")


def _named_to_qmark(sql: str) -> tuple[str, int]:
    """named(`:name`) 플레이스홀더를 qmark(`?`) 위치 파라미터로 전개 — 레거시
    `COUNT_SQL` export 의 파생 규칙. ARGC 는 치환된 토큰 출현 수(같은 이름이
    반복돼도 각 출현이 위치 파라미터 하나다)."""
    _assert_no_named_token_in_string_literals(sql)
    argc = 0

    def _sub(_m: re.Match) -> str:
        nonlocal argc
        argc += 1
        return "?"

    return _NAMED_TOKEN_RE.sub(_sub, sql), argc


# ── 앵커 SQL — sqlite 방언 산출물이 레거시 export다(과거 리터럴과 공백만 다르다:
# `_dialect.json_get` 이 쉼표 뒤 공백을 낸다 — 의미는 동일, gate ④가 공백
# 정규화로 확인한다).
ANCHOR_SQL = build_anchor_sql(SQLITE)


def _is_anchor_node(node_id: str, props: dict) -> bool:
    """앵커 노드 판정의 Python 쪽 정본. `ANCHOR_SQL` 과 같은 조건을 낸다.

    `live_nodes` 같은 특정 dict 를 안에서 조회하지 않는다 — 호출자가 props 를
    직접 넘긴다. 예전 구현은 `incremental_finalize` 지역 함수가 `live_nodes`
    를 닫혀서 참조했는데, `live_nodes` 밖 node_id(예: doc_nodes 에만 있는 앵커)
    는 조회하면 빈 dict 를 받아 앵커가 앵커로 안 잡혔다.
    """
    if node_id.startswith("dataset:"):
        return True
    return props.get("created_by") == "title-backfill"


def _batched(seq: list, size: int = 500):
    """SQLite 파라미터 상한(기본 999) 회피용 배치 분할."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# 4축 대사 쿼리 **레거시 qmark export**. `pack_live_counts()` 자신은 이제
# `build_count_sql()` 의 named-플레이스홀더 산출물을 `_fetch_one` 으로 실행한다
# (아래 정의) — 이 딕셔너리는 스토어 객체 없이 raw sqlite3 로 세는 호출자용
# 하위호환이다(그런 호출자는 `sqlite3.connect(..., mode=ro)` 로 파일을 직접 열어
# 스토어 훅을 못 쓴다). `build_count_sql(SQLITE)` 산출물에서 named→qmark 로
# **기계 파생**한다 — 손으로 두 벌을 유지하지 않는다(과거 5벌 사본 중 하나가
# `$.source` 절을 빠뜨려 그 형태로 태그된 행을 통째로 못 셌다, 2026-08-11 적대
# 검증). 행 집합은 `build_count_sql` 과 동일(문자열 전용 스칼라 정책은 양쪽 다
# 적용) — 리터럴 텍스트는 과거 손으로 쓴 버전과 다르다(정상, 파생 규칙 산출물).
_COUNT_SQL_NAMED = build_count_sql(SQLITE)
COUNT_SQL: dict[str, str] = {}
COUNT_SQL_ARGC: dict[str, int] = {}
for _axis, _sql in _COUNT_SQL_NAMED.items():
    COUNT_SQL[_axis], COUNT_SQL_ARGC[_axis] = _named_to_qmark(_sql)
del _axis, _sql, _COUNT_SQL_NAMED


def _vec_backend(vec):
    """벡터 스토어가 **팩 단위 연산을 어떤 방식으로 지원하는가**.

    반환: `("sql", conn, table)` · `("chroma", collection, None)` ·
          `("sqlalchemy", engine, table)` · `(None, None, None)`

    **왜 한 자리로 모으는가.** 종전에는 삭제(`delete_pack`)·수집(`live_pack_state`)이
    각자 `getattr(vec, "_conn")` / `hasattr(vec, "_collection")` 를 따로 열거했고,
    그 목록에서 **pgvector 가 빠져 있었다**(`_engine`/`_table` 만 노출한다).
    결과는 조용한 실패였다 — 삭제는 "성공"을 보고하면서 벡터가 전부 남고,
    증분은 `vec_ids` 가 항상 비어 고아 임베딩을 영영 못 지운다(2026-08-11 리뷰 지적).

    같은 열거를 두 곳에 두면 한쪽만 고쳐진다. 판별을 여기 하나로 두고,
    **지원하지 않는 백엔드는 `None` 으로 명시**해 호출자가 조용히 0 을 내지 않게 한다.
    """
    if not getattr(vec, "available", False):
        return (None, None, None)
    conn = getattr(vec, "_conn", None) or getattr(vec, "conn", None)
    if conn is not None:
        return ("sql", conn, getattr(vec, "_table", None) or getattr(vec, "table_name", "vectors_kure"))
    if hasattr(vec, "_collection"):
        return ("chroma", vec._collection, None)
    engine = getattr(vec, "_engine", None)
    if engine is not None:
        return ("sqlalchemy", engine, getattr(vec, "_table", None) or "vectors")
    return (None, None, None)


def _vec_meta_update(vec, chunk_id: str, meta: dict) -> bool:
    """벡터 레코드의 **메타데이터만** 갱신. 성공하면 True.

    텍스트가 안 바뀌었으니 임베딩은 그대로 두고 메타만 맞춘다. 백엔드가 그 연산을
    지원하지 않으면 **False 를 돌려 호출자가 재임베딩으로 우회**하게 한다 —
    조용히 True 를 내면 그 어긋남이 영구히 남는다(다음 증분이 "동일"로 판정하므로).

    **chroma 분기의 한계(2026-08-13 실측 근거)**: chromadb 1.5.7 의 `update`/`upsert`
    는 메타를 **병합**하고(겹치는 키만 갱신, 그 외 키는 존속) `delete`+`add` 만
    **치환**한다. 스테일 키(청크 스키마가 축소되어 없어진 옛 메타 키)를 없애려면
    치환이 필요해 이 분기는 delete+add 를 쓴다. 그 대가로 세 한계를 그대로 안는다:
    ① **단일 작성자 전제** — delete 와 add 사이의 창에서 동시 writer 가 같은
    레코드를 건드리면(TOCTOU) 그 갱신이 사라질 수 있다. 이 함수의 모든 호출자는
    단일 프로세스 순차 적재이므로 이 창은 실측상 열리지 않지만, 코드 자체가 그것을
    보장하지는 않는다. ② **URI 보존은 이 함수의 성공 조건이 아니다** — URI 가 붙은
    레코드를 만나면 치환하지 않고 False 로 물러나 호출자가 upsert 병합으로
    우회하게 한다(아래 참고). ③ **스테일 키 병합 창**(delete 실패 시 호출자가
    upsert 로 병합 갱신하면 겹치는 키는 새 값, 그 외 옛 키는 그대로 남는다)은
    닫지 않는다 — 그 창은 `localcrab#175` 로 등록돼 있다.
    """
    # 백엔드가 전용 API 를 내놓으면 그것을 쓴다 — 내부 속성을 뒤지는 것보다 낫고,
    # 테스트 더블도 이 축으로 실계약을 흉내낼 수 있다.
    updater = getattr(vec, "update_metadata", None)
    if callable(updater):
        try:
            return bool(updater(chunk_id, meta))
        except Exception as exc:                              # noqa: BLE001
            log.warning("벡터 메타 갱신 실패(%s): %s — 재임베딩으로 우회한다", chunk_id, exc)
            return False

    kind, handle, table = _vec_backend(vec)
    import json as _json
    try:
        if kind == "sql":
            cur = handle.execute(f"UPDATE {table} SET metadata = ? WHERE node_id = ?",
                                  (_json.dumps(meta, ensure_ascii=False), chunk_id))
            handle.commit()
            # rowcount == 0이면 UPDATE가 아무 행도 못 건드린 것이다(node_id가 벡터
            # 테이블에 없음 등) — 그런데도 True를 돌려주면 호출자가 "메타를 고쳤다"고
            # 믿고 doc 기준을 옮기고, 벡터는 옛 메타 그대로 남아 다음 증분이 c_same으로
            # 넘어간다(영구 불일치). rowcount를 봐야 재임베딩 경로로 보낼 수 있다.
            return bool(cur.rowcount)
        if kind == "chroma":
            got = handle.get(ids=[chunk_id],
                             include=["embeddings", "documents", "uris", "metadatas"])
            if not got["ids"]:
                return False                      # 부재 — 재임베딩(upsert=add, 정확 메타)
            if got.get("uris") and got["uris"][0] is not None:
                # URI 레코드는 이 시스템이 생산하지 않는다(uris API 사용 전수 검색 0).
                # 외부 기록으로 보고 파괴적 치환을 하지 않는다. False → upsert 병합:
                # URI 보존·값 갱신 실측. 여분 스테일 키 창은 localcrab#175.
                log.warning("URI 벡터 레코드(%s) — 치환 대신 병합 갱신으로 우회한다", chunk_id)
                return False
            try:
                handle.delete(ids=[chunk_id])      # delete 도 try 안 — 예외는 False 로 수렴
                handle.add(ids=[chunk_id], embeddings=got["embeddings"],
                           documents=got["documents"], metadatas=[meta])
            except Exception as exc:               # noqa: BLE001
                # 원상복구는 하지 않는다(v11 검수 실증: 복구로 레코드가 남으면 호출자
                # upsert 가 구 메타와 병합해 스테일 키가 영구화된다). 이 분기에 오는
                # 레코드는 URI 없는 시스템 생산분뿐이라 **부재가 정확 치유 상태**다 —
                # 재임베딩 upsert=add 가 임베딩·문서·메타를 전부 정본에서 재생성한다.
                # 재임베딩마저 실패하면 청크가 실패로 기록되고 기준선이 안 전진해
                # 다음 실행이 재시도한다(기존 TestVectorTotalLoss 경로).
                log.warning("벡터 치환 실패(%s): %s — 재임베딩으로 우회한다", chunk_id, exc)
                return False
            post = handle.get(ids=[chunk_id],       # 후상태 3축 검증
                              include=["embeddings", "documents", "metadatas"])
            ok = (post["ids"] == [chunk_id]          # ID 동일성까지 (v14 검수)
                  and post["metadatas"][0] == meta
                  and post["documents"][0] == got["documents"][0]
                  and post["embeddings"] is not None and post["embeddings"][0] is not None
                  and len(post["embeddings"][0]) == len(got["embeddings"][0])
                  and all(abs(float(a) - float(b)) <= 1e-6 + 1e-6 * abs(float(b))
                          for a, b in zip(post["embeddings"][0], got["embeddings"][0])))
            if not ok:
                log.warning("치환 후검증 실패(%s) — 재임베딩으로 우회한다", chunk_id)
                return False
            return True
    except Exception as exc:                                  # noqa: BLE001
        log.warning("벡터 메타 갱신 실패(%s): %s — 재임베딩으로 우회한다", chunk_id, exc)
    return False


# graph/doc 두 축이 이 모듈의 SQL 진입점(`pack_live_counts`·`delete_pack`·
# `live_pack_state`)에 필요로 하는 훅 세트. graph 축엔 `_row_get` 이 없다
# (`_sql_graph_base.py` 의 계약 — 위치 접근이 정착 관례).
_GRAPH_SQL_HOOKS = ("_table", "_fetch_all", "_fetch_one", "_exec_write")
_DOC_SQL_HOOKS = ("_table", "_fetch_all", "_fetch_one", "_exec_write", "_row_get")


def _require_sql_hooks(store, hooks: tuple[str, ...], label: str) -> None:
    """비SQL 스토어(예: Kuzu — `_conn` 은 있으나 이 훅 세트가 없다)는 이 모듈의
    적재/회수 진입점을 쓸 수 없다. 예전엔 그런 스토어로 부르면 `_conn` 없는
    속성 접근이 `AttributeError` 로 죽었다 — 원인이 모호했다. 여기서 명시적으로
    거부해 무엇이 왜 안 되는지 말한다."""
    missing = [h for h in hooks if not hasattr(store, h)]
    if missing:
        raise NotImplementedError(
            f"pack 적재/회수는 SQL 계열 graph/doc 스토어 전용입니다"
            f"({label} 이 {', '.join(missing)} 훅이 없습니다: {type(store).__name__})")


def pack_live_counts(pack_name: str, graph, docs, vec) -> dict[str, int | None]:
    """팩 하나의 **라이브 4축 카운트**. 적재 전후 대사의 정본이다.

    노드·엣지·문서·벡터를 스토어에서 직접 센다. 이 SQL 은 **스토어 스키마 세부**라
    호출자 리포가 아니라 여기가 자리다 — 호출자가 같은 쿼리를 따로 적어 두면 스키마가
    바뀔 때 한쪽만 고쳐지고, 그 어긋남은 "카운트가 안 맞는다"로만 보여 원인 추적이 어렵다.
    실제로 `incremental_finalize` 와 호출자의 대사 스크립트가 **같은 쿼리를 두 벌** 갖고
    있었다(2026-08-11 이관 정리에서 통합).

    **엣지축은 2026-07-30 에 추가됐다.** 노드·문서·벡터만 대사하던 동안 by-pack 대비
    미적재 엣지 187,069건(15팩)이 전 팩 "정상" 판정을 통과했다. 축을 하나 빼면 그 축의
    결손은 영영 안 보인다.

    결손의 **원인**(grammar 공간쌍 미정의 등)은 판정하지 않는다 — 숫자만 낸다.

    **`vectors` 는 `int | None`.** 벡터 카운트는 `_vec_backend()` 를 거친다 —
    종전에는 `getattr(vec, "_conn")` 를 직접 봐서 Chroma·pgvector 에서 **항상 0**
    이었다(둘 다 `_conn` 을 안 낸다). 팩 단위 카운트를 낼 수 있으면(`sql`/
    `chroma`/`sqlalchemy`) 실제로 세어 `int` 를 돌려주고(빈 스토어면 `0`), 낼 수
    없으면(백엔드 미지원·미가용) `None` 을 돌려준다. **`0` 과 `None` 은 다른
    사실이다** — `0` 은 "세어보니 없다", `None` 은 "셀 방법이 없어 모른다"다.
    이 둘을 섞으면 "벡터 0건"이라는 잘못된 결론이 조용히 보고서에 남는다.

    graph/doc SQL 은 `build_count_sql()`(named 플레이스홀더, 방언 중립)을
    `_fetch_one` 으로 실행한다 — sqlite/PG 어느 쪽에서도 `_conn` 을 직접 열지
    않는다(r11 P1). graph 축은 SELECT 컬럼이 COUNT 하나뿐이라 위치 접근(`[0]`)
    이고, doc 축은 `_row_get` 로 읽는다(두 축의 기존 관례 그대로).
    """
    _require_sql_hooks(graph, _GRAPH_SQL_HOOKS, "graph 스토어")
    _require_sql_hooks(docs, _DOC_SQL_HOOKS, "doc 스토어")
    graph_sql = build_count_sql(graph._dialect, graph_table=graph._table)
    doc_sql = build_count_sql(docs._dialect, doc_table=docs._table)
    g = graph._fetch_one(graph_sql["nodes"], {"pack": pack_name})[0]
    e = graph._fetch_one(graph_sql["edges"], {"pack": pack_name})[0]
    d = docs._row_get(docs._fetch_one(doc_sql["docs"], {"pack": pack_name}), "n")

    v: int | None
    kind, handle, table = _vec_backend(vec)
    if kind == "sql":
        v = handle.execute(
            f"SELECT COUNT(*) FROM {table} WHERE pack_id = ?", (pack_name,)
        ).fetchone()[0]
    elif kind == "chroma":
        # F6 이후 delete_pack 의 회수 술어와 동일하게 pack_id 단일 키로 좁혔다 —
        # source 는 소유 키가 아니라 대사에도 넣지 않는다(위 graph_nodes 조회
        # 주석 참고).
        got = handle.get(where={"pack_id": pack_name})
        v = len(got.get("ids", []))
    elif kind == "sqlalchemy":
        from sqlalchemy import text as _sa_text
        with handle.connect() as _c:
            v = _c.execute(
                _sa_text(f"SELECT COUNT(*) FROM {table} WHERE pack_id = :p"), {"p": pack_name}
            ).scalar()
    else:
        v = None
    return {"nodes": g, "edges": e, "docs": d, "vectors": v}


def delete_pack(pack_name: str, graph, docs, vec) -> tuple[int, int, int]:
    """기존 팩 노드·엣지(cascade)·청크를 삭제. 반환: (node_del, chunk_sql_del, chunk_vec_del)"""
    require_live_data("delete_pack")
    _require_sql_hooks(graph, _GRAPH_SQL_HOOKS, "graph 스토어")
    _require_sql_hooks(docs, _DOC_SQL_HOOKS, "doc 스토어")
    node_del = 0

    # ── graph_nodes: pack_id == pack_name 인 노드 조회 ──────────────────
    #
    # **레거시 `source` 폴백은 정확일치여야 한다.** `LIKE '%{pack}%'` 였을 때
    # 한 팩 이름이 다른 팩 이름의 **부분 문자열**이면 경계를 넘었다 — `pack-a` 를
    # 지우면 `pack_id` 가 `pack-a-v2` 인 노드까지 삭제되고 엣지가 cascade 로 따라간다
    # (재현: 그 노드의 레거시 `source` 가 "pack-a-legacy-dump" 이면 걸린다, 2026-08-11).
    # **삭제는 되돌릴 수 없다** — 경계 판정을 문자열 포함으로 하면 안 되는 자리다.
    #
    # **회수(reclaim) 술어를 `pack_id` 단일 소유 키로 좁힌다.** 한동안 `source`/
    # `source_id`/`pack` 까지 OR 로 넓게 봤는데, 그 세 키는 이 노드의 **소유**를
    # 말하지 않는다.
    #   - `source`/`source_id`: `transform_node` 가 입력의 외래 `source`/`source_id`
    #     를 properties 에 **보존**한다(`normalize.py:297-301` 이 NODE_STRUCT_KEYS
    #     밖 키를 그대로 병합). 그것을 회수 키로 두면 `pack_id` 가 다른 팩인 행이
    #     엉뚱한 `delete_pack('A')` 호출에 걸린다 — 소유 키가 아니다.
    #   - `pack`: 생산자가 `pack` 을 `pack_id` 와 **같은 값으로만** 쓴다
    #     (`normalize.py:308-309`). 넓혀도 회수 범위가 한 행도 안 늘고, rename 이
    #     `pack` 을 갱신 안 해 생기는 stale alias 를 오히려 회수 술어가 보게 만든다.
    # 대사(reconcile) 술어(COUNT_SQL·live_pack_state)와 폭이 다른 것은 의도다 —
    # `docs/pack-contract-layer.md` 의 회수/대사 분리 서술과 일치시킨다.
    space_expr = graph._dialect.json_get("properties", "space")
    node_pack_pred = _json_str_eq(graph._dialect, "properties", "pack_id", "pack")
    rows = graph._fetch_all(
        f"""
        SELECT node_type, node_id, COALESCE({space_expr}, 'concept') AS space
        FROM {graph._table('graph_nodes')}
        WHERE {node_pack_pred}
        """,
        {"pack": pack_name},
    )

    for node_type, node_id, space in rows:
        # delete_node: 노드 + 관련 엣지 cascade. bool을 돌려준다(_sql_graph_base.py
        # 4백엔드 통일 계약, "Returns True iff the node itself was deleted") —
        # 그 값을 보고서만 node_del을 센다. cascade로 함께 지워지는 엣지 수는
        # 이 반환값에 없으므로 여기서 세지 않는다(엣지는 별도 축).
        try:
            deleted = graph.delete_node(node_type, node_id)
        except Exception as exc:
            deleted = False
            log.warning("노드 삭제 오류(%s) %s/%s: %s", pack_name, node_type, node_id, exc)
        # doc_nodes 삭제
        try:
            docs.delete_node_doc(space, node_id)
        except Exception:
            pass
        if deleted:
            node_del += 1

    # ── doc_nodes: graph 트윈 없이 남은 pack_id 앵커 노드 직접 정리 ───────
    # (예: backfill이 생성한 dataset: 앵커 — graph_nodes cascade에서 누락됨)
    # 위 graph_nodes 조회와 동일하게 `pack_id` 단일 소유 키만 본다(위 주석의
    # source/source_id/pack 제외 근거를 그대로 적용한다).
    # 조회 후 행별 삭제 대신 **집합 단위 DELETE 1문장** — 삭제 집합이 바로 위
    # graph_nodes 조회와 동일한 술어(`$.pack_id`)이므로 rowcount 가 행별 삭제
    # 총합과 등가다(게이트 ⑪, 실측 대조로 확인). 조회를 유지할 이유가 없다.
    dn_pack_pred = _json_str_eq(docs._dialect, "properties", "pack_id", "pack")
    doc_node_extra_del = docs._exec_write(
        f"DELETE FROM {docs._table('doc_nodes')} WHERE {dn_pack_pred}",
        {"pack": pack_name},
    )
    node_del += doc_node_extra_del

    # ── doc_sources (청크): metadata.source == pack_name 또는 metadata.pack_id == pack_name
    # (실제 레코드는 source가 아니라 pack_id 필드에만 팩 식별자를 갖는 경우가 있음)
    src_pred = (f"{_json_str_eq(docs._dialect, 'metadata', 'source', 'pack')}"
                f" OR {_json_str_eq(docs._dialect, 'metadata', 'pack_id', 'pack')}")
    src_rows = docs._fetch_all(
        f"SELECT source_id FROM {docs._table('doc_sources')} WHERE {src_pred}",
        {"pack": pack_name},
    )
    src_ids = [docs._row_get(r, "source_id") for r in src_rows]

    doc_sources_table = docs._table("doc_sources")
    chunk_sql_del = 0
    fts_del = 0
    for batch in _batched(src_ids):
        placeholders, in_params = _in_names("sid", batch)
        if not placeholders:                          # 도달 불가(위 주석) — 방어
            continue
        chunk_sql_del += docs._exec_write(
            f"DELETE FROM {doc_sources_table} WHERE source_id IN ({placeholders})",
            in_params,
        )
        # doc_sources_fts 동기화(별도 fts5 가상 테이블 — 트리거 없이 수동 관리됨).
        # [Δ r11 P1] sqlite 전용 방언 게이트만 신설 — 순서(doc_sources 삭제 뒤
        # FTS 삭제)와 삭제 실패의 warning-삼킴은 **현행 그대로 유지**한다(FTS-first
        # 로 뒤집지 않는다). 근거: 고아 FTS 행은 `keyword_search`(doc_sources
        # 와 INNER JOIN, local_sql_doc_store.py:293)에서 안 보여 무해하고, 같은
        # source_id 가 재적재되면 `upsert_source` 의 DELETE+INSERT 가 자가
        # 치유한다(v6 검수 실측: orphan 상태 검색 결과 미포함·재업서트 후 fts
        # 행 1). 전량 회수는 재실행으로 안 되고(별건, 대사 스윕 필요) —
        # `_init_db` 의 n_fts==0 백필 가드(local_sql_doc_store.py:131)가 이
        # 고아를 건드리는 유일한 비-JOIN 소비처다(그 가드는 "FTS 가 통째로
        # 비어 있을 때"만 발동하므로 부분 고아는 못 건드린다).
        if docs._dialect.name == "sqlite":
            try:
                fts_del += docs._exec_write(
                    f"DELETE FROM doc_sources_fts WHERE source_id IN ({placeholders})",
                    in_params,
                )
            except Exception as exc:
                log.warning("doc_sources_fts 삭제 오류(%s): %s", pack_name, exc)

    # ── 벡터 삭제: SqliteVecStore(KURE, pack_id 컬럼) 우선, Chroma(_collection) 폴백 ──
    chunk_vec_del = 0
    if vec.available:
        try:
            kind, handle, table = _vec_backend(vec)
            if kind == "sql":
                cur = handle.execute(f"DELETE FROM {table} WHERE pack_id = ?", (pack_name,))
                handle.commit()
                chunk_vec_del = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            elif kind == "chroma":
                # 회수 술어 — pack_id 단일 소유 키(F6, 위 graph_nodes 조회 주석의
                # 근거와 동일: source 는 소유 키가 아니다).
                result = handle.get(where={"pack_id": pack_name})
                ids_to_del = result.get("ids", [])
                if ids_to_del:
                    handle.delete(ids=ids_to_del)
                    # Chroma의 delete()는 삭제 건수를 돌려주지 않는다 — 이 값은
                    # "요청 수"이지 확인된 삭제 수가 아니다.
                    chunk_vec_del = len(ids_to_del)
            elif kind == "sqlalchemy":
                from sqlalchemy import text as _sa_text
                with handle.begin() as _c:
                    r = _c.execute(_sa_text(f"DELETE FROM {table} WHERE pack_id = :p"),
                                   {"p": pack_name})
                    chunk_vec_del = r.rowcount or 0
            else:
                # **조용히 0 을 내지 않는다.** 지원 안 되는 백엔드면 벡터가 그대로 남는데
                # 삭제가 "성공"으로 보고되면 다음 적재가 고아 임베딩 위에 쌓인다.
                log.warning(
                    "벡터 삭제 미지원 백엔드(%s) — 팩 %s 의 벡터가 남는다. "
                    "수동 정리가 필요하다", type(vec).__name__, pack_name)
        except Exception as e:
            log.warning("벡터 delete 오류(%s): %s", pack_name, e)

    print(
        f"  [{pack_name}] 삭제: 노드+엣지 {node_del}개(doc_nodes 보강 {doc_node_extra_del}), "
        f"doc_sources {chunk_sql_del}개(fts {fts_del}), 벡터(sqlite-vec) {chunk_vec_del}개",
        flush=True,
    )
    return node_del, chunk_sql_del, chunk_vec_del


def live_pack_state(pack_name: str, graph, docs, vec) -> dict:
    """증분 대조용 라이브 상태 로드 (delete_pack과 동일한 접근 관례: 방언 중립 훅
    — `_fetch_all`/`_dialect.json_get`/`_table`, `_conn` 직접 접근 없음).

    반환 dict:
      nodes: {node_id: (node_type, space_id, props_dict)}
      chunks: {source_id: (text, metadata_dict)}
      edges: {(from_id, relation, to_id), ...}
      vec_ids: {node_id, ...}
      doc_node_spaces: {node_id: {space, ...}} — doc_nodes 축 대사용(F4). doc 축
        쿼리들은 각자 `docs._fetch_all` 로 독립 호출한다(PG 는 매 호출이 단명
        커넥션 — sqlite 처럼 하나의 커넥션을 계속 쥐고 있지 않는다). 앵커는
        뺀다 — 앵커는 삭제 후보가 아니므로 대사 대상도 아니다.
    """
    _require_sql_hooks(graph, _GRAPH_SQL_HOOKS, "graph 스토어")
    _require_sql_hooks(docs, _DOC_SQL_HOOKS, "doc 스토어")

    node_pred = _json_str_eq(graph._dialect, "properties", "pack_id", "pack")
    nodes: dict[str, tuple[str, str, dict]] = {}
    for node_type, node_id, space_id, properties in graph._fetch_all(
        f"""
        SELECT node_type, node_id, space_id, properties
        FROM {graph._table('graph_nodes')}
        WHERE {node_pred}
        """,
        {"pack": pack_name},
    ):
        nodes[node_id] = (node_type, space_id, _as_json_dict(properties))

    chunk_pred = (f"{_json_str_eq(docs._dialect, 'metadata', 'pack_id', 'pack')}"
                  f" OR {_json_str_eq(docs._dialect, 'metadata', 'source', 'pack')}")
    chunks: dict[str, tuple[str, dict]] = {}
    for row in docs._fetch_all(
        f"""
        SELECT source_id, text, metadata
        FROM {docs._table('doc_sources')}
        WHERE {chunk_pred}
        """,
        {"pack": pack_name},
    ):
        source_id = docs._row_get(row, "source_id")
        text = docs._row_get(row, "text")
        metadata = docs._row_get(row, "metadata")
        chunks[source_id] = (text, _as_json_dict(metadata))

    edge_pred = _json_str_eq(graph._dialect, "properties", "pack_id", "pack")
    edges: set[tuple[str, str, str]] = set()
    for from_id, relation, to_id in graph._fetch_all(
        f"""
        SELECT from_id, relation, to_id
        FROM {graph._table('graph_edges')}
        WHERE {edge_pred}
        """,
        {"pack": pack_name},
    ):
        edges.add((from_id, relation, to_id))

    vec_ids: set[str] = set()
    kind, handle, table = _vec_backend(vec)
    if kind == "sql":
        for (node_id,) in handle.execute(
            f"SELECT node_id FROM {table} WHERE pack_id = ?", (pack_name,)
        ).fetchall():
            vec_ids.add(node_id)
    elif kind == "chroma":
        # F6 이후 delete_pack 의 회수 술어와 동일하게 pack_id 단일 키로 좁혔다.
        # 이 vec_ids 는 incremental_finalize 에서 그대로
        # `vec_orphans = live_vecids - (bypack_node_ids | bypack_chunk_ids)` 로
        # 이어져 **삭제를 만든다** — source 까지 넓게 매치하면 pack_id 가 다른
        # 팩인 벡터 행(레거시 source 만 이 팩명과 같은 행)이 고아 후보에 섞여
        # 지워진다. F6 가 SQL 쪽에서 닫은 것과 같은 교차팩 삭제 경로다.
        got = handle.get(where={"pack_id": pack_name})
        vec_ids.update(got.get("ids", []))
    elif kind == "sqlalchemy":
        from sqlalchemy import text as _sa_text
        with handle.connect() as _c:
            for (node_id,) in _c.execute(
                _sa_text(f"SELECT node_id FROM {table} WHERE pack_id = :p"), {"p": pack_name}):
                vec_ids.add(node_id)
    elif getattr(vec, "available", False):
        # 빈 집합은 "벡터가 없다"로 읽힌다. 그러면 증분이 고아 임베딩을 **영영 못 지운다** —
        # 삭제된 노드의 벡터가 의미검색에 계속 뜬다. 모른다는 것을 말한다.
        log.warning("벡터 ID 열거 미지원 백엔드(%s) — 고아 임베딩을 판정할 수 없다",
                    type(vec).__name__)

    # doc_node_spaces (F4-b): 노드축 **대사(reconcile)** 술어(pack_id 단일 키) —
    # 위 nodes 조회와 동일한 폭이다. 회수(4키) 술어를 쓰면 `pack` 으로만 태그된
    # 행의 doc 은 지워지고 graph 는 남아 새 비대칭이 생긴다.
    dn_pred = _json_str_eq(docs._dialect, "properties", "pack_id", "pack")
    anchor_sql = build_anchor_sql(docs._dialect)
    doc_node_spaces: dict[str, set[str]] = {}
    for row in docs._fetch_all(
        f"""
        SELECT node_id, space
        FROM {docs._table('doc_nodes')}
        WHERE {dn_pred}
          AND NOT {anchor_sql}
        """,
        {"pack": pack_name},
    ):
        node_id = docs._row_get(row, "node_id")
        space = docs._row_get(row, "space")
        doc_node_spaces.setdefault(node_id, set()).add(space)

    return {
        "nodes": nodes, "chunks": chunks, "edges": edges, "vec_ids": vec_ids,
        "doc_node_spaces": doc_node_spaces,
    }


def load_nodes(
    pack_name: str,
    nodes_file: Path,
    builder: OntologyBuilder,
    id_map: dict[str, tuple[str, str]],
) -> tuple[int, int, int]:
    """노드 적재. id_map에 추가. 반환: (ok, skip, err)"""
    require_live_data("load_nodes")
    ok = skip = err = 0

    for row in iter_jsonl(nodes_file):  # shard-aware 논리 스트림(단일/분할 투명)
            space, node_type, node_id, props = transform_node(pack_name, row)
            id_map[node_id] = (space, node_type)

            try:
                res = builder.add_node(space, node_type, node_id, properties=props)
                # **영수증을 본다.** `add_node`는 스토어 실패를 예외로 올리지 않고 반환
                # dict에 적는다 — `builder.store_write_failures()`의 docstring이
                # "호출자가 이것을 불러야 실제 성공 여부를 안다"고 명시한다.
                fails = store_write_failures(res.get("stores", {}) if isinstance(res, dict) else {})
                if fails:
                    err += 1
                    log.warning("노드 저장 실패 %s (%s/%s): %s",
                                node_id, space, node_type, "; ".join(fails))
                else:
                    ok += 1
            except ValueError as ve:
                skip += 1
                log.debug("노드 문법위반 skip %s (%s/%s): %s", node_id, space, node_type, ve)
            except Exception as exc:
                err += 1
                log.warning("노드 오류 %s: %s", node_id, exc)

            done = ok + skip + err
            if done % 500 == 0:
                print(f"    …노드 {done} (ok={ok} skip={skip} err={err})", flush=True)

    return ok, skip, err


def load_nodes_incremental(
    pack_name: str,
    nodes_file: Path,
    builder: OntologyBuilder,
    id_map: dict[str, tuple[str, str]],
    live_nodes: dict[str, tuple[str, str, dict]],
    graph,
    docs,
    doc_node_spaces: dict[str, set[str]],
) -> tuple[int, int, int, int, int, set]:
    """노드 증분 적재. 라이브와 동일한 행은 완전 스킵(어떤 스토어도 미접촉).

    graph/docs는 명시 파라미터 — OntologyBuilder는 스토어를 내부명(_neo4j/_mongo)으로
    보관하므로 builder 속성 접근은 불가(vendor 실측, 2026-07-22).

    `doc_node_spaces`는 F4-b `live_pack_state` 의 반환이다 — **필수 인자**다.
    노드가 이번 적재에서 space X 로 확인됐는데 doc_nodes 에 다른 space Y 의 행이
    남아 있는 경우 그 자리에서 Y 행을 지운다(F4-c). 한동안 기본값 `None` 으로
    받아 생략을 허용했는데, 그러면 호출자가 안 넘겨도 예외 없이 이 정리가
    **조용히 꺼진다** — 그리고 그 누락을 `incremental_finalize`(F4-d)가 못 잡는다.
    F4-d 의 doc 축 후보는 `set(doc_node_spaces) - bypack_node_ids` 라 **입력에
    아직 있는 노드**(bypack_node_ids 안)의 이종 space 잔재는 그쪽 후보에 안 잡힌다
    — 그게 정확히 F4-c 의 몫이다. 조용히 꺼지면 타입 변경 잔재가 영영 안 걷힌다.
    빈 dict(`{}`)는 유효하다 — "대사할 doc 행이 없다"는 사실이고, 없는 것과는
    다르다.

    반환: (n_new, n_chg, n_same, skip, err, bypack_ids)
    """
    require_live_data("load_nodes_incremental")
    n_new = n_chg = n_same = skip = err = 0
    bypack_ids: set[str] = set()

    def _cleanup_stale_doc_spaces(node_id: str, space: str) -> None:
        """이 node_id 가 이번에 `space` 로 확정됐다 — doc_node_spaces 에 기록된
        다른 space 의 doc 행은 이제 고아다.

        `incremental_finalize` 의 doc 축 정리(F4-d)가 증분 사이에 놓친 것을
        뒤에서 한 번 더 쓸어가긴 하지만, 그 전까지 다른 space 의 stale 행이
        살아 있으면 검색이 옛 space 로도 이 노드를 반환한다. 여기서 즉시
        지우면 그 창을 없앤다. 멱등이다 — 이번에 실패해도(반환 False·예외)
        다음 실행이 `doc_node_spaces` 를 다시 계산해 재시도한다.
        """
        if not doc_node_spaces:
            return
        stale = doc_node_spaces.get(node_id, set()) - {space}
        for other_space in stale:
            try:
                ok_del = docs.delete_node_doc(other_space, node_id)
            except Exception as exc:
                log.warning("doc 이종 space 정리 오류 %s space=%s: %s", node_id, other_space, exc)
                continue
            if not ok_del:
                log.warning("doc 이종 space 정리 실패(반환 False) %s space=%s", node_id, other_space)

    for row in iter_jsonl(nodes_file):  # shard-aware 논리 스트림
            space, node_type, node_id, props = transform_node(pack_name, row)
            id_map[node_id] = (space, node_type)
            bypack_ids.add(node_id)  # add 성공 여부와 무관하게 항상(엣지 endpoint·삭제 대조용)

            live = live_nodes.get(node_id)
            if live is not None:
                # **스토어가 주입하는 키는 비교에서 뺀다.** 한동안 `id` 하나만 뺐는데,
                # upstream 이 `space_id`/`properties[space]` 우선순위를 통합하면서
                # `space` 도 주입하게 됐고(#125), 그 순간 **동일한 행이 전부 chg 로
                # 판정돼 매 증분마다 전량 재적재**된다. 이름을 하나 더 적는 대신
                # "스토어가 넣는 것"이라는 축으로 묶는다.
                live_props = {k: v for k, v in live[2].items() if k not in STORE_INJECTED_KEYS}
                if live[0] == node_type and live_props == props:
                    n_same += 1
                    # F4-c: 노드 자체는 안 바뀌었어도 doc 이 다른 space 를 가리키는
                    # 채로 남아 있을 수 있다(예: 지난 증분이 이 정리 전에 실패했다).
                    # same 경로도 확인한다.
                    _cleanup_stale_doc_spaces(node_id, space)
                    done = n_new + n_chg + n_same + skip + err
                    if done % 500 == 0:
                        print(f"    …노드(증분) {done} (new={n_new} chg={n_chg} same={n_same} skip={skip} err={err})", flush=True)
                    continue

            # 타입이 바뀐 구 행은 **새 노드가 실제로 저장된 뒤에** 지운다(아래).
            #
            # 종전에는 `add_node` **전에** 지웠다. 그러면 저장이 실패했을 때 구 노드와
            # cascade 엣지가 이미 없어 **재시도로도 복구되지 않는다** — 다음 증분은
            # `live is None` 으로 보고 다시 시도하다 같은 이유로 또 실패한다. 영구 소실이다.
            # 노드 키가 `(node_type, node_id)` 라 타입이 다르면 잠시 공존할 수 있으므로
            # 이 순서가 가능하다. 저장이 실패하면 구 행이 남아 **현상 유지**가 된다.
            stale_typed = (live[0], live[1] or space) if (live and live[0] != node_type) else None

            try:
                res = builder.add_node(space, node_type, node_id, properties=props)
                # **영수증을 본다.** `add_node` 는 스토어 실패를 예외로 올리지 않고 반환
                # dict 에 적는다 — `builder.store_write_failures()` 의 docstring 이
                # "호출자가 이것을 불러야 실제 성공 여부를 안다"고 명시한다.
                fails = store_write_failures(res.get("stores", {}) if isinstance(res, dict) else {})
                if fails:
                    err += 1
                    log.warning("노드 저장 실패 %s (%s/%s): %s",
                                node_id, space, node_type, "; ".join(fails))
                else:
                    if stale_typed is not None:
                        # 새 행이 저장된 뒤에만 구 타입 행을 지운다.
                        try:
                            graph.delete_node(stale_typed[0], node_id)
                        except Exception as exc:
                            log.warning("구 타입 노드 삭제 실패 %s(%s): %s",
                                        node_id, stale_typed[0], exc)
                        # space 동일성 가드(2026-08-12, 이관 회귀 수정): `doc_nodes`
                        # PK 는 `(space, node_id)` 로 타입을 안 담는다. space 가 같으면
                        # 위 `add_node` 의 `upsert_node_doc` 이 **같은 행을 이미 신 값으로
                        # 갱신**했으므로 지울 구 행이 없다 — 여기서 지우면 방금 갱신한
                        # 행이 사라진다(2026-08-12 실측: doc 행 0 소실). space 가 다를
                        # 때만 구 space 행이 별도로 남아 삭제 대상이다. 원본(이관 전)은
                        # 삭제가 add_node **앞**이라 이 문제가 없었고, 저장 실패 시 영구
                        # 소실을 막으려 저장-후-삭제로 순서를 바꾼 것(위 주석)이 이 가드를
                        # 필요하게 만들었다.
                        if stale_typed[1] != space:
                            try:
                                docs.delete_node_doc(stale_typed[1], node_id)
                            except Exception as exc:
                                log.warning("구 타입 노드 doc 삭제 실패 %s(%s): %s",
                                            node_id, stale_typed[1], exc)
                    # F4-c: 저장이 확인된 뒤 doc_node_spaces 기준으로 다른 space 의
                    # doc 행을 정리한다. stale_typed 삭제와는 별개다 — 저건 "타입이
                    # 바뀐 구 행"이고 이건 "같은 노드가 다른 space 로도 찍혀 있던 것"이다.
                    _cleanup_stale_doc_spaces(node_id, space)
                    if live is None:
                        n_new += 1
                    else:
                        n_chg += 1
            except ValueError as ve:
                skip += 1
                log.debug("노드 문법위반 skip %s (%s/%s): %s", node_id, space, node_type, ve)
            except Exception as exc:
                err += 1
                log.warning("노드 오류 %s: %s", node_id, exc)

            done = n_new + n_chg + n_same + skip + err
            if done % 500 == 0:
                print(f"    …노드(증분) {done} (new={n_new} chg={n_chg} same={n_same} skip={skip} err={err})", flush=True)

    return n_new, n_chg, n_same, skip, err, bypack_ids


def load_edges(
    pack_name: str,
    edges_file: Path,
    builder: OntologyBuilder,
    id_map: dict[str, tuple[str, str]],
    applied: set | None = None,
    reasons: Counter | None = None,
) -> tuple[int, int, int]:
    """엣지 적재. 반환: (ok, skip, err)

    skip·err 사유를 (종류, 원라벨, from_space, to_space)로 집계해 **호출부와 무관하게** 요약을
    출력한다 — 사유가 log.debug 뿐이라 미적재 엣지가 카운트만 남고 원인이 증발하던 것을 막는다
    (2026-07-30: 라이브 실손실 131,072건이 사유 불명으로 누적돼 있었다).
    집계·출력을 함수 안에 두는 이유: 호출부에 맡기면 형제 호출부(reload_nodes_only·
    load_packs_into_kure)가 빠진다 — 실제로 처음엔 그렇게 만들었다가 지적받았다.
    reasons를 넘기면 집계 Counter를 그대로 돌려받는다(호출부 추가 가공용).
    """
    require_live_data("load_edges")
    ok = skip = err = 0
    if reasons is None:
        reasons = Counter()

    for row in iter_jsonl(edges_file):  # shard-aware 논리 스트림
            raw_label = row.get("label") or row.get("relation") or ""
            src_id    = row.get("source_id") or row.get("from_id") or ""
            tgt_id    = row.get("target_id") or row.get("to_id")   or ""

            # endpoint space 조회 (통합 맵)
            src_info = id_map.get(src_id)
            tgt_info = id_map.get(tgt_id)
            if src_info is None or tgt_info is None:
                skip += 1
                miss = 'src' if src_info is None else ''
                miss += ('+' if miss and tgt_info is None else '') + ('tgt' if tgt_info is None else '')
                reasons[('endpoint 미존재', raw_label, miss, '')] += 1
                log.debug("엣지 endpoint 미존재 skip: %s->%s (%s)", src_id[:8], tgt_id[:8], raw_label)
                continue

            from_space = src_info[0]
            to_space   = tgt_info[0]

            # 라벨/공간 해석은 normalize.resolve_edge 가 정본이다(게이트와 공용).
            # 반전이면 space 는 이미 뒤바뀐 값이 오므로 endpoint 도 함께 바꾼다.
            # 어느 표에도 없으면 lowercase 로 귀착 — 원본 라벨은 아래 source_label 로 보존된다.
            from_space, relation, to_space, _reversed = resolve_edge(
                raw_label, from_space, to_space)
            if _reversed:
                src_id, tgt_id = tgt_id, src_id

            if applied is not None:
                # **성공/실패와 무관하게 넣는다.** 이 시점에서 (src_id, relation, tgt_id)는
                # 반전까지 반영된 최종 형태로 확정됐다 — 이 행이 파일에 있다는 사실 자체가
                # "고아가 아니다"의 근거다. 저장이 실패해도 여기 안 넣으면, 이전 증분에서
                # 이미 라이브에 들어가 있는 동일 엣지가 `incremental_finalize`의
                # `stale_edges = live_edges - applied_edges`에 걸려 **삭제**된다
                # (재현: live_edges={('f','r','t')}, applied_edges=set() →
                # stale_delete_would_run=True, 2026-08-11 적대 검증).
                applied.add((src_id, relation, tgt_id))

            props: dict = {}
            props["source_label"] = raw_label   # 원본 라벨 보존
            if row.get("properties"):
                props.update({k: str(v) if not isinstance(v, (str, int, float, bool)) else v
                              for k, v in row["properties"].items()})
            # pack_id는 row properties 병합 "후" 강제 — 덤프에 내장된 원본 pack_id가
            # 덮어쓰는 버그 방지 (2026-07-05 openclaw: 엣지 216,711건이
            # 'openclaw-conversations-local'로 오염됐던 사례; 원본은 origin_pack_id로 보존)
            if props.get("pack_id") and props["pack_id"] != pack_name:
                props["origin_pack_id"] = props["pack_id"]
            props["pack_id"] = pack_name

            try:
                res = builder.add_edge(from_space, src_id, relation, to_space, tgt_id, properties=props)
                # **영수증을 본다.** `add_edge`도 `add_node`와 같은 계약이다 — 스토어
                # 실패를 예외로 올리지 않고 반환 dict에 적으므로, 호출자가 이것을 불러야
                # 실제 성공 여부를 안다(`builder.store_write_failures()` docstring).
                fails = store_write_failures(res.get("stores", {}) if isinstance(res, dict) else {})
                if fails:
                    err += 1
                    reasons[('저장 실패', f'{raw_label}→{relation}', from_space, to_space)] += 1
                    log.warning("엣지 저장 실패 %s->%s [%s→%s]: %s",
                                src_id[:8], tgt_id[:8], raw_label, relation, "; ".join(fails))
                else:
                    ok += 1
            except ValueError as ve:
                skip += 1
                reasons[('grammar 위반', f'{raw_label}→{relation}', from_space, to_space)] += 1
                log.debug("엣지 문법위반 skip %s->%s [%s→%s]: %s", src_id[:8], tgt_id[:8], raw_label, relation, ve)
            except Exception as exc:
                err += 1
                # err도 사유를 남긴다 — skip만 집계하면 예외 경로가 감시 밖에 남는다
                reasons[('예외', f'{raw_label}: {type(exc).__name__}', from_space, to_space)] += 1
                log.warning("엣지 오류 %s->%s: %s", src_id[:8], tgt_id[:8], exc)

    if skip or err:
        # 사유는 log.debug라 조작자에게 보이지 않는다 — 상위 사유를 노출한다.
        # 상위에 안 든 몫도 건수로 밝혀 "일부만 보여주고 숨김"이 되지 않게 한다.
        # 집계 총합은 reasons가 아니라 이 호출에서 센 skip+err로 잡는다 —
        # 호출부가 내용이 든 Counter를 재사용하면 '그 외 -99건' 같은 음수가 나온다.
        shown = reasons.most_common(8)
        print(f"    [{pack_name}] skip/err 사유({len(reasons)}종): " + ' · '.join(
            f'{kind} {lbl} {a}->{b} x{n}' if b else f'{kind} {lbl}({a}) x{n}'
            for (kind, lbl, a, b), n in shown), flush=True)
        assert sum(reasons.values()) == skip + err, (
            f'{pack_name}: 사유 집계 {sum(reasons.values())} != skip+err {skip + err} — '
            f'미집계 경로가 있거나 호출부가 쓰던 Counter를 넘겼다')
        rest = (skip + err) - sum(n for _, n in shown)
        if rest:
            print(f"    … 그 외 {len(reasons) - len(shown)}종 {rest}건", flush=True)

    return ok, skip, err




def load_chunks(
    pack_name: str,
    chunks_file: Path,
    vec,
    docs,
    batch_size: int = 256,
) -> tuple[int, int]:
    """청크 적재. 반환: (ok, err)"""
    require_live_data("load_chunks")
    if not vec.available:
        print(f"  [{pack_name}] Chroma 미가용 → 청크 skip", flush=True)
        return 0, 0

    ok = err = 0
    seen_ids: set[str]  = set()   # 중복 청크 ID dedup
    b_texts: list[str]  = []
    b_ids:   list[str]  = []
    b_metas: list[dict] = []

    def flush_single(sid: str, txt: str, meta: dict) -> bool:
        """청크 1건 upsert. 성공 시 True — **doc 쓰기까지 성공해야** True다.

        [Δ r11 P2, #142 재리뷰] 종전엔 `docs.upsert_source` 실패를 통째로
        삼켰다(`except Exception: pass`) — 벡터는 써졌는데 doc(BM25 색인·앵커
        판정의 근거)만 결손 나도 `ok` 가 올라 호출자가 실패를 알 방법이 없었다.
        이제 doc 쓰기 실패는 err 로 세고 청크 ID 와 함께 경고를 남긴다 — 그
        청크는 doc_sources 기준선이 안 움직이므로 다음 증분이 재시도한다.
        """
        nonlocal ok, err
        try:
            vec.upsert_texts(texts=[txt], ids=[sid], metadatas=[meta])
        except Exception as exc2:
            err += 1
            log.warning("청크 개별 오류(%s) %s: %s", pack_name, sid[:8], exc2)
            return False
        try:
            docs.upsert_source(sid, txt, meta)
        except Exception as exc3:
            err += 1
            log.warning("청크 doc 쓰기 오류(%s) %s: %s", pack_name, sid[:8], exc3)
            return False
        ok += 1
        return True

    def flush() -> None:
        nonlocal ok, err
        if not b_texts:
            return
        try:
            vec.upsert_texts(texts=b_texts, ids=b_ids, metadatas=b_metas)
        except Exception as exc:
            # 배치 실패 시 건별 재시도 (1건 결함이 배치 전체를 날리지 않게)
            log.warning("청크 배치 오류(%s), 건별 재시도: %s", pack_name, exc)
            for sid, txt, meta in zip(b_ids, b_texts, b_metas):
                flush_single(sid, txt, meta)
        else:
            # [Δ r11 P2] doc 쓰기 실패를 더 이상 삼키지 않는다 — flush_single 과
            # 같은 계약(벡터 성공 + doc 성공이어야 ok).
            doc_failed = 0
            for sid, txt, meta in zip(b_ids, b_texts, b_metas):
                try:
                    docs.upsert_source(sid, txt, meta)
                except Exception as exc3:
                    doc_failed += 1
                    err += 1
                    log.warning("청크 doc 쓰기 오류(%s) %s: %s", pack_name, sid[:8], exc3)
            ok += len(b_texts) - doc_failed
        b_texts.clear()
        b_ids.clear()
        b_metas.clear()

    for row in iter_jsonl(chunks_file):  # shard-aware 논리 스트림
            chunk_id = row["id"]
            # 중복 ID skip (첫 등장만 유지)
            if chunk_id in seen_ids:
                log.debug("청크 중복 ID skip: %s", chunk_id)
                continue
            seen_ids.add(chunk_id)
            meta: dict = transform_chunk_meta(pack_name, row)
            b_texts.append(row["text"])
            b_ids.append(chunk_id)
            b_metas.append(meta)
            if len(b_texts) >= batch_size:
                flush()

    flush()
    return ok, err


def load_chunks_incremental(
    pack_name: str,
    chunks_file: Path,
    vec,
    docs,
    live_chunks: dict[str, tuple[str, dict]],
    batch_size: int = 256,
) -> tuple[int, int, int, int, int, set]:
    """청크 증분 적재. 텍스트 불변·메타만 변경된 행은 임베딩 없이 upsert_source만 호출.

    반환: (c_new, c_txt, c_meta, c_same, err, bypack_ids)
    """
    require_live_data("load_chunks_incremental")
    c_new = c_txt = c_meta = c_same = err = 0
    seen_ids: set[str]  = set()   # 중복 청크 ID dedup
    bypack_ids: set[str] = set()
    b_texts: list[str]  = []
    b_ids:   list[str]  = []
    b_metas: list[dict] = []
    b_kinds: list[str]  = []      # "new" | "txt" — flush 후 c_new/c_txt 반영용

    def flush_single(sid: str, txt: str, meta: dict, kind: str) -> None:
        """청크 1건 upsert(재임베딩). doc 쓰기까지 성공해야 kind 에 따라
        c_new/c_txt 반영한다(불변식 ①, [Δ r11 P2] — 형제 `flush()` 와 동일)."""
        nonlocal c_new, c_txt, err
        try:
            vec.upsert_texts(texts=[txt], ids=[sid], metadatas=[meta])
        except Exception as exc2:
            err += 1
            log.warning("청크 개별 오류(%s) %s: %s", pack_name, sid[:8], exc2)
            return
        try:
            docs.upsert_source(sid, txt, meta)
        except Exception as exc3:
            err += 1
            log.warning("청크 doc 쓰기 오류(%s) %s: %s", pack_name, sid[:8], exc3)
            return
        if kind == "new":
            c_new += 1
        else:
            c_txt += 1

    def flush() -> None:
        nonlocal c_new, c_txt, err
        if not b_texts:
            return
        try:
            vec.upsert_texts(texts=b_texts, ids=b_ids, metadatas=b_metas)
        except Exception as exc:
            # 배치 실패 시 건별 재시도 (1건 결함이 배치 전체를 날리지 않게)
            log.warning("청크 배치 오류(%s), 건별 재시도: %s", pack_name, exc)
            for sid, txt, meta, kind in zip(b_ids, b_texts, b_metas, b_kinds):
                flush_single(sid, txt, meta, kind)
        else:
            # [Δ r11 P2] doc 쓰기 실패를 더 이상 삼키지 않는다.
            for sid, txt, meta, kind in zip(b_ids, b_texts, b_metas, b_kinds):
                try:
                    docs.upsert_source(sid, txt, meta)
                except Exception as exc3:
                    err += 1
                    log.warning("청크 doc 쓰기 오류(%s) %s: %s", pack_name, sid[:8], exc3)
                    continue
                if kind == "new":
                    c_new += 1
                else:
                    c_txt += 1
        b_texts.clear()
        b_ids.clear()
        b_metas.clear()
        b_kinds.clear()

    for row in iter_jsonl(chunks_file):  # shard-aware 논리 스트림
            chunk_id = row["id"]
            # 중복 ID skip (첫 등장만 유지)
            if chunk_id in seen_ids:
                log.debug("청크 중복 ID skip: %s", chunk_id)
                continue
            seen_ids.add(chunk_id)
            bypack_ids.add(chunk_id)  # 중복 제외 후

            meta = transform_chunk_meta(pack_name, row)
            live = live_chunks.get(chunk_id)

            if live is None:
                b_ids.append(chunk_id)
                b_texts.append(row["text"])
                b_metas.append(meta)
                b_kinds.append("new")
            elif live[0] != row["text"]:
                b_ids.append(chunk_id)
                b_texts.append(row["text"])
                b_metas.append(meta)
                b_kinds.append("txt")
            elif live[1] != meta:
                # 텍스트 불변, 메타만 변경 — 임베딩은 재계산하지 않는다(텍스트가 같으므로).
                #
                # **벡터를 먼저 고치고, 성공했을 때만 doc 기준을 옮긴다.** 형제(flush·
                # flush_single)는 이미 벡터 먼저·doc 나중 순서다. 종전엔 이 메타 전용
                # 분기만 `doc_sources`를 **먼저** 커밋하고 그 뒤에 벡터를 고쳤는데,
                # `doc_sources`는 **다음 증분의 비교 기준**이라 벡터 수리가 실패해도
                # 기준이 갱신돼 다음 실행이 "동일"로 넘어간다 — **영구 불일치**가 된다
                # (2026-08-11 리뷰 지적). 벡터를 못 고치는 백엔드면 조용히 넘기지 않고
                # 텍스트 경로로 보내 재임베딩시킨다 — 이때 doc은 옛 메타 그대로 두어
                # 재시도 가능성을 보존한다(비교 기준을 섣불리 옮기지 않는다).
                try:
                    if _vec_meta_update(vec, chunk_id, meta):
                        docs.upsert_source(chunk_id, row["text"], meta)
                        c_meta += 1
                    else:
                        # 벡터 메타를 못 고쳤다 — 재임베딩으로라도 맞춘다.
                        b_ids.append(chunk_id)
                        b_texts.append(row["text"])
                        b_metas.append(meta)
                        b_kinds.append("txt")
                        continue
                except Exception as exc:
                    err += 1
                    log.warning("청크 메타갱신 오류(%s) %s: %s", pack_name, chunk_id[:8], exc)
            else:
                c_same += 1

            if len(b_texts) >= batch_size:
                flush()

    flush()
    return c_new, c_txt, c_meta, c_same, err, bypack_ids


def incremental_finalize(
    pack_name: str,
    graph,
    docs,
    vec,
    live: dict,
    bypack_node_ids: set[str],
    bypack_chunk_ids: set[str],
    applied_edges: set[tuple[str, str, str]],
    force_delete: bool,
    nodes_total: int,
    chunks_total: int,
) -> dict:
    """증분 삭제 + 3원 대사. live는 live_pack_state()의 반환 dict.

    반환: {"node_del":…, "chunk_del":…, "edge_del":…, "vec_orphan_del":…,
           "doc_orphan_del":…}
    """
    require_live_data("incremental_finalize")
    _require_sql_hooks(graph, _GRAPH_SQL_HOOKS, "graph 스토어")
    _require_sql_hooks(docs, _DOC_SQL_HOOKS, "doc 스토어")
    live_nodes  = live["nodes"]
    live_chunks = live["chunks"]
    live_edges  = live["edges"]
    live_vecids = live["vec_ids"]
    doc_node_spaces = live["doc_node_spaces"]  # F4-b: {node_id: {space, ...}}

    def _is_anchor(node_id: str) -> bool:
        return _is_anchor_node(node_id, live_nodes.get(node_id, (None, None, {}))[2])

    node_del = chunk_del = edge_del = vec_orphan_del = doc_orphan_del = 0

    # ── 안전핀 0: by-pack 파일 누락 의심 (0-항목인데 라이브엔 데이터 존재) ──
    if not bypack_node_ids and live_nodes:
        sys.exit(
            f"ERROR: [{pack_name}] by-pack 노드 0건 · 라이브 {len(live_nodes)}건 —"
            " by-pack 파일 누락 의심 — 삭제 폭주 방지 중단"
        )
    if not bypack_chunk_ids and live_chunks:
        sys.exit(
            f"ERROR: [{pack_name}] by-pack 청크 0건 · 라이브 {len(live_chunks)}건 —"
            " by-pack 파일 누락 의심 — 삭제 폭주 방지 중단"
        )

    # ── 삭제 후보 (앵커 제외) ──────────────────────────────────────────
    node_del_candidates = {nid for nid in (set(live_nodes) - bypack_node_ids) if not _is_anchor(nid)}
    chunk_del_candidates = set(live_chunks) - bypack_chunk_ids
    # doc 축 후보(F4-d). 노드 축 후보와 축이 다르다 — node_del_candidates 는
    # "노드 자체가 사라졌는가"고, 이건 "이 노드가 남아있어도 이 space 의 doc
    # 행은 재확인됐는가"다. doc_node_spaces 는 이미 앵커를 뺀 채로 온다(F4-b).
    doc_del_candidates = set(doc_node_spaces) - bypack_node_ids

    # ── 안전핀: doc_node_spaces 가 비었는데 후보가 있으면 불변식 위반 ──────
    # doc_del_candidates 는 집합 뺄셈이라 정의상 doc_node_spaces 의 부분집합이다.
    # 그런데도 위반이 보이면 doc_node_spaces 계산 자체가 깨진 것이고, 그 상태로는
    # 30% 핀도 못 막는다(분모가 0이라 `max(1, ...)` 로 얼버무리면 100% 넘는 삭제가
    # 조용히 통과한다). 즉시 중단한다.
    if not doc_node_spaces and doc_del_candidates:
        sys.exit(
            f"ERROR: [{pack_name}] doc_node_spaces 0건인데 doc 삭제 후보"
            f" {len(doc_del_candidates)}건 — 불변식 위반(후보는 doc_node_spaces 의"
            " 부분집합이어야 한다), 중단"
        )

    # ── 안전핀 30%: 삭제 폭주 방지 ───────────────────────────────────────
    node_ratio  = len(node_del_candidates)  / max(1, len(live_nodes))
    chunk_ratio = len(chunk_del_candidates) / max(1, len(live_chunks))
    if (node_ratio > 0.30 or chunk_ratio > 0.30) and not force_delete:
        sys.exit(
            f"ERROR: [{pack_name}] 삭제 후보 비율 초과 — "
            f"노드 {len(node_del_candidates)}/{len(live_nodes)}({node_ratio:.1%}) "
            f"청크 {len(chunk_del_candidates)}/{len(live_chunks)}({chunk_ratio:.1%}) — "
            "--force-delete 로 강행하십시오."
        )
    # doc 축 30% 핀은 노드/청크와 별도다 — 분모가 doc_node_spaces 의 **노드 수**다
    # (행 수가 아니다). 한 노드가 여러 space 에 걸쳐 있으면 행 수가 노드 수보다
    # 많아지므로, 행 수를 분모로 쓰면 같은 삭제량도 비율이 낮게 나와 핀이 무뎌진다.
    # doc_node_spaces 가 비면 후보도 비므로(위 불변식 핀에서 이미 확인됐다) 건너뛴다.
    if doc_node_spaces:
        doc_node_ratio = len(doc_del_candidates) / len(doc_node_spaces)
        if doc_node_ratio > 0.30 and not force_delete:
            sys.exit(
                f"ERROR: [{pack_name}] doc 삭제 후보 비율 초과 — "
                f"{len(doc_del_candidates)}/{len(doc_node_spaces)}({doc_node_ratio:.1%}) — "
                "--force-delete 로 강행하십시오."
            )

    # ── 노드 삭제 ────────────────────────────────────────────────────
    # doc 삭제는 여기서 하지 않는다 — 아래 doc 축 정리(doc_del_candidates)가
    # 이 node_id 의 모든 space 를 한 번에 지운다. 예전엔 여기서 live_nodes 의
    # space_id 하나만 지워서 다른 space 의 행을 놓쳤다(F4-d, 2026-08-11 설계 지적).
    for node_id in node_del_candidates:
        node_type, _space_id, _props = live_nodes[node_id]
        try:
            deleted = graph.delete_node(node_type, node_id)
        except Exception as exc:
            log.warning("노드 삭제 오류(%s) %s type=%s: %s", pack_name, node_id, node_type, exc)
            continue
        if deleted:
            node_del += 1

    # ── doc 축 정리: 후보 노드의 모든 space 행을 지운다 ─────────────────────
    # load_nodes_incremental 의 F4-c 가 매 증분 실시간으로 대부분 정리하지만,
    # 그 경로를 안 거치는 자리(위 노드 삭제 경로 등)의 doc 삭제도 여기서 합류한다.
    for node_id in doc_del_candidates:
        for space in doc_node_spaces[node_id]:
            try:
                ok_del = docs.delete_node_doc(space, node_id)
            except Exception as exc:
                log.warning("doc 고아 삭제 오류(%s) %s space=%s: %s", pack_name, node_id, space, exc)
                continue
            if ok_del:
                doc_orphan_del += 1

    # ── 청크 삭제 (delete_pack과 동일 배치 패턴) ────────────────────────
    chunk_del_list = list(chunk_del_candidates)
    doc_sources_table = docs._table("doc_sources")
    for batch in _batched(chunk_del_list):
        placeholders, in_params = _in_names("cid", batch)
        if not placeholders:                          # 도달 불가 — 방어(위 _in_names 주석)
            continue
        try:
            chunk_del += docs._exec_write(
                f"DELETE FROM {doc_sources_table} WHERE source_id IN ({placeholders})",
                in_params,
            )
        except Exception as exc:
            log.warning("청크 삭제 오류(%s): %s", pack_name, exc)
        # [Δ r11 P1] delete_pack 과 동일 — sqlite 전용 방언 게이트만, 순서·삼킴은
        # 현행 유지(고아 FTS 는 keyword_search INNER JOIN 기준 무해, 재적재가
        # 자가 치유 — 위 delete_pack 의 상세 주석 참고).
        if docs._dialect.name == "sqlite":
            try:
                docs._exec_write(
                    f"DELETE FROM doc_sources_fts WHERE source_id IN ({placeholders})",
                    in_params,
                )
            except Exception as exc:
                log.warning("doc_sources_fts 삭제 오류(%s): %s", pack_name, exc)
    if chunk_del_list:
        # evidence 노드 id == 청크 id 공유 팩: 청크만 사라지고 노드가 남는 id의
        # 벡터는 보존(노드 벡터 겸용 — 오삭제 방지, 2026-07-22)
        vec_del_ids = [i for i in chunk_del_list if i not in bypack_node_ids]
        try:
            if vec_del_ids:
                vec.delete(vec_del_ids)
        except Exception as exc:
            log.warning("청크 벡터 삭제 오류(%s): %s", pack_name, exc)

    # ── 엣지 정리 (live에 있으나 이번 적재에서 재확인되지 않은 엣지) ──────
    # 안전핀: applied가 통째로 비었는데 live 엣지가 있으면 edges.jsonl 누락/전건
    # 문법 실패 의심 — 전량 삭제 대신 스킵(0-항목 핀과 동일 클래스, 2026-07-22)
    if not applied_edges and live_edges:
        print(
            f"  ⚠️ [{pack_name}] 반영 엣지 0건·라이브 {len(live_edges)}건 — "
            "edges.jsonl 누락 의심, 엣지 정리 스킵",
            flush=True,
        )
        stale_edges = set()
    else:
        stale_edges = live_edges - applied_edges
    # per-edge `_exec_write` 유지(집합화 안 함) — 스테일 엣지는 평시 소량이고,
    # 아래 rowcount 이중 계상 방지 계약이 행 단위 카운트에 걸려 있다. 대량
    # 스테일 시 row-value IN 배치가 업그레이드 경로다(한계, 별건 — v6 검수).
    edges_table = graph._table("graph_edges")
    edge_pack_pred = _json_str_eq(graph._dialect, "properties", "pack_id", "pack")
    for (f_id, r, t_id) in stale_edges:
        try:
            # rowcount로 센다 — 무조건 +=1이면 노드 삭제의 cascade가 이미 지운
            # 엣지까지 여기서 다시 세어 **이중 계상**한다.
            edge_del += graph._exec_write(
                f"DELETE FROM {edges_table} WHERE from_id=:f AND relation=:r AND to_id=:t"
                f" AND {edge_pack_pred}",
                {"f": f_id, "r": r, "t": t_id, "pack": pack_name},
            )
        except Exception as exc:
            log.warning("엣지 삭제 오류(%s) %s-%s->%s: %s", pack_name, f_id[:8], r, t_id[:8], exc)

    # ── 벡터 고아 정리 (live vec_ids 중 이번 적재의 노드·청크 어느 쪽도 아닌 것) ──
    # 앵커는 노드 삭제와 동일하게 보호 — 현재 앵커 벡터 0건이나 향후 생겨도 오삭제 방지
    vec_orphans = [
        i for i in live_vecids - (bypack_node_ids | bypack_chunk_ids) if not _is_anchor(i)
    ]
    for batch in _batched(vec_orphans):
        try:
            # vec.delete는 요청 개수만 안다(실제 삭제 건수를 돌려주지 않는다) —
            # 아래 카운트는 "요청 수"이지 확인된 삭제 수가 아니다.
            vec.delete(batch)
            vec_orphan_del += len(batch)
        except Exception as exc:
            log.warning("벡터 고아 삭제 오류(%s): %s", pack_name, exc)

    # ── 3원 대사 출력 (assert 아님 — 숫자 보고) ────────────────────────────
    counts = pack_live_counts(pack_name, graph, docs, vec)
    graph_count, doc_count, vec_count = counts["nodes"], counts["docs"], counts["vectors"]
    vec_expected = len(bypack_node_ids | bypack_chunk_ids)

    # vec_count 는 `int | None` 이다(pack_live_counts 계약). `None` 은 "백엔드가
    # 카운트를 못 낸다"는 뜻이라 vec_expected 와 뺄셈하면 TypeError 다 — 산술하지
    # 않고 "미확인"이라고만 말한다.
    vec_line = (f"vec {vec_count} (기대 {vec_expected} {vec_count - vec_expected:+d})"
                if vec_count is not None else f"vec ? (기대 {vec_expected}, 미확인)")
    print(
        f"  [{pack_name}] 3원 대사: graph {graph_count} (by-pack {nodes_total} {graph_count - nodes_total:+d}) / "
        f"doc {doc_count} (by-pack {chunks_total} {doc_count - chunks_total:+d}) / "
        f"{vec_line}",
        flush=True,
    )
    print(
        f"  [{pack_name}] 증분 삭제: 노드 {node_del} / 청크 {chunk_del} / 엣지 {edge_del} / "
        f"벡터고아 {vec_orphan_del} / doc고아 {doc_orphan_del}",
        flush=True,
    )

    return {
        "node_del": node_del,
        "chunk_del": chunk_del,
        "edge_del": edge_del,
        "vec_orphan_del": vec_orphan_del,
        "doc_orphan_del": doc_orphan_del,
    }
