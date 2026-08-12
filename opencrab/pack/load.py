"""팩 적재 계층 — `{nodes,edges,chunks}.jsonl` 을 4스토어(graph/doc/sql/vector)에 반영.

생산자(`opencrab.pack.build`)와 소비자(여기)가 서로 다른 리포에 있던 동안 아무도 둘을
대조하지 않았고, 그래서 노드 커스텀 필드 91만 건이 파일에는 있는데 라이브에는 없는 상태로
방치됐다 — 생산자는 props 를 노드 최상위에 펼쳤고 소비자는 중첩 `properties` 만 읽었다.
어느 게이트도 잡지 못했다. 계약(`schema`)·생산자(`build`)·정규화(`normalize`)·소비자(여기)를
한 패키지에 모아 빌드에서 적재까지 한 스위트로 왕복 검증할 수 있게 하는 것이 이 이관이다.

**쓰기 함수는 각자 `require_live_data()` 를 부른다.** 진입점에서 한 번 부르는 방식은
진입점을 안 거치고 이 함수들을 직접 호출하는 경로에서 통째로 빠진다(실측: 그런 호출
스크립트가 3종 있었다). 계약은 `tests/test_pack_load.py` 가 AST 로 건다.

**스토어 private 속성 직접 접근이 21곳 있다**(`docs._conn` 12, `graph._conn` 6,
`vec._collection` 2). 이관 전에는 패키지 밖에서의 접근이라 명백한 계약 위반이었고, 지금은
패키지 안이라 형식적으로는 정당해졌다. 그러나 그것이 가리키는 사실은 그대로다 —
스토어 protocol(`opencrab/stores/_graph_protocol.py`)에 삭제·카운트 API 가 없다.
protocol 승격은 4백엔드 전부 구현을 요구하므로 별건으로 다룬다.

**`sys.exit` 가 `incremental_finalize` 안에 3곳 있다**(증분 삭제 안전핀). 라이브러리
코드로서는 부적절하지만 이 커밋은 순수 이동이라 행동을 바꾸지 않는다. 예외로의 승격도 별건.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

from opencrab.ontology.builder import OntologyBuilder, store_write_failures
from opencrab.pack.jsonl_io import iter_jsonl
from opencrab.pack.live_data import require_live_data
from opencrab.pack.normalize import (
    resolve_edge,
    transform_chunk_meta,
    transform_node,
)

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


# 앵커 노드 판정(F4-a). `dataset:` 프리픽스 노드나 title-backfill 이 만든 노드는
# graph 트윈이 없거나 있어도 삭제 후보에서 빼야 한다 — 이 판정을 두 곳(Python 술어,
# SQL WHERE 조각)에서 각자 구현하면 갈린다. 실제로 종전엔 `incremental_finalize`
# 지역 함수 하나뿐이었는데, F4-b 가 `live_pack_state`(모듈 함수, `incremental_finalize`
# 밖)에서도 같은 판정이 필요해지면서 두 벌이 될 뻔했다.
#
# SQL 쪽에 `LIKE` 를 쓰면 안 된다 — SQLite `LIKE` 는 ASCII 대소문자를 무시해서
# `DATASET:x` 도 앵커로 잡는데 Python `str.startswith` 는 그 행을 안 잡는다.
# 그러면 graph 축과 doc 축이 서로 다른 노드를 앵커로 보고 판정이 갈린다.
# `GLOB` 은 대소문자를 구분해 Python 쪽과 일치한다.
ANCHOR_SQL = (
    "(node_id GLOB 'dataset:*'"
    " OR COALESCE(json_extract(properties,'$.created_by'),'') = 'title-backfill')"
)


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


# 4축 대사 쿼리 **문자열 정본**. `pack_live_counts()` 가 이것을 쓰고, 스토어 객체 없이
# raw sqlite3 로 세는 호출자도 **같은 문자열**을 가져다 쓴다.
#
# 왜 함수만으로는 부족한가: 어떤 호출자는 `sqlite3.connect(..., mode=ro)` 로 파일을 직접
# 열어 센다(스토어 객체를 안 만든다). 그런 자리는 함수를 못 부르므로 쿼리를 손으로
# 베끼게 되고, 실제로 그렇게 **5벌**이 됐다. 그중 하나는 이미 정본과 갈려 있었다 —
# `doc_sources` 를 `pack_id` 로만 세고 `OR ... $.source` 를 빠뜨려, 그 형태로 태그된
# 행을 통째로 못 셌다(실측: 5건 중 3건 누락, 2026-08-11 적대 검증).
COUNT_SQL: dict[str, str] = {
    "nodes": "SELECT COUNT(*) FROM graph_nodes WHERE json_extract(properties,'$.pack_id')=?",
    "edges": "SELECT COUNT(*) FROM graph_edges WHERE json_extract(properties,'$.pack_id')=?",
    # doc_sources 는 **두 형태**로 태그돼 있다. 한쪽만 세면 조용히 적게 나온다.
    "docs": ("SELECT COUNT(*) FROM doc_sources WHERE json_extract(metadata,'$.pack_id')=?"
             " OR json_extract(metadata,'$.source')=?"),
}
# 파라미터 개수가 쿼리마다 다르다 — docs 만 pack_name 을 두 번 받는다.
COUNT_SQL_ARGC: dict[str, int] = {"nodes": 1, "edges": 1, "docs": 2}


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
            handle.update(ids=[chunk_id], metadatas=[meta])
            return True
    except Exception as exc:                                  # noqa: BLE001
        log.warning("벡터 메타 갱신 실패(%s): %s — 재임베딩으로 우회한다", chunk_id, exc)
    return False


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
    """
    g = graph._conn.execute(COUNT_SQL["nodes"], (pack_name,)).fetchone()[0]
    e = graph._conn.execute(COUNT_SQL["edges"], (pack_name,)).fetchone()[0]
    d = docs._conn.execute(COUNT_SQL["docs"], (pack_name,) * COUNT_SQL_ARGC["docs"]).fetchone()[0]

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
    rows = graph._conn.execute(
        """
        SELECT node_type, node_id,
               COALESCE(json_extract(properties, '$.space'), 'concept') as space
        FROM graph_nodes
        WHERE json_extract(properties, '$.pack_id') = ?
        """,
        (pack_name,),
    ).fetchall()

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
    dn_rows = docs._conn.execute(
        """
        SELECT space, node_id FROM doc_nodes
        WHERE json_extract(properties, '$.pack_id') = ?
        """,
        (pack_name,),
    ).fetchall()
    doc_node_extra_del = 0
    for space, node_id in dn_rows:
        cur = docs._conn.execute(
            "DELETE FROM doc_nodes WHERE space=? AND node_id=?", (space, node_id)
        )
        doc_node_extra_del += cur.rowcount
    docs._conn.commit()
    node_del += doc_node_extra_del

    # ── doc_sources (청크): metadata.source == pack_name 또는 metadata.pack_id == pack_name
    # (실제 레코드는 source가 아니라 pack_id 필드에만 팩 식별자를 갖는 경우가 있음)
    src_rows = docs._conn.execute(
        "SELECT source_id FROM doc_sources WHERE json_extract(metadata, '$.source') = ?"
        " OR json_extract(metadata, '$.pack_id') = ?",
        (pack_name, pack_name),
    ).fetchall()
    src_ids = [r[0] for r in src_rows]

    chunk_sql_del = 0
    fts_del = 0
    for batch in _batched(src_ids):
        placeholders = ",".join("?" * len(batch))
        cur = docs._conn.execute(
            f"DELETE FROM doc_sources WHERE source_id IN ({placeholders})", batch
        )
        chunk_sql_del += cur.rowcount
        # doc_sources_fts 동기화(별도 fts5 가상 테이블 — 트리거 없이 수동 관리됨)
        try:
            cur2 = docs._conn.execute(
                f"DELETE FROM doc_sources_fts WHERE source_id IN ({placeholders})", batch
            )
            fts_del += cur2.rowcount
        except Exception as exc:
            log.warning("doc_sources_fts 삭제 오류(%s): %s", pack_name, exc)
    docs._conn.commit()

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
    """증분 대조용 라이브 상태 로드 (delete_pack과 동일한 접근 관례: graph._conn/docs._conn).

    반환 dict:
      nodes: {node_id: (node_type, space_id, props_dict)}
      chunks: {source_id: (text, metadata_dict)}
      edges: {(from_id, relation, to_id), ...}
      vec_ids: {node_id, ...}
      doc_node_spaces: {node_id: {space, ...}} — doc_nodes 축 대사용(F4). 이미 이
        함수가 `docs._conn` 을 쥐고 있으므로(위 doc_sources 조회) 같은 커넥션으로
        모은다. 앵커는 뺀다 — 앵커는 삭제 후보가 아니므로 대사 대상도 아니다.
    """
    nodes: dict[str, tuple[str, str, dict]] = {}
    for node_type, node_id, space_id, properties in graph._conn.execute(
        """
        SELECT node_type, node_id, space_id, properties
        FROM graph_nodes
        WHERE json_extract(properties, '$.pack_id') = ?
        """,
        (pack_name,),
    ).fetchall():
        nodes[node_id] = (node_type, space_id, json.loads(properties))

    chunks: dict[str, tuple[str, dict]] = {}
    for source_id, text, metadata in docs._conn.execute(
        """
        SELECT source_id, text, metadata
        FROM doc_sources
        WHERE json_extract(metadata, '$.pack_id') = ?
           OR json_extract(metadata, '$.source') = ?
        """,
        (pack_name, pack_name),
    ).fetchall():
        chunks[source_id] = (text, json.loads(metadata))

    edges: set[tuple[str, str, str]] = set()
    for from_id, relation, to_id in graph._conn.execute(
        """
        SELECT from_id, relation, to_id
        FROM graph_edges
        WHERE json_extract(properties, '$.pack_id') = ?
        """,
        (pack_name,),
    ).fetchall():
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
    doc_node_spaces: dict[str, set[str]] = {}
    for node_id, space in docs._conn.execute(
        f"""
        SELECT node_id, space
        FROM doc_nodes
        WHERE json_extract(properties, '$.pack_id') = ?
          AND NOT {ANCHOR_SQL}
        """,
        (pack_name,),
    ).fetchall():
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
        """청크 1건 upsert. 성공 시 True."""
        nonlocal ok, err
        try:
            vec.upsert_texts(texts=[txt], ids=[sid], metadatas=[meta])
            try:
                docs.upsert_source(sid, txt, meta)
            except Exception:
                pass
            ok += 1
            return True
        except Exception as exc2:
            err += 1
            log.warning("청크 개별 오류(%s) %s: %s", pack_name, sid[:8], exc2)
            return False

    def flush() -> None:
        nonlocal ok, err
        if not b_texts:
            return
        try:
            vec.upsert_texts(texts=b_texts, ids=b_ids, metadatas=b_metas)
            for sid, txt, meta in zip(b_ids, b_texts, b_metas):
                try:
                    docs.upsert_source(sid, txt, meta)
                except Exception:
                    pass
            ok += len(b_texts)
        except Exception as exc:
            # 배치 실패 시 건별 재시도 (1건 결함이 배치 전체를 날리지 않게)
            log.warning("청크 배치 오류(%s), 건별 재시도: %s", pack_name, exc)
            for sid, txt, meta in zip(b_ids, b_texts, b_metas):
                flush_single(sid, txt, meta)
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
        """청크 1건 upsert(재임베딩). 성공 시 kind에 따라 c_new/c_txt 반영."""
        nonlocal c_new, c_txt, err
        try:
            vec.upsert_texts(texts=[txt], ids=[sid], metadatas=[meta])
            try:
                docs.upsert_source(sid, txt, meta)
            except Exception:
                pass
            if kind == "new":
                c_new += 1
            else:
                c_txt += 1
        except Exception as exc2:
            err += 1
            log.warning("청크 개별 오류(%s) %s: %s", pack_name, sid[:8], exc2)

    def flush() -> None:
        nonlocal c_new, c_txt
        if not b_texts:
            return
        try:
            vec.upsert_texts(texts=b_texts, ids=b_ids, metadatas=b_metas)
            for sid, txt, meta in zip(b_ids, b_texts, b_metas):
                try:
                    docs.upsert_source(sid, txt, meta)
                except Exception:
                    pass
            for kind in b_kinds:
                if kind == "new":
                    c_new += 1
                else:
                    c_txt += 1
        except Exception as exc:
            # 배치 실패 시 건별 재시도 (1건 결함이 배치 전체를 날리지 않게)
            log.warning("청크 배치 오류(%s), 건별 재시도: %s", pack_name, exc)
            for sid, txt, meta, kind in zip(b_ids, b_texts, b_metas, b_kinds):
                flush_single(sid, txt, meta, kind)
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
    for batch in _batched(chunk_del_list):
        placeholders = ",".join("?" * len(batch))
        try:
            cur = docs._conn.execute(
                f"DELETE FROM doc_sources WHERE source_id IN ({placeholders})", batch
            )
            chunk_del += cur.rowcount
        except Exception as exc:
            log.warning("청크 삭제 오류(%s): %s", pack_name, exc)
        try:
            docs._conn.execute(
                f"DELETE FROM doc_sources_fts WHERE source_id IN ({placeholders})", batch
            )
        except Exception as exc:
            log.warning("doc_sources_fts 삭제 오류(%s): %s", pack_name, exc)
    docs._conn.commit()
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
    for (f_id, r, t_id) in stale_edges:
        try:
            cur = graph._conn.execute(
                "DELETE FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?"
                " AND json_extract(properties,'$.pack_id')=?",
                (f_id, r, t_id, pack_name),
            )
            # rowcount로 센다 — 무조건 +=1이면 노드 삭제의 cascade가 이미 지운
            # 엣지까지 여기서 다시 세어 **이중 계상**한다.
            edge_del += cur.rowcount
        except Exception as exc:
            log.warning("엣지 삭제 오류(%s) %s-%s->%s: %s", pack_name, f_id[:8], r, t_id[:8], exc)
    graph._conn.commit()

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
