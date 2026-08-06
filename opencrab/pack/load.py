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

from opencrab.ontology.builder import OntologyBuilder
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


def _batched(seq: list, size: int = 500):
    """SQLite 파라미터 상한(기본 999) 회피용 배치 분할."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def delete_pack(pack_name: str, graph, docs, vec) -> tuple[int, int, int]:
    """기존 팩 노드·엣지(cascade)·청크를 삭제. 반환: (node_del, chunk_sql_del, chunk_vec_del)"""
    require_live_data("delete_pack")
    node_del = 0

    # ── graph_nodes: pack_id == pack_name 인 노드 조회 ──────────────────
    rows = graph._conn.execute(
        """
        SELECT node_type, node_id,
               COALESCE(json_extract(properties, '$.space'), 'concept') as space
        FROM graph_nodes
        WHERE json_extract(properties, '$.pack_id') = ?
           OR json_extract(properties, '$.source') LIKE ?
        """,
        (pack_name, f"%{pack_name}%"),
    ).fetchall()

    for node_type, node_id, space in rows:
        # delete_node: 노드 + 관련 엣지 cascade
        graph.delete_node(node_type, node_id)
        # doc_nodes 삭제
        try:
            docs.delete_node_doc(space, node_id)
        except Exception:
            pass
        node_del += 1

    # ── doc_nodes: graph 트윈 없이 남은 pack_id 앵커 노드 직접 정리 ───────
    # (예: backfill이 생성한 dataset: 앵커 — graph_nodes cascade에서 누락됨)
    dn_rows = docs._conn.execute(
        "SELECT space, node_id FROM doc_nodes WHERE json_extract(properties, '$.pack_id') = ?",
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
            # SqliteVecStore(KURE): vectors_kure.pack_id 컬럼으로 직접 삭제
            conn = getattr(vec, "_conn", None) or getattr(vec, "conn", None)
            table = getattr(vec, "_table", None) or getattr(vec, "table_name", "vectors_kure")
            if conn is not None:
                cur = conn.execute(f"DELETE FROM {table} WHERE pack_id = ?", (pack_name,))
                conn.commit()
                chunk_vec_del = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            elif hasattr(vec, "_collection"):
                # Chroma 폴백: metadata.source 기준
                result = vec._collection.get(where={"source": pack_name})
                ids_to_del = result.get("ids", [])
                if ids_to_del:
                    vec._collection.delete(ids=ids_to_del)
                    chunk_vec_del = len(ids_to_del)
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
    if vec.available:
        conn = getattr(vec, "_conn", None)
        table = getattr(vec, "_table", None) or "vectors_kure"
        if conn is not None:
            for (node_id,) in conn.execute(
                f"SELECT node_id FROM {table} WHERE pack_id = ?", (pack_name,)
            ).fetchall():
                vec_ids.add(node_id)

    return {"nodes": nodes, "chunks": chunks, "edges": edges, "vec_ids": vec_ids}


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
                builder.add_node(space, node_type, node_id, properties=props)
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
) -> tuple[int, int, int, int, int, set]:
    """노드 증분 적재. 라이브와 동일한 행은 완전 스킵(어떤 스토어도 미접촉).

    graph/docs는 명시 파라미터 — OntologyBuilder는 스토어를 내부명(_neo4j/_mongo)으로
    보관하므로 builder 속성 접근은 불가(vendor 실측, 2026-07-22).

    반환: (n_new, n_chg, n_same, skip, err, bypack_ids)
    """
    require_live_data("load_nodes_incremental")
    n_new = n_chg = n_same = skip = err = 0
    bypack_ids: set[str] = set()

    for row in iter_jsonl(nodes_file):  # shard-aware 논리 스트림
            space, node_type, node_id, props = transform_node(pack_name, row)
            id_map[node_id] = (space, node_type)
            bypack_ids.add(node_id)  # add 성공 여부와 무관하게 항상(엣지 endpoint·삭제 대조용)

            live = live_nodes.get(node_id)
            if live is not None:
                live_props = {k: v for k, v in live[2].items() if k != "id"}  # upsert_node가 주입하는 'id' 제외
                if live[0] == node_type and live_props == props:
                    n_same += 1
                    done = n_new + n_chg + n_same + skip + err
                    if done % 500 == 0:
                        print(f"    …노드(증분) {done} (new={n_new} chg={n_chg} same={n_same} skip={skip} err={err})", flush=True)
                    continue

                if live[0] != node_type:
                    # 타입 변경 — 구 행 고아 방지(신규 타입으로 add_node 하기 전에 구 행 제거)
                    try:
                        graph.delete_node(live[0], node_id)
                    except Exception:
                        pass
                    try:
                        docs.delete_node_doc(live[1] or space, node_id)
                    except Exception:
                        pass

            try:
                builder.add_node(space, node_type, node_id, properties=props)
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
                builder.add_edge(from_space, src_id, relation, to_space, tgt_id, properties=props)
                ok += 1
                if applied is not None:
                    # 반전(do_reverse/REVERSE_RELATIONS) 적용 후의 최종 src/tgt 기준
                    applied.add((src_id, relation, tgt_id))
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
                # 텍스트 불변, 메타만 변경 — 임베딩 없이 upsert_source만(FTS는 자동 싱크)
                try:
                    docs.upsert_source(chunk_id, row["text"], meta)
                    c_meta += 1
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

    반환: {"node_del":…, "chunk_del":…, "edge_del":…, "vec_orphan_del":…}
    """
    require_live_data("incremental_finalize")
    live_nodes  = live["nodes"]
    live_chunks = live["chunks"]
    live_edges  = live["edges"]
    live_vecids = live["vec_ids"]

    # ── 안전핀 0: by-pack 파일 누락 의심 (0-항목인데 라이브엔 데이터 존재) ──────
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

    # ── 삭제 후보 (앵커 제외) ──────────────────────────────────────────────
    def _is_anchor(node_id: str) -> bool:
        if node_id.startswith("dataset:"):
            return True
        props = live_nodes.get(node_id, (None, None, {}))[2]
        return props.get("created_by") == "title-backfill"

    node_del_candidates = {nid for nid in (set(live_nodes) - bypack_node_ids) if not _is_anchor(nid)}
    chunk_del_candidates = set(live_chunks) - bypack_chunk_ids

    # ── 안전핀 30%: 삭제 폭주 방지 ───────────────────────────────────────────
    node_ratio  = len(node_del_candidates)  / max(1, len(live_nodes))
    chunk_ratio = len(chunk_del_candidates) / max(1, len(live_chunks))
    if (node_ratio > 0.30 or chunk_ratio > 0.30) and not force_delete:
        sys.exit(
            f"ERROR: [{pack_name}] 삭제 후보 비율 초과 — "
            f"노드 {len(node_del_candidates)}/{len(live_nodes)}({node_ratio:.1%}) "
            f"청크 {len(chunk_del_candidates)}/{len(live_chunks)}({chunk_ratio:.1%}) — "
            "--force-delete 로 강행하십시오."
        )

    # ── 노드 삭제 ────────────────────────────────────────────────────────
    node_del = 0
    for node_id in node_del_candidates:
        node_type, space_id, _props = live_nodes[node_id]
        try:
            graph.delete_node(node_type, node_id)
        except Exception:
            pass
        try:
            docs.delete_node_doc(space_id or "concept", node_id)
        except Exception:
            pass
        node_del += 1

    # ── 청크 삭제 (delete_pack과 동일 배치 패턴) ──────────────────────────
    chunk_del = 0
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

    # ── 엣지 정리 (live에 있으나 이번 적재에서 재확인되지 않은 엣지) ────────
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
    edge_del = 0
    for (f_id, r, t_id) in stale_edges:
        try:
            graph._conn.execute(
                "DELETE FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?"
                " AND json_extract(properties,'$.pack_id')=?",
                (f_id, r, t_id, pack_name),
            )
            edge_del += 1
        except Exception as exc:
            log.warning("엣지 삭제 오류(%s) %s-%s->%s: %s", pack_name, f_id[:8], r, t_id[:8], exc)
    graph._conn.commit()

    # ── 벡터 고아 정리 (live vec_ids 중 이번 적재의 노드·청크 어느 쪽도 아닌 것) ──
    # 앵커는 노드 삭제와 동일하게 보호 — 현재 앵커 벡터 0건이나 향후 생겨도 오삭제 방지
    vec_orphans = [
        i for i in live_vecids - (bypack_node_ids | bypack_chunk_ids) if not _is_anchor(i)
    ]
    vec_orphan_del = 0
    for batch in _batched(vec_orphans):
        try:
            vec.delete(batch)
            vec_orphan_del += len(batch)
        except Exception as exc:
            log.warning("벡터 고아 삭제 오류(%s): %s", pack_name, exc)

    # ── 3원 대사 출력 (assert 아님 — 숫자 보고) ────────────────────────────
    graph_count = graph._conn.execute(
        "SELECT COUNT(*) FROM graph_nodes WHERE json_extract(properties,'$.pack_id')=?",
        (pack_name,),
    ).fetchone()[0]
    doc_count = docs._conn.execute(
        "SELECT COUNT(*) FROM doc_sources WHERE json_extract(metadata,'$.pack_id')=?"
        " OR json_extract(metadata,'$.source')=?",
        (pack_name, pack_name),
    ).fetchone()[0]
    vec_count = 0
    v_conn  = getattr(vec, "_conn", None)
    v_table = getattr(vec, "_table", None) or "vectors_kure"
    if v_conn is not None:
        vec_count = v_conn.execute(
            f"SELECT COUNT(*) FROM {v_table} WHERE pack_id = ?", (pack_name,)
        ).fetchone()[0]
    vec_expected = len(bypack_node_ids | bypack_chunk_ids)

    print(
        f"  [{pack_name}] 3원 대사: graph {graph_count} (by-pack {nodes_total} {graph_count - nodes_total:+d}) / "
        f"doc {doc_count} (by-pack {chunks_total} {doc_count - chunks_total:+d}) / "
        f"vec {vec_count} (기대 {vec_expected} {vec_count - vec_expected:+d})",
        flush=True,
    )
    print(
        f"  [{pack_name}] 증분 삭제: 노드 {node_del} / 청크 {chunk_del} / 엣지 {edge_del} / 벡터고아 {vec_orphan_del}",
        flush=True,
    )

    return {
        "node_del": node_del,
        "chunk_del": chunk_del,
        "edge_del": edge_del,
        "vec_orphan_del": vec_orphan_del,
    }
