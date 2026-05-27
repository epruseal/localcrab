"""
migrate_to_local.py 핵심 함수 단위 테스트.

모든 테스트는 live 서비스 없이 Mock으로 실행된다.
LocalSQLDocStore 미구현 시 해당 테스트는 skip 처리.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 모듈 경로 설정 (scripts/ 는 패키지가 아니므로 직접 import)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_to_local as mig


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

    def _make_result(data: list[dict]) -> MagicMock:
        r = MagicMock()
        r.data.return_value = data
        return r

    node_call_count = [0]
    edge_call_count = [0]

    def _run_side_effect(query: str, **kwargs) -> MagicMock:
        # 노드 쿼리: MATCH (n) RETURN ...
        if "MATCH (n)" in query:
            node_call_count[0] += 1
            # 첫 번째 호출만 데이터 반환, 이후 EOF
            if node_call_count[0] == 1:
                return _make_result(node_rows)
            return _make_result([])
        # 엣지 쿼리: MATCH (a)-[r]->(b) ...
        if "MATCH (a)-[r]->(b)" in query:
            edge_call_count[0] += 1
            if edge_call_count[0] == 1:
                return _make_result(edge_rows)
            return _make_result([])
        # 기타 쿼리
        return _make_result([])

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
    def test_migrate_graph_neo4j_to_local(self, tmp_path: Path) -> None:
        """Neo4j → LocalGraphStore 변환 로직 검증."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        node_rows = [
            {
                "props": {"id": "node-1", "name": "Alice", "space": "default"},
                "labels": ["OpenCrabNode", "Person"],
            },
            {
                "props": {"id": "node-2", "name": "Bob"},
                "labels": ["OpenCrabNode", "Person"],
            },
        ]
        edge_rows = [
            {
                "from_id": "node-1",
                "from_labels": ["OpenCrabNode", "Person"],
                "relation": "KNOWS",
                "rel_props": {"since": 2020},
                "to_id": "node-2",
                "to_labels": ["OpenCrabNode", "Person"],
            }
        ]
        driver = _make_neo4j_session(node_rows, edge_rows)

        db_path = str(tmp_path / "graph.db")
        local_store = LocalGraphStore(db_path=db_path)
        import logging
        result = mig.migrate_graph(driver, local_store, batch_size=100, log=logging.getLogger())

        assert result["nodes"] == 2
        assert result["edges"] == 1

        # 실제 DB에 저장됐는지 확인
        assert local_store.count_nodes() == 2
        node = local_store.get_node("Person", "node-1")
        assert node is not None
        assert node.get("name") == "Alice"
        local_store.close()

    def test_migrate_graph_skips_node_without_id(self, tmp_path: Path) -> None:
        """id 없는 노드는 경고만 출력하고 건너뜀."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        node_rows = [
            # id 없음
            {"props": {"name": "NoId"}, "labels": ["OpenCrabNode", "Ghost"]},
            # id 있음
            {"props": {"id": "valid-1"}, "labels": ["OpenCrabNode", "Valid"]},
        ]
        driver = _make_neo4j_session(node_rows, [])

        db_path = str(tmp_path / "graph_skip.db")
        local_store = LocalGraphStore(db_path=db_path)
        import logging
        result = mig.migrate_graph(driver, local_store, batch_size=100, log=logging.getLogger())

        # id 없는 노드는 스킵 → 1개만 저장
        assert result["nodes"] == 1
        assert local_store.count_nodes() == 1
        local_store.close()

    def test_labels_opencrbanode_removed(self, tmp_path: Path) -> None:
        """labels에서 OpenCrabNode 제거 후 node_type 추출."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        node_rows = [
            {"props": {"id": "n1"}, "labels": ["OpenCrabNode", "Lever", "ExtraLabel"]},
        ]
        driver = _make_neo4j_session(node_rows, [])

        db_path = str(tmp_path / "graph_labels.db")
        local_store = LocalGraphStore(db_path=db_path)
        import logging
        mig.migrate_graph(driver, local_store, batch_size=100, log=logging.getLogger())

        # node_type = 'Lever' (OpenCrabNode 제거 후 첫 번째)
        node = local_store.get_node("Lever", "n1")
        assert node is not None
        local_store.close()

    def test_migrate_graph_skips_edge_without_ids(self, tmp_path: Path) -> None:
        """from_id 또는 to_id 없는 엣지는 스킵."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        node_rows = [
            {"props": {"id": "n1"}, "labels": ["Node"]},
            {"props": {"id": "n2"}, "labels": ["Node"]},
        ]
        edge_rows = [
            # to_id 없음
            {"from_id": "n1", "from_labels": ["Node"], "relation": "REL",
             "rel_props": {}, "to_id": None, "to_labels": ["Node"]},
            # 정상
            {"from_id": "n1", "from_labels": ["Node"], "relation": "KNOWS",
             "rel_props": {}, "to_id": "n2", "to_labels": ["Node"]},
        ]
        driver = _make_neo4j_session(node_rows, edge_rows)

        db_path = str(tmp_path / "graph_edge_skip.db")
        local_store = LocalGraphStore(db_path=db_path)
        import logging
        result = mig.migrate_graph(driver, local_store, batch_size=100, log=logging.getLogger())

        assert result["edges"] == 1
        local_store.close()

    def test_migrate_graph_only_opencrabnode_label(self, tmp_path: Path) -> None:
        """labels가 ['OpenCrabNode'] 뿐이면 node_type='Unknown'으로 저장."""
        from opencrab.stores.local_graph_store import LocalGraphStore

        node_rows = [
            {"props": {"id": "only-ocn"}, "labels": ["OpenCrabNode"]},
        ]
        driver = _make_neo4j_session(node_rows, [])

        db_path = str(tmp_path / "graph_unknown.db")
        local_store = LocalGraphStore(db_path=db_path)
        import logging
        result = mig.migrate_graph(driver, local_store, batch_size=100, log=logging.getLogger())

        assert result["nodes"] == 1
        node = local_store.get_node("Unknown", "only-ocn")
        assert node is not None
        local_store.close()


# ---------------------------------------------------------------------------
# Test: migrate_docs — MongoDB → LocalDocStore
# ---------------------------------------------------------------------------

class TestMigrateDocs:
    def _make_mongo_db(
        self,
        nodes: list[dict],
        sources: list[dict],
        audit: list[dict],
    ) -> MagicMock:
        """pymongo db mock."""
        def _cursor(docs: list[dict]) -> MagicMock:
            c = MagicMock()
            c.__iter__ = MagicMock(return_value=iter(docs))
            c.sort = MagicMock(return_value=c)  # .sort() 체이닝
            return c

        db = MagicMock()
        db.__getitem__ = MagicMock(side_effect=lambda name: {
            "nodes":     _make_col(nodes),
            "sources":   _make_col(sources),
            "audit_log": _make_col(audit),
        }[name])
        return db

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
    def test_backup_creates_bak_files(self, tmp_path: Path) -> None:
        """기존 파일/디렉토리가 있을 때 .bak.{timestamp} 백업 생성."""
        # graph.db 생성
        graph_db = tmp_path / "graph.db"
        graph_db.write_text("fake db")
        # chroma/ 디렉토리 생성
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        (chroma_dir / "index.bin").write_text("vec")

        backed_up = mig.backup_local_data(str(tmp_path))

        # graph.db 백업 확인
        bak_files = list(tmp_path.glob("graph.db.bak.*"))
        assert len(bak_files) == 1
        assert bak_files[0].read_text() == "fake db"

        # chroma 백업 디렉토리 확인
        bak_dirs = list(tmp_path.glob("chroma.bak.*"))
        assert len(bak_dirs) == 1
        assert (bak_dirs[0] / "index.bin").exists()

    def test_backup_skips_missing_files(self, tmp_path: Path) -> None:
        """없는 파일은 조용히 스킵 (예외 없음)."""
        # tmp_path 비어 있음
        backed_up = mig.backup_local_data(str(tmp_path))
        # 백업 없음 (경고만)
        assert backed_up == {}

    def test_backup_creates_timestamped_name(self, tmp_path: Path) -> None:
        """백업 파일명에 타임스탬프 포함."""
        graph_db = tmp_path / "graph.db"
        graph_db.write_text("x")

        backed_up = mig.backup_local_data(str(tmp_path))
        bak_files = list(tmp_path.glob("graph.db.bak.*"))
        assert len(bak_files) == 1
        # 타임스탬프 형식: YYYYMMDD_HHMMSS
        bak_name = bak_files[0].name
        suffix = bak_name.replace("graph.db.bak.", "")
        assert len(suffix) == 15  # 20240101_120000
        assert "_" in suffix


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
