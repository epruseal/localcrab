"""
migrate_to_local.py 핵심 함수 단위 테스트.

모든 테스트는 live 서비스 없이 Mock으로 실행된다.
LocalSQLDocStore 미구현 시 해당 테스트는 skip 처리.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencrab.common.graph_identity import GraphMigrationFixtureOnlyError

# ---------------------------------------------------------------------------
# 모듈 경로 설정 (scripts/ 는 패키지가 아니므로 직접 import)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_to_local as mig  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_neo4j_session(node_rows: list[dict], edge_rows: list[dict]) -> MagicMock:
    """Neo4j driver mock: Cypher 쿼리 내용으로 노드/엣지를 구분해 반환.

    migrate_graph()는 루프마다 ``with driver.session() as sess:`` 를 열고
    sess.run(query, ...) 을 호출한다. 한 페이지가 batch_size 보다 적으면
    루프가 일찍 종료되므로 호출 횟수를 사전에 알 수 없다.

    따라서 session.run() 의 첫 번째 인수(Cypher 쿼리 문자열)를 보고
    'MATCH (n)' → node_rows / 'MATCH (a)-[r]->(b)' → edge_rows 를 반환하며,
    두 번째 이후 동일 패턴 호출(EOF 시뮬레이션)에는 [] 를 반환한다.
    """

    node_call_count = [0]
    edge_call_count = [0]

    def _run_side_effect(query: str, **kwargs) -> list[dict]:
        # 노드 쿼리: MATCH (n) RETURN ...
        if "MATCH (n)" in query:
            node_call_count[0] += 1
            # 첫 번째 호출만 데이터 반환, 이후 EOF
            if node_call_count[0] == 1:
                return node_rows
            return []
        # 엣지 쿼리: MATCH (a)-[r]->(b) ...
        if "MATCH (a)-[r]->(b)" in query:
            edge_call_count[0] += 1
            if edge_call_count[0] == 1:
                return edge_rows
            return []
        # 기타 쿼리
        return []

    session = MagicMock()
    session.run.side_effect = _run_side_effect
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    driver = MagicMock()
    driver.session.return_value = session
    return driver


# ---------------------------------------------------------------------------
# Test: migrate_graph — 정상 노드/엣지 변환
# ---------------------------------------------------------------------------

class TestMigrateGraph:
    def test_migrate_graph_apply_is_fixture_only(self, tmp_path: Path) -> None:
        """Production Neo4j-to-SQL apply remains closed until qualification."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        driver = _make_neo4j_session([], [])
        local_store = LocalGraphStore(db_path=str(tmp_path / "graph.db"))
        try:
            with pytest.raises(GraphMigrationFixtureOnlyError):
                mig.migrate_graph(driver, local_store, batch_size=100, log=MagicMock())
            assert local_store.count_nodes() == 0
        finally:
            local_store.close()

    def test_inspect_graph_reports_duplicate_node_types_and_missing_endpoints(self) -> None:
        node_rows = [
            {"props": {"id": "node-1"}, "labels": ["OpenCrabNode", "Person"], "node_type": "Person"},
            {"props": {"id": "node-1"}, "labels": ["OpenCrabNode", "Agent"], "node_type": "Agent"},
        ]
        edge_rows = [{
            "source_props": {"id": "node-1"}, "source_node_type": "Person",
            "target_props": {"id": "missing"}, "target_node_type": "Person",
            "props": {}, "relation": "KNOWS",
        }]

        report = mig.inspect_graph_source(_make_neo4j_session(node_rows, edge_rows))

        assert report["nodes"] == 2
        assert report["edges"] == 1
        assert report["duplicates"] == [{"node_id": "node-1", "node_types": ["Agent", "Person"]}]
        assert report["incomplete_endpoints"] == [{"relation": "KNOWS", "missing": ["missing"]}]

    def test_inspect_graph_is_read_only_and_uses_explicit_node_type(self) -> None:
        driver = _make_neo4j_session(
            [{"props": {"id": "n1"}, "labels": ["OpenCrabNode", "Legacy"], "node_type": "Person"}],
            [],
        )

        report = mig.inspect_graph_source(driver)

        assert report["duplicates"] == []
        session = driver.session.return_value
        queries = [entry.args[0] for entry in session.run.call_args_list]
        assert all("MATCH" in query and "RETURN" in query for query in queries)
        assert mig._extract_node_type(["OpenCrabNode", "Legacy"], "Person") == "Person"
        assert mig._extract_node_type(["OpenCrabNode", "Legacy"]) == "Legacy"

    def test_inspect_graph_ignores_rows_without_ids_for_identity_checks(self) -> None:
        node_rows = [
            {"props": {"name": "NoId"}, "labels": ["OpenCrabNode", "Ghost"]},
            {"props": {"id": "valid-1"}, "labels": ["OpenCrabNode", "Valid"]},
        ]

        report = mig.inspect_graph_source(_make_neo4j_session(node_rows, []))

        assert report["nodes"] == 2
        assert report["duplicates"] == []

    def test_extract_node_type_falls_back_to_unknown_marker_only(self) -> None:
        assert mig._extract_node_type(["OpenCrabNode"]) == "Unknown"


