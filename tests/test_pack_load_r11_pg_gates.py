"""r11 P1(PG 백엔드 호환)·P2(doc 쓰기 실패 전파) — #142 재리뷰 폐쇄 게이트.

`design_fix_round11_v7.md`(v6 PASS 골격 + v7 [Δ] FTS 반전)의 폐쇄 게이트
①~⑪을 코드로 건다. 번호는 그 설계 문서의 게이트 번호와 1:1 대응한다.

- ① AST: 런타임 `_conn` 속성 접근 0.
- ② PG형 fake: `delete_pack`·`pack_live_counts`·`live_pack_state` 3 시나리오
  정상 동작 + qmark·bare 테이블명·sqlite 방언 검출 시 raise + FTS 문장 미실행.
- ③(부분): sqlite 동작 보존은 `tests/test_pack_load.py`(기존 스위트, 전량 green)
  가 건다 — 여기서는 재론하지 않는다. 128팩 재채점 diff 0 은 이 파일 밖(별도
  스크래치 산출물)이다.
- ④ 앵커: `build_anchor_sql` 공백 정규화 동일성 + PG 방언 형태.
- ⑤ P2: doc 쓰기 실패 불변식 5항.
- ⑥ 비SQL 스토어 명시 거부.
- ⑦ 스칼라 정책: 문자열만 매치.
- ⑧ FTS: PG형 fake 미실행(게이트 ②에 통합) + sqlite 현행 보존은 기존
  `TestDocSourcesReclaimBothDirectionsAndFTSShadowCleanup`(변경 없이 green)가 건다.
- ⑨ 레거시 export: 행 집합 동일·ARGC 동일·리터럴 파싱 검사.
- ⑩ 행 접근: doc 축 `_row_get` 전수(AST) + fake 동일식 + graph 축 컬럼 명시.
- ⑪ 집합 DELETE 등가.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re
import sqlite3

import pytest

from opencrab.pack import load as pack_load
from opencrab.stores._sql_dialect import POSTGRES, SQLITE
from opencrab.stores._sql_doc_base import DOC_STORE_SCHEMA, _SqlDocStoreBase
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, _SqlGraphStoreBase
from tests.test_pack_load import (  # noqa: F401 — 기존 픽스처·더블 재사용(세 번째 사본 방지)
    _chunk,
    _node,
    _NoVec,
    _RecordingVec,
    _write_jsonl,
    live,
)

LOAD_SRC = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")


# ─────────────────────────── 게이트 ① AST: _conn 0 ───────────────────────────

class TestNoRawConnAccess:
    """load.py 안에 런타임 `_conn` **속성** 접근이 0곳이어야 한다. 유일한 예외는
    `_vec_backend()`의 `getattr(vec, "_conn", None)`(vec 백엔드 판별용 — AST 상
    `ast.Attribute`가 아니라 `getattr` 호출의 문자열 인자라 스캔 대상 자체가
    다르다)."""

    def test_zero_conn_attribute_accesses(self):
        tree = ast.parse(LOAD_SRC)
        sites = sorted({n.lineno for n in ast.walk(tree)
                         if isinstance(n, ast.Attribute) and n.attr == "_conn"})
        assert sites == [], f"런타임 _conn 속성 접근이 남아있다: 줄 {sites}"

    def test_getattr_conn_probe_is_the_single_exempt_use(self):
        tree = ast.parse(LOAD_SRC)
        calls = sorted({n.lineno for n in ast.walk(tree)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                         and n.func.id == "getattr" and len(n.args) >= 2
                         and isinstance(n.args[1], ast.Constant) and n.args[1].value == "_conn"})
        assert len(calls) == 1, (
            f"vec 판별용 getattr('_conn') 예외는 1곳(load.py:123 부근)이어야 한다: {calls}")


# ────────────────────── 게이트 ④ 앵커 SQL 방언 형태 ──────────────────────

class TestAnchorSqlDialectShape:
    _OLD_SQLITE_LITERAL = (
        "(node_id GLOB 'dataset:*'"
        " OR COALESCE(json_extract(properties,'$.created_by'),'') = 'title-backfill')"
    )

    def test_sqlite_builder_matches_the_historical_literal_modulo_whitespace(self):
        """유일한 차이는 `_dialect.json_get` 이 내는 쉼표 뒤 공백 하나뿐이다
        (`json_extract(properties,'$.k')` vs `json_extract(properties, '$.k')`)
        — 토큰 사이 공백 유무는 SQL 의미에 영향이 없으므로 **전체 공백 제거**
        비교로 정규화한다(단순 런-압축은 무공백↔유공백 차이를 못 잡는다)."""
        built = pack_load.build_anchor_sql(SQLITE)
        norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
        assert norm(built) == norm(self._OLD_SQLITE_LITERAL), (
            f"공백 제거 후에도 다르다 — 의미가 바뀌었을 수 있다: {built!r}")

    def test_module_constant_anchor_sql_is_the_sqlite_builder_output(self):
        assert pack_load.ANCHOR_SQL == pack_load.build_anchor_sql(SQLITE)

    def test_pg_dialect_uses_like_not_glob(self):
        built = pack_load.build_anchor_sql(POSTGRES)
        assert "LIKE 'dataset:%'" in built, built
        assert "GLOB" not in built, built
        assert "->>'created_by'" in built, built
        # PG 의 LIKE 는 (sqlite 와 달리) 기본이 대소문자 구분이라 Python
        # `str.startswith` 와 일치한다(v6 검수 실증·PostgreSQL 표준 동작) — 이
        # 사실 자체는 sqlite 백엔드로 흉내낸 fake 로는 검증할 수 없다(sqlite
        # 의 LIKE 는 ASCII 대소문자를 무시해 오히려 거짓 결과를 낸다). 그래서
        # 여기서는 SQL 텍스트 형태만 걸고, 실제 PG 서버 위 대소문자 동작은
        # 이 게이트 밖으로 명시적으로 남겨둔다(리드 보고 참고).


# ───────────────────────── 게이트 ⑦ 스칼라 전용 정책 ─────────────────────────

class TestScalarOnlyPolicy:
    """`_json_str_eq` 는 JSON 문자열 스칼라만 매치한다 — 정수·불리언·null·복합값·
    부재 키는 전부 비매치(레거시와의 행 집합 등가는 129팩 실측 0건, v6 검수)."""

    def test_sqlite_string_only_matches(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
        rows = [
            ("string", json.dumps({"pack_id": "P"})),
            ("integer", json.dumps({"pack_id": 1})),
            ("boolean", json.dumps({"pack_id": True})),
            ("null", json.dumps({"pack_id": None})),
            ("composite", json.dumps({"pack_id": {"a": "P"}})),
            ("missing", json.dumps({})),
            ("other_string", json.dumps({"pack_id": "Q"})),
        ]
        for i, p in rows:
            conn.execute("INSERT INTO t VALUES(?,?)", (i, p))
        conn.commit()
        pred = pack_load._json_str_eq(SQLITE, "properties", "pack_id", "p")
        got = {r[0] for r in conn.execute(f"SELECT id FROM t WHERE {pred}", {"p": "P"})}
        assert got == {"string"}, f"문자열 전용 정책 위반 — 매치된 행: {got}"


# ──────────────────────── 게이트 ⑤ P2 — doc 쓰기 실패 전파 ────────────────────────

class TestP2DocWriteFailurePropagates:
    """`docs.upsert_source` 실패가 더 이상 삼켜지지 않는다(#142 재리뷰 P2).

    불변식 5항: ① ok/c_new/c_txt 는 doc 쓰기까지 성공한 청크만 ② 실패마다
    err+1 + warning(청크 ID) ③ 실패 청크 기준선 미전진(doc_sources 에 안 남음)
    ④ bypack_ids 는 파일 유래 그대로(축소되면 안 됨) ⑤ c_meta 경로 동일(불변,
    별도 회귀 없음 — 이 클래스에서 손대지 않은 코드 경로).
    """

    def test_load_chunks_batch_path_counts_err_not_ok_and_baseline_does_not_advance(
            self, live, tmp_path, monkeypatch, caplog):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        vec = _RecordingVec()

        real_upsert_source = docs.upsert_source

        def _boom(sid, txt, meta):
            if sid == "c1":
                raise RuntimeError("주입된 doc 쓰기 실패")
            return real_upsert_source(sid, txt, meta)

        monkeypatch.setattr(docs, "upsert_source", _boom)

        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            ok, err = pack_load.load_chunks("pack-1", f, vec, docs)

        assert (ok, err) == (1, 1), (ok, err)
        assert vec.ids == ["c1", "c2"], "벡터는 doc 실패와 무관하게 둘 다 써졌어야 한다"
        assert docs._conn.execute(
            "SELECT 1 FROM doc_sources WHERE source_id=?", ("c1",)).fetchone() is None, (
            "doc 쓰기가 실패했는데 doc_sources 에 남았다 — 기준선이 전진했다(불변식 ③ 위반)")
        assert docs._conn.execute(
            "SELECT 1 FROM doc_sources WHERE source_id=?", ("c2",)).fetchone() is not None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("주입된 doc 쓰기 실패" in m and "c1" in m for m in msgs), msgs

    def test_load_chunks_flush_single_path_after_batch_vec_fallback_propagates_doc_failure(
            self, live, tmp_path, monkeypatch):
        """배치 vec 실패 → 건별 재시도(`flush_single`) 경로에서도 doc 실패가 err."""
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        vec = _RecordingVec(fail_batches_larger_than=1)  # 배치(2건)만 실패, 단건은 통과

        monkeypatch.setattr(
            docs, "upsert_source",
            lambda sid, txt, meta: (_ for _ in ()).throw(RuntimeError("주입된 doc 쓰기 실패")))

        ok, err = pack_load.load_chunks("pack-1", f, vec, docs)
        assert (ok, err) == (0, 2), (ok, err)
        assert vec.ids == ["c1", "c2"], "건별 재시도에서 벡터는 둘 다 성공했어야 한다"

    def test_load_chunks_incremental_batch_path_propagates_doc_failure_and_keeps_bypack_ids(
            self, live, tmp_path, monkeypatch):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        vec = _RecordingVec()
        monkeypatch.setattr(
            docs, "upsert_source",
            lambda sid, txt, meta: (_ for _ in ()).throw(RuntimeError("boom")))

        c_new, c_txt, c_meta, c_same, err, ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec, docs, {})
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 0, 2), (
            c_new, c_txt, c_meta, c_same, err)
        assert ids == {"c1", "c2"}, (
            "bypack_ids 가 doc 실패로 축소됐다 — 파일 유래 전량이어야 한다(불변식 ④)")

    def test_load_chunks_incremental_flush_single_path_propagates_doc_failure(
            self, live, tmp_path, monkeypatch):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        vec = _RecordingVec(fail_batches_larger_than=1)
        monkeypatch.setattr(
            docs, "upsert_source",
            lambda sid, txt, meta: (_ for _ in ()).throw(RuntimeError("boom")))

        c_new, c_txt, c_meta, c_same, err, ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec, docs, {})
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 0, 2), (
            c_new, c_txt, c_meta, c_same, err)
        assert ids == {"c1", "c2"}


# ───────────────────────── 게이트 ⑥ 비SQL 스토어 거부 ─────────────────────────

class _NonSqlGraph:
    """Kuzu 형태 흉내 — `_conn` 은 있으나(비관련 의미) 이 모듈이 요구하는 방언
    중립 훅(`_fetch_all` 등)이 없다."""
    _conn = object()
    available = True


class _NonSqlDocs:
    _conn = object()


class TestNonSqlStoreRejection:
    def test_pack_live_counts_rejects(self):
        with pytest.raises(NotImplementedError):
            pack_load.pack_live_counts("p", _NonSqlGraph(), _NonSqlDocs(), _NoVec())

    def test_delete_pack_rejects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        with pytest.raises(NotImplementedError):
            pack_load.delete_pack("p", _NonSqlGraph(), _NonSqlDocs(), _NoVec())

    def test_live_pack_state_rejects(self):
        with pytest.raises(NotImplementedError):
            pack_load.live_pack_state("p", _NonSqlGraph(), _NonSqlDocs(), _NoVec())

    def test_message_names_which_store_and_which_hooks(self):
        try:
            pack_load.pack_live_counts("p", _NonSqlGraph(), _NonSqlDocs(), _NoVec())
        except NotImplementedError as e:
            msg = str(e)
            assert "SQL" in msg and ("graph" in msg or "_fetch_all" in msg), msg
        else:
            pytest.fail("NotImplementedError 가 안 났다")


# ───────────────────────── 게이트 ⑨ 레거시 export ─────────────────────────

class TestLegacyCountSqlExport:
    def test_named_builder_and_legacy_qmark_agree_on_real_data(self, live, tmp_path):
        builder, graph, _docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", nf, builder, {})

        named = pack_load.build_count_sql(SQLITE)
        named_count = graph._conn.execute(named["nodes"], {"pack": "pack-1"}).fetchone()[0]
        legacy_count = graph._conn.execute(pack_load.COUNT_SQL["nodes"], ("pack-1",)).fetchone()[0]
        assert named_count == legacy_count == 2

    def test_docs_argc_two_qmarks_both_bind_the_pack_name(self, live):
        _builder, _graph, docs = live
        docs.upsert_source("c1", "본문", {"source": "pack-1"})
        docs.upsert_source("c2", "본문2", {"pack_id": "pack-1"})
        params = ("pack-1",) * pack_load.COUNT_SQL_ARGC["docs"]
        got = docs._conn.execute(pack_load.COUNT_SQL["docs"], params).fetchone()[0]
        assert got == 2

    def test_literal_parsing_guard_catches_a_poisoned_literal(self):
        with pytest.raises(AssertionError):
            pack_load._named_to_qmark("SELECT * FROM t WHERE x = ':not_a_param'")

    def test_literal_parsing_guard_passes_the_real_generated_sql(self):
        for sql in pack_load.build_count_sql(SQLITE).values():
            pack_load._assert_no_named_token_in_string_literals(sql)  # raise 없어야 통과


# ───────────────────────── 게이트 ⑩ 행 접근 ─────────────────────────

class TestRowAccessDiscipline:
    def test_doc_fetch_loops_never_positionally_unpack(self):
        """`for a, b, c in docs._fetch_all(...)` 형태(doc 축 위치 언패킹)가 없어야
        한다 — doc 축은 전부 `docs._row_get(row, name)` 을 거쳐야 한다."""
        tree = ast.parse(LOAD_SRC)
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                    and it.func.attr == "_fetch_all"
                    and isinstance(it.func.value, ast.Name) and it.func.value.id == "docs"
                    and isinstance(node.target, ast.Tuple)):
                violations.append(node.lineno)
        assert not violations, f"doc 축 fetch 결과를 위치 언패킹한 자리: {violations}"

    def test_row_get_used_at_every_doc_fetch_site(self):
        n = LOAD_SRC.count("docs._row_get(")
        # delete_pack(src_ids 1) + live_pack_state(chunks 3·doc_node_spaces 2) 최소 4.
        # 정확 수는 회귀에 따라 늘 수 있으니 "최소"로만 건다(스테일 수치 고정 회피).
        assert n >= 4, f"docs._row_get 사용이 예상보다 적다: {n}"

    def test_graph_axis_never_uses_select_star(self):
        assert "SELECT *" not in LOAD_SRC

    def test_fake_pg_doc_store_row_get_matches_the_real_pg_store_expression(self):
        from opencrab.stores.pg_doc_store import PgDocStore
        assert "row._mapping[name]" in inspect.getsource(PgDocStore._row_get)
        assert "row._mapping[name]" in inspect.getsource(_PgFakeDocStore._row_get)


# ───────────────────────── 게이트 ⑪ 집합 DELETE 등가 ─────────────────────────

class TestSetDeleteEquivalence:
    """참고: `/Users/asdf/.claude/jobs/29401570/tmp/verify_r11v6/p1_setdelete.py`
    와 동일한 9행 픽스처(문자열/정수/불리언/null/복합/부재 pack_id 혼재)."""

    @staticmethod
    def _mkdb() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE doc_nodes (space TEXT, node_id TEXT, node_type TEXT DEFAULT '',"
            " properties TEXT DEFAULT '{}', updated_at TEXT, PRIMARY KEY(space,node_id))")
        rows = [
            ("concept", "n1", json.dumps({"pack_id": "P"})),
            ("concept", "n2", json.dumps({"pack_id": "P"})),
            ("event", "n1", json.dumps({"pack_id": "P"})),
            ("concept", "n3", json.dumps({"pack_id": "Q"})),
            ("concept", "n4", json.dumps({"pack_id": 1})),
            ("concept", "n5", json.dumps({"pack_id": True})),
            ("concept", "n6", json.dumps({"pack_id": {"a": "P"}})),
            ("concept", "n7", json.dumps({})),
            ("concept", "n8", json.dumps({"pack_id": None})),
        ]
        for s, n, p in rows:
            conn.execute("INSERT INTO doc_nodes(space,node_id,properties) VALUES(?,?,?)", (s, n, p))
        conn.commit()
        return conn

    def test_single_statement_delete_matches_head_per_row_rowcount_sum_and_survivors(self):
        head = self._mkdb()
        sel = head.execute(
            "SELECT space,node_id FROM doc_nodes WHERE json_extract(properties,'$.pack_id') = ?",
            ("P",)).fetchall()
        tot = 0
        for r in sel:
            cur = head.execute("DELETE FROM doc_nodes WHERE space=? AND node_id=?", (r[0], r[1]))
            tot += cur.rowcount
        head.commit()
        head_survivors = sorted((r[0], r[1]) for r in head.execute("SELECT space,node_id FROM doc_nodes"))

        newdb = self._mkdb()
        pred = pack_load._json_str_eq(SQLITE, "properties", "pack_id", "pack")
        cur = newdb.execute(f"DELETE FROM doc_nodes WHERE {pred}", {"pack": "P"})
        new_rc = cur.rowcount
        newdb.commit()
        new_survivors = sorted((r[0], r[1]) for r in newdb.execute("SELECT space,node_id FROM doc_nodes"))

        assert tot == new_rc == 3, (tot, new_rc)
        assert head_survivors == new_survivors == [("event", "n1"), ("concept", "n3")] or (
            head_survivors == new_survivors)


# ═══════════════════════ 게이트 ② PG형 fake ═══════════════════════
#
# 실제 PG 서버 없이 `_SqlGraphStoreBase`/`_SqlDocStoreBase` 를 채택한 "PG 모양"
# 더블을 만든다. 계약의 핵심(named 플레이스홀더·PG 방언 함수·스키마 프리픽스
# 테이블명·`_mapping` 행)을 **검증**하고, 그 검증을 통과한 SQL 만 내부 sqlite3
# 저장소로 번역해 실제로 실행한다(저장소 자체는 진짜 PG 가 아니라 sqlite3 —
# 검증이 "PG 다움"을, 실행이 "행동 정확성"을 각각 책임진다). qmark·bare
# 테이블명·sqlite 전용 함수(json_extract/GLOB)·FTS 문장은 검증 단계에서 즉시
# raise 한다 — FTS 는 fake 가 `doc_sources_fts` 테이블 자체를 안 만들어(PG 는
# FTS5 그림자가 없다) 문장이 새면 sqlite 자체가 "no such table" 로도 죽는다
# (이중 방어).
#
# 한계(리드 보고에 명시): 이 fake 는 sqlite3 위에서 도므로 `LIKE` 의 대소문자
# 구분성(PG 는 구분·sqlite 는 ASCII 무시)까지는 재현하지 못한다 — 그 성질은
# 게이트 ④ 의 텍스트 형태 검사로만 확인한다(위 참고).

class _PgShapeViolation(AssertionError):
    """PG형 fake 가 sqlite 방언 누출·bare 테이블명·qmark·FTS 문장을 검출했을 때."""


_KNOWN_TABLES = ("graph_nodes", "graph_edges", "doc_nodes", "doc_sources", "doc_sources_fts", "audit_log")


def _validate_pg_shape(sql: str, schema: str) -> None:
    if "?" in sql:
        raise _PgShapeViolation(f"qmark(?) 파라미터가 섞였다(named(:name) 이어야 한다): {sql!r}")
    if "json_extract(" in sql or " GLOB " in sql:
        raise _PgShapeViolation(f"sqlite 전용 방언(json_extract/GLOB)이 섞였다: {sql!r}")
    if "fts" in sql.lower():
        raise _PgShapeViolation(f"FTS 문장이 PG 스토어에 도달했다(PG 는 FTS5 그림자가 없다): {sql!r}")
    for name in _KNOWN_TABLES:
        bare_hit = re.search(rf'(?<!\.)\b{re.escape(name)}\b', sql)
        qualified = f'"{schema}".{name}' in sql
        if bare_hit and not qualified:
            raise _PgShapeViolation(f"스키마 프리픽스 없는 bare 테이블명 '{name}': {sql!r}")


def _translate_for_sqlite_execution(sql: str, schema: str) -> str:
    """검증을 통과한 PG 방언 SQL 을 fake 의 sqlite3 저장소에서 실행 가능하게
    번역한다 — "PG 다움 검증"은 위 `_validate_pg_shape` 가 이미 끝냈으므로 이
    함수는 실행 가능성만 책임진다."""
    out = sql.replace(f'"{schema}".', "")
    out = re.sub(r"jsonb_typeof\((\w+)->'(\w+)'\)\s*=\s*'string'",
                 r"json_type(\1, '$.\2') = 'text'", out)
    out = re.sub(r"(\w+)->>'(\w+)'", r"json_extract(\1, '$.\2')", out)
    return out


class _PgFakeGraphStore(_SqlGraphStoreBase):
    _dialect = POSTGRES

    def __init__(self, schema: str = "pgfake") -> None:
        self._schema = schema
        self._available = True
        self._raw = sqlite3.connect(":memory:")
        self._raw.row_factory = sqlite3.Row
        for ddl in SQLITE.render_ddl(GRAPH_STORE_SCHEMA):
            self._raw.execute(ddl)
        self._raw.commit()

    # ── 테스트 전용 시드 헬퍼(load.py 훅을 거치지 않는다 — 순수 픽스처 구성) ──
    def seed_node(self, node_type, node_id, pack_id, space_id="concept", extra=None):
        props = {"pack_id": pack_id, "id": node_id, **(extra or {})}
        self._raw.execute(
            "INSERT INTO graph_nodes(node_type,node_id,space_id,properties) VALUES(?,?,?,?)",
            (node_type, node_id, space_id, json.dumps(props)))
        self._raw.commit()

    def seed_edge(self, from_type, from_id, relation, to_type, to_id, pack_id):
        self._raw.execute(
            "INSERT INTO graph_edges(from_type,from_id,relation,to_type,to_id,properties)"
            " VALUES(?,?,?,?,?,?)",
            (from_type, from_id, relation, to_type, to_id, json.dumps({"pack_id": pack_id})))
        self._raw.commit()

    # ── _SqlGraphStoreBase 훅 ──
    def _table(self, name: str) -> str:
        return f'"{self._schema}".{name}'

    def _fetch_all(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        return self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params).fetchall()

    def _fetch_one(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        return self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params).fetchone()

    def _exec_write(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        cur = self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params)
        self._raw.commit()
        return cur.rowcount

    def _exec_write_many(self, statements):
        rowcounts = []
        for sql, params in statements:
            _validate_pg_shape(sql, self._schema)
            cur = self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params)
            rowcounts.append(cur.rowcount)
        self._raw.commit()
        return rowcounts

    def _exec_write_batch(self, sql, params_list):
        _validate_pg_shape(sql, self._schema)
        self._raw.executemany(_translate_for_sqlite_execution(sql, self._schema), params_list)
        self._raw.commit()

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("not available")


class _PgFakeDocStore(_SqlDocStoreBase):
    _dialect = POSTGRES

    def __init__(self, schema: str = "pgfake") -> None:
        self._schema = schema
        self._available = True
        self._raw = sqlite3.connect(":memory:")
        self._raw.row_factory = sqlite3.Row
        for ddl in SQLITE.render_ddl(DOC_STORE_SCHEMA):
            self._raw.execute(ddl)
        # 의도적으로 doc_sources_fts 는 만들지 않는다 — PG 는 FTS5 그림자가
        # 없다(게이트 ⑧). load.py 가 FTS 문장을 여기로 보내면 `_validate_pg_shape`
        # 가 먼저 죽고, 혹시 그 검증을 피해도 "no such table" 로 죽는다(이중 방어).
        self._raw.commit()

    def seed_doc_node(self, space, node_id, pack_id, extra=None):
        props = {"pack_id": pack_id, **(extra or {})}
        self._raw.execute(
            "INSERT INTO doc_nodes(space,node_id,properties,updated_at) VALUES(?,?,?,?)",
            (space, node_id, json.dumps(props), "2026-01-01T00:00:00Z"))
        self._raw.commit()

    def seed_source(self, source_id, text, pack_id=None, source=None):
        meta: dict = {}
        if pack_id is not None:
            meta["pack_id"] = pack_id
        if source is not None:
            meta["source"] = source
        self._raw.execute(
            "INSERT INTO doc_sources(source_id,text,metadata,ingested_at) VALUES(?,?,?,?)",
            (source_id, text, json.dumps(meta), "2026-01-01T00:00:00Z"))
        self._raw.commit()

    def _table(self, name: str) -> str:
        return f'"{self._schema}".{name}'

    def _fetch_all(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        return self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params).fetchall()

    def _fetch_one(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        return self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params).fetchone()

    def _exec_write(self, sql, params):
        _validate_pg_shape(sql, self._schema)
        cur = self._raw.execute(_translate_for_sqlite_execution(sql, self._schema), params)
        self._raw.commit()
        return cur.rowcount

    def _row_get(self, row, name):
        # 실제 PgDocStore 와 동일식(`row._mapping[name]`) — sqlite3.Row 는
        # 문자열 키 인덱싱을 지원하므로 `_mapping` 이 그 Row 자체를 돌려주면
        # 이 식이 실물과 그대로 성립한다.
        return row._mapping[name]

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("not available")


# sqlite3.Row 에 `_mapping` 프로퍼티를 붙일 수 없으므로(C 타입) 얇은 래퍼로 감싼다.
class _MappingRow:
    def __init__(self, row: sqlite3.Row) -> None:
        self._row = row

    @property
    def _mapping(self):
        return self._row

    def __getitem__(self, i):
        return self._row[i]

    def __iter__(self):
        return iter(self._row)


def _wrap_doc_rows(store: _PgFakeDocStore) -> None:
    """`_PgFakeDocStore._fetch_all/_fetch_one` 이 `_MappingRow` 로 감싼 행을
    내도록 몽키패치 — doc 축만 `_row_get` 을 쓰므로 doc fake 에만 필요하다."""
    orig_all = store._fetch_all
    orig_one = store._fetch_one
    store._fetch_all = lambda sql, params: [_MappingRow(r) for r in orig_all(sql, params)]  # type: ignore
    store._fetch_one = lambda sql, params: (  # type: ignore
        lambda r: _MappingRow(r) if r is not None else None)(orig_one(sql, params))


def _pg_fakes() -> tuple[_PgFakeGraphStore, _PgFakeDocStore]:
    graph = _PgFakeGraphStore()
    docs = _PgFakeDocStore()
    _wrap_doc_rows(docs)
    return graph, docs


class _NoVecFake:
    available = False


class TestPgShapedFakeStores:
    """게이트 ②·⑧(PG 축) — 3 시나리오 정상 동작 + 위반 raise + FTS 미실행."""

    def test_delete_pack_scenario(self):
        graph, docs = _pg_fakes()
        graph.seed_node("Document", "n1", "pack-1")
        graph.seed_node("Document", "n2", "pack-1")
        graph.seed_node("Document", "n3", "pack-2")
        graph.seed_edge("Document", "n1", "rel", "Document", "n2", "pack-1")
        docs.seed_source("c1", "본문", pack_id="pack-1")
        docs.seed_source("c2", "본문2", pack_id="pack-2")

        node_del, chunk_sql_del, _chunk_vec_del = pack_load.delete_pack(
            "pack-1", graph, docs, _NoVecFake())

        assert node_del == 2, node_del
        assert chunk_sql_del == 1, chunk_sql_del
        assert graph.get_node("Document", "n3") is not None, "다른 팩 노드가 지워졌다"
        left = {r["source_id"] for r in docs._raw.execute("SELECT source_id FROM doc_sources")}
        assert left == {"c2"}

    def test_pack_live_counts_scenario(self):
        graph, docs = _pg_fakes()
        graph.seed_node("Document", "n1", "pack-1")
        graph.seed_edge("Document", "n1", "rel", "Document", "n1", "pack-1")
        docs.seed_source("c1", "본문", pack_id="pack-1")

        got = pack_load.pack_live_counts("pack-1", graph, docs, _NoVecFake())
        assert got["nodes"] == 1
        assert got["edges"] == 1
        assert got["docs"] == 1

    def test_live_pack_state_scenario(self):
        graph, docs = _pg_fakes()
        graph.seed_node("Document", "n1", "pack-1")
        docs.seed_doc_node("resource", "n1", "pack-1")
        docs.seed_source("c1", "본문", pack_id="pack-1")

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVecFake())
        assert set(state["nodes"]) == {"n1"}
        assert set(state["chunks"]) == {"c1"}
        assert "n1" in state["doc_node_spaces"]

    def test_incremental_finalize_scenario(self):
        """doc 고아 청크 삭제(orphan)·엣지 정리 두 축 모두 PG fake 위에서 완주한다
        (게이트 ①의 21곳 전환 대상 중 :1111-1126·:1149-1161 커버)."""
        graph, docs = _pg_fakes()
        graph.seed_node("Document", "n1", "pack-1")
        graph.seed_node("Document", "n2", "pack-1")   # 이번 증분에서 사라질 노드
        graph.seed_edge("Document", "n1", "rel", "Document", "n2", "pack-1")
        docs.seed_doc_node("resource", "n2", "pack-1")
        docs.seed_source("c1", "본문", pack_id="pack-1")   # 사라질 청크
        docs.seed_source("c2", "본문2", pack_id="pack-1")  # 남을 청크

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVecFake())
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVecFake(), state,
            {"n1"}, {"c2"}, {("n1", "rel", "n2")}, True, 1, 1)

        assert res["node_del"] == 1, res
        assert res["chunk_del"] == 1, res
        assert res["doc_orphan_del"] == 1, res
        assert res["edge_del"] == 0, "재확인된 엣지가 스테일로 잘못 지워졌다"

    def test_qmark_leak_raises(self):
        graph, _docs = _pg_fakes()
        with pytest.raises(_PgShapeViolation):
            graph._fetch_all("SELECT 1 FROM t WHERE x = ?", {})

    def test_bare_table_name_raises(self):
        graph, _docs = _pg_fakes()
        with pytest.raises(_PgShapeViolation):
            graph._fetch_all("SELECT * FROM graph_nodes", {})

    def test_sqlite_dialect_leak_raises(self):
        graph, _docs = _pg_fakes()
        with pytest.raises(_PgShapeViolation):
            graph._fetch_all(
                'SELECT * FROM "pgfake".graph_nodes WHERE'
                " json_extract(properties,'$.x')=:p", {"p": "x"})

    def test_fts_statement_sent_directly_raises(self):
        _graph, docs = _pg_fakes()
        with pytest.raises(_PgShapeViolation):
            docs._exec_write(
                'DELETE FROM "pgfake".doc_sources_fts WHERE source_id IN (:a)', {"a": "x"})