# ---------------------------------------------------------------------------
# Test: migrate_docs — MongoDB → LocalDocStore
# ---------------------------------------------------------------------------

class TestMigrateDocs:
    def test_migrate_docs_mongo_to_local_doc_store(self, tmp_path: Path) -> None:
        """MongoDB → LocalDocStore 변환 로직 검증."""
        from opencrab.stores.local_doc_store import LocalDocStore

        node_docs = [
            {"node_id": "n1", "space": "test", "node_type": "Person",
             "properties": {"name": "Alice"}},
        ]
        source_docs = [
            {"source_id": "src-1", "text": "Hello world", "metadata": {"lang": "en"}},
        ]
        audit_docs = [
            {"event_type": "create", "subject_id": "n1",
             "details": {"action": "upsert"}, "timestamp": "2024-01-01T00:00:00Z"},
        ]

        db = _make_mongo_db_mock(node_docs, source_docs, audit_docs)
        doc_store = LocalDocStore(data_dir=str(tmp_path / "docs"))

        import logging
        result = mig.migrate_docs(db, doc_store, batch_size=100, log=logging.getLogger())

        assert result["nodes"] == 1
        assert result["sources"] == 1
        assert result["audit_events"] == 1

        # 실제 저장 확인
        node = doc_store.get_node_doc("test", "n1")
        assert node is not None
        assert node["properties"]["name"] == "Alice"

        src = doc_store.get_source("src-1")
        assert src is not None

    def test_migrate_docs_skips_node_without_node_id(self, tmp_path: Path) -> None:
        """node_id 없는 문서는 건너뜀."""
        from opencrab.stores.local_doc_store import LocalDocStore

        node_docs = [
            # node_id 없음
            {"space": "test", "node_type": "X", "properties": {}},
            # 정상
            {"node_id": "valid", "space": "test", "node_type": "Y", "properties": {}},
        ]
        db = _make_mongo_db_mock(node_docs, [], [])
        doc_store = LocalDocStore(data_dir=str(tmp_path / "docs2"))

        import logging
        result = mig.migrate_docs(db, doc_store, batch_size=100, log=logging.getLogger())

        assert result["nodes"] == 1

    def test_migrate_docs_with_local_sql_doc_store(self, tmp_path: Path) -> None:
        """LocalSQLDocStore가 있으면 해당 store로 마이그레이션."""
        pytest.importorskip(
            "opencrab.stores.local_sql_doc_store",
            reason="LocalSQLDocStore 미구현, 스킵",
        )
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore  # type: ignore[import]

        node_docs = [
            {"node_id": "n1", "space": "s", "node_type": "T", "properties": {"x": 1}},
        ]
        db = _make_mongo_db_mock(node_docs, [], [])
        db_path = str(tmp_path / "doc_store.db")
        doc_store = LocalSQLDocStore(db_path=db_path)

        import logging
        result = mig.migrate_docs(db, doc_store, batch_size=100, log=logging.getLogger())
        assert result["nodes"] == 1


# ---------------------------------------------------------------------------
# Test: migrate_vectors — HTTP Chroma → local Chroma
# ---------------------------------------------------------------------------

class TestMigrateVectors:
    def test_migrate_vectors_copy_without_recompute(self, tmp_path: Path) -> None:
        """벡터 마이그레이션 시 임베딩 재계산 없이 그대로 복사."""
        http_col = MagicMock()
        http_col.count.return_value = 3
        # get() 첫 호출 → 데이터, 두 번째 호출(다음 offset) → ids=[]
        first_batch = {
            "ids":        ["v1", "v2", "v3"],
            "embeddings": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
            "documents":  ["doc1", "doc2", "doc3"],
            "metadatas":  [{}, {}, {}],
        }
        http_col.get.side_effect = [first_batch, {"ids": []}]

        http_client = MagicMock()
        http_client.get_collection.return_value = http_col

        local_col = MagicMock()
        local_client = MagicMock()
        local_client.get_or_create_collection.return_value = local_col

        import logging
        result = mig.migrate_vectors(
            http_client, local_client, "test_col", batch_size=100, log=logging.getLogger()
        )

        assert result["vectors"] == 3
        # add() 한 번 호출됐는지, 임베딩 원본 그대로인지 확인
        local_col.add.assert_called_once_with(
            ids=["v1", "v2", "v3"],
            embeddings=[[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
            documents=["doc1", "doc2", "doc3"],
            metadatas=[{}, {}, {}],
        )

    def test_migrate_vectors_missing_collection(self, tmp_path: Path) -> None:
        """소스 컬렉션이 없으면 경고 후 vectors=0 반환."""
        http_client = MagicMock()
        http_client.get_collection.side_effect = Exception("collection not found")

        local_client = MagicMock()

        import logging
        result = mig.migrate_vectors(
            http_client, local_client, "missing_col", batch_size=100, log=logging.getLogger()
        )
        assert result["vectors"] == 0

    def test_migrate_vectors_batching(self, tmp_path: Path) -> None:
        """batch_size보다 많은 벡터가 있으면 여러 번 get() 호출."""
        http_col = MagicMock()
        http_col.count.return_value = 5

        batch1 = {
            "ids": ["v1", "v2", "v3"],
            "embeddings": [[0.1]] * 3,
            "documents":  ["d"] * 3,
            "metadatas":  [{}] * 3,
        }
        batch2 = {
            "ids": ["v4", "v5"],
            "embeddings": [[0.1]] * 2,
            "documents":  ["d"] * 2,
            "metadatas":  [{}] * 2,
        }
        # 세 번째 get() → ids=[] (EOF)
        http_col.get.side_effect = [batch1, batch2, {"ids": []}]

        http_client = MagicMock()
        http_client.get_collection.return_value = http_col

        local_col = MagicMock()
        local_client = MagicMock()
        local_client.get_or_create_collection.return_value = local_col

        import logging
        result = mig.migrate_vectors(
            http_client, local_client, "col", batch_size=3, log=logging.getLogger()
        )
        assert result["vectors"] == 5
        assert local_col.add.call_count == 2


# ---------------------------------------------------------------------------
# Test: backup_local_data
# ---------------------------------------------------------------------------

class TestBackupLocalData:
    """backup_local_data 의 계약 (issues #128, #123).

    산출물 배치가 항목별 ``graph.db.bak.{ts}`` 에서 세트 디렉터리
    ``backup.{ts}.{난수}/graph.db`` 로 바뀌었다. 세트 전체를 rename 한 번으로
    공개해야 부분 공개된 백업이 남지 않기 때문이다. 반환 계약
    ``{원본경로: 백업경로}`` 는 그대로다.

    일관성·완전성 자체의 회귀 테스트는 tests/test_backup_consistency.py 에
    있다. 여기서는 스크립트가 그 모듈에 제대로 위임하는지만 본다.
    """

    @staticmethod
    def _make_db(path: Path, marker: str) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (marker,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _set_dir(root: Path) -> Path:
        sets = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("backup.")]
        assert len(sets) == 1, f"expected exactly one backup set, found {sets}"
        return sets[0]

    def test_backup_creates_a_set_with_files_and_directories(self, tmp_path: Path) -> None:
        """존재하는 파일과 디렉터리가 하나의 백업 세트에 담긴다."""
        self._make_db(tmp_path / "graph.db", "graph-marker")
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        (chroma_dir / "index.bin").write_text("vec")

        backed_up = mig.backup_local_data(str(tmp_path))

        s = self._set_dir(tmp_path)
        assert (s / "graph.db").is_file()
        assert (s / "chroma" / "index.bin").read_text() == "vec"

        conn = sqlite3.connect(str(s / "graph.db"))
        try:
            assert conn.execute("SELECT v FROM t").fetchone()[0] == "graph-marker"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

        assert backed_up[str(tmp_path / "graph.db")] == str(s / "graph.db")

    def test_backup_includes_the_vector_store(self, tmp_path: Path) -> None:
        """#123: vectors.db 가 백업 대상에서 빠져 있었다."""
        self._make_db(tmp_path / "vectors.db", "vector-marker")
        mig.backup_local_data(str(tmp_path))
        assert (self._set_dir(tmp_path) / "vectors.db").is_file()

    def test_backup_skips_missing_files(self, tmp_path: Path) -> None:
        """없는 파일은 조용히 스킵한다 (예외 없음)."""
        backed_up = mig.backup_local_data(str(tmp_path))
        assert backed_up == {}

    def test_set_name_carries_a_timestamp_and_a_unique_suffix(self, tmp_path: Path) -> None:
        """세트 이름은 ``backup.{YYYYMMDD_HHMMSS}.{난수 hex}`` 다.

        난수 접미사가 있어야 같은 초에 두 번 실행해도 충돌하지 않고,
        사전 검사와 rename 사이의 경쟁 창도 실질적으로 닫힌다.
        """
        self._make_db(tmp_path / "graph.db", "x")
        mig.backup_local_data(str(tmp_path))

        name = self._set_dir(tmp_path).name
        assert name.startswith("backup.")
        ts, _, suffix = name[len("backup."):].partition(".")
        assert len(ts) == 15 and "_" in ts, ts  # 20240101_120000
        assert len(suffix) == 6 and suffix, suffix

    def test_no_wal_or_shm_sidecars_in_the_set(self, tmp_path: Path) -> None:
        """온라인 백업 목적지는 동반 파일 없이 단독으로 열린다.

        WAL 연결을 백업 동안 열어 둔다. 마지막 연결을 닫으면 체크포인트가
        일어나 사이드카가 사라지므로, 닫은 뒤 백업하면 애초에 제외할
        사이드카가 없어 아무것도 증명하지 못한다. 아래 전제 단언이 그
        상황을 조용히 통과시키지 않고 드러낸다.
        """
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('x')")
            conn.commit()
            assert (tmp_path / "graph.db-wal").is_file(), (
                "전제 조건 실패: -wal 이 없어 이 테스트는 아무것도 증명하지 못한다"
            )

            mig.backup_local_data(str(tmp_path))
        finally:
            conn.close()

        s = self._set_dir(tmp_path)
        assert not list(s.glob("*-wal"))
        assert not list(s.glob("*-shm"))
        # WAL 을 통과해 읽으므로 사이드카 없이도 내용이 온전해야 한다.
        copied = sqlite3.connect(str(s / "graph.db"))
        try:
            assert copied.execute("SELECT v FROM t").fetchone()[0] == "x"
        finally:
            copied.close()

    def test_a_corrupt_store_file_aborts_the_backup(self, tmp_path: Path) -> None:
        """#128: raw 사본을 백업이라 보고하지 않고 중단한다."""
        from opencrab.stores.backup import BackupError

        (tmp_path / "graph.db").write_text("not a sqlite database")
        with pytest.raises(BackupError):
            mig.backup_local_data(str(tmp_path))
        assert [p for p in tmp_path.iterdir() if p.name.startswith("backup.")] == []


# ---------------------------------------------------------------------------
# Test: --dry-run (파일 생성 없음 확인)
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_makes_no_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run 시 DB 파일 생성/수정 없음.

        main() 에 dry_run=True 를 넘기면 preflight 이후 곧바로 return 하므로
        LocalGraphStore / SQLStore 등이 초기화되지 않아 DB 파일이 생성되지 않는다.
        """
        import argparse

        fake_counts = {
            "neo4j_nodes": 100, "neo4j_edges": 200,
            "mongo_nodes": 50, "mongo_sources": 10, "mongo_audit": 5,
            "chroma_vectors": 30,
            "pg_tables": {"ontology_nodes": 20},
        }
        monkeypatch.setattr(mig, "preflight", lambda _args: {
            "neo4j_driver": MagicMock(),
            "mongo_db": MagicMock(),
            "chroma_http": MagicMock(),
            "pg_engine": MagicMock(),
            "counts": fake_counts,
        })

        args = argparse.Namespace(
            dry_run=True,
            skip_graph=False,
            skip_docs=False,
            skip_vectors=False,
            skip_sql=False,
            batch_size=100,
            local_data_dir=str(tmp_path),
            neo4j_uri="bolt://x:7687",
            neo4j_user="neo4j",
            neo4j_pass="pw",
            mongo_uri="mongodb://x:27017",
            mongo_db="test",
            chroma_host="x",
            chroma_port=8000,
            chroma_collection="col",
            pg_url="postgresql://x/x",
        )
        monkeypatch.setattr(mig, "_parse_args", lambda: args)

        # main() 은 dry-run 시 정상 return (SystemExit 없음)
        mig.main()

        # DB 파일이 생성되지 않아야 함
        db_files = [
            tmp_path / "graph.db",
            tmp_path / "opencrab.db",
            tmp_path / "doc_store.db",
        ]
        for db_file in db_files:
            assert not db_file.exists(), f"{db_file} 이 dry-run 중에 생성됨"


# ---------------------------------------------------------------------------
# Test: _extract_node_type
# ---------------------------------------------------------------------------

class TestExtractNodeType:
    def test_removes_opencrabnode(self) -> None:
        assert mig._extract_node_type(["OpenCrabNode", "Person"]) == "Person"

    def test_multiple_remaining_labels(self) -> None:
        # OpenCrabNode 제거 후 첫 번째 반환
        assert mig._extract_node_type(["OpenCrabNode", "Lever", "Other"]) == "Lever"

    def test_only_opencrabnode(self) -> None:
        assert mig._extract_node_type(["OpenCrabNode"]) == "Unknown"

    def test_empty_labels(self) -> None:
        assert mig._extract_node_type([]) == "Unknown"

    def test_no_opencrabnode(self) -> None:
        assert mig._extract_node_type(["Person"]) == "Person"


# ---------------------------------------------------------------------------
# Helpers (module-level, used by TestMigrateDocs)
# ---------------------------------------------------------------------------

def _make_mongo_db_mock(
    nodes: list[dict],
    sources: list[dict],
    audit: list[dict],
) -> MagicMock:
    """pymongo db mock — __getitem__ 로 컬렉션 접근 지원."""
    def _make_col(docs: list[dict]) -> MagicMock:
        col = MagicMock()
        cursor = MagicMock()
        cursor.__iter__ = MagicMock(return_value=iter(docs))
        cursor.sort = MagicMock(return_value=cursor)
        col.find = MagicMock(return_value=cursor)
        return col

    col_map = {
        "nodes":     _make_col(nodes),
        "sources":   _make_col(sources),
        "audit_log": _make_col(audit),
    }
    db = MagicMock()
    db.__getitem__ = MagicMock(side_effect=lambda name: col_map[name])
    return db


# ---------------------------------------------------------------------------
# Test: migrate_sql — PostgreSQL → SQLite
# ---------------------------------------------------------------------------

class TestMigrateSQL:
    def _make_src_engine(self, tmp_path: Path) -> Any:
        """ontology_nodes 테이블 + 테스트 행을 포함한 SQLite in-memory 엔진."""
        from sqlalchemy import create_engine, text

        src_engine = create_engine("sqlite:///:memory:")
        with src_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE ontology_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    space TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(space, node_id)
                )
            """))
            conn.execute(text(
                "INSERT INTO ontology_nodes(space, node_type, node_id) VALUES('test','Person','alice')"
            ))
        return src_engine

    def test_migrate_sql_inserts_rows(self, tmp_path: Path) -> None:
        """PostgreSQL 테이블 데이터를 SQLite로 복사한다.

        #151: migrate_sql 의 반환 형태가 ``{"tables": {name: int}}`` 에서
        ``{"tables": {name: {"copied": int, "source": int|None, "target": int|None}}}``
        로 바뀌었다(역방향 실제 행 보존 검증, 설계 6절) — 중첩 구조로 갱신한다.
        이 patch 는 "postgresql" URL 요청에 SQLite 엔진을 대신 물리는데, ``**kw``
        를 그대로 전달하므로 #151 이 새로 붙인 ``hide_parameters=True`` 도 그냥
        통과하고, 컬럼 조회가 ``inspect()`` 기반이라 SQLite 카탈로그에서는
        boolean/timestamp 타입이 검출되지 않아(무변환 통과) 이 테스트는 여전히
        유효하다.
        """
        import logging

        import sqlalchemy

        src_engine = self._make_src_engine(tmp_path)
        dst_path = str(tmp_path / "opencrab.db")

        real_create_engine = sqlalchemy.create_engine

        def _patched_create_engine(url, **kw):
            if "postgresql" in str(url):
                return src_engine
            return real_create_engine(url, **kw)

        with patch("sqlalchemy.create_engine", side_effect=_patched_create_engine):
            result = mig.migrate_sql(
                "postgresql://x/x", dst_path, logging.getLogger()
            )

        # ontology_nodes 에 최소 1개 이상 삽입됐는지 확인
        from sqlalchemy import create_engine as ce
        from sqlalchemy import text as t
        eng = ce(f"sqlite:///{dst_path}")
        with eng.connect() as conn:
            row = conn.execute(t("SELECT COUNT(*) FROM ontology_nodes")).fetchone()
        assert row[0] >= 1

        # 반환값 구조 확인 -- 중첩 딕셔너리로 copied/source/target 을 각각 담는다.
        assert "tables" in result
        node = result["tables"]["ontology_nodes"]
        assert node["copied"] >= 1
        assert node["source"] == 1
        assert node["target"] == node["source"]

    def test_migrate_sql_duplicate_rows_not_counted(self, tmp_path: Path) -> None:
        """중복 행은 copied 에 포함되지 않지만, target 은 여전히 source 와 같아야 한다.

        #151 이전에는 두 번째 실행에서 count(=copied) 가 0 이라는 것만 확인했다.
        새 반환 형태의 요점은 삽입 카운트가 아니라 "행이 실제로 보존됐는가"이므로
        (설계 6절), 이 테스트를 그 주장까지 검증하도록 강화한다: INSERT OR IGNORE
        로 두 번째 실행의 copied 는 0 이 되지만, 재조회한 target(SQLite 실제 행 수)
        은 source(PostgreSQL 행 수)와 여전히 같아야 한다 -- 중복이 조용히 행을
        잃어버리지 않았다는 뜻이다.
        """
        import logging

        import sqlalchemy

        src_engine = self._make_src_engine(tmp_path)
        dst_path = str(tmp_path / "dup_test.db")

        real_create_engine = sqlalchemy.create_engine

        def _patched_create_engine(url, **kw):
            if "postgresql" in str(url):
                return src_engine
            return real_create_engine(url, **kw)

        with patch("sqlalchemy.create_engine", side_effect=_patched_create_engine):
            result1 = mig.migrate_sql(
                "postgresql://x/x", dst_path, logging.getLogger()
            )
            result2 = mig.migrate_sql(
                "postgresql://x/x", dst_path, logging.getLogger()
            )

        # 첫 번째 호출: 1개 삽입, target == source
        node1 = result1["tables"]["ontology_nodes"]
        assert node1["copied"] >= 1
        assert node1["target"] == node1["source"]

        # 두 번째 호출: 자연 키가 있는 테이블은 업서트하므로 같은 행을 다시 쓴다
        # (copied 는 쓴 행 수이지 새로 삽입된 행 수가 아니다). 중복되지 않는다는 것은
        # target 이 그대로라는 사실로 확인한다.
        node2 = result2["tables"]["ontology_nodes"]
        assert node2["copied"] == node2["source"]
        assert node2["target"] == node1["target"]
