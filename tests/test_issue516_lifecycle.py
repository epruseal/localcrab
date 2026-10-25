"""#516 G4: pack deletion removes source, integrity helper and FTS rows together.

The cases run the real ``delete_pack`` and ``incremental_finalize`` against a
real LocalSQLDocStore. The helper rows are written by the real store.
"""

from __future__ import annotations

import pytest

from opencrab.auth import principal_scope
from opencrab.pack import load as pack_load
from tests import test_pack_delete_gate as _gate

# The environment fixture builds registry, graph and doc stores in a temp dir.
env = _gate.env


def _rows(docs, table: str, source_id: str) -> list[tuple]:
    column = "source_id"
    return [
        tuple(row)
        for row in docs._conn.execute(
            f"SELECT {column} FROM {table} WHERE {column}=?", (source_id,)
        ).fetchall()
    ]


def _present(docs, source_id: str) -> dict[str, bool]:
    return {
        "source": bool(_rows(docs, "doc_sources", source_id)),
        "helper": bool(_rows(docs, "doc_source_integrity", source_id)),
        "fts": bool(_rows(docs, "doc_sources_fts", source_id)),
    }


@pytest.fixture
def seeded(env):
    sql, graph, docs, log, calls, tmp = env
    if not docs._fts_ok:
        pytest.skip("SQLite FTS5 is not available in this build")
    _gate._seed(sql, graph, docs, tmp, "pk")
    docs.upsert_source("pk-keep", "keep body", {"pack_id": "pk"})
    docs.upsert_source("pk-gone-1", "first gone body", {"pack_id": "pk"})
    docs.upsert_source("pk-gone-2", "second gone body", {"pack_id": "pk"})
    docs.upsert_source("other-s1", "other pack body", {"pack_id": "other-pack"})
    for source_id in ("pk-keep", "pk-gone-1", "pk-gone-2", "other-s1"):
        assert _present(docs, source_id) == {"source": True, "helper": True, "fts": True}
    return sql, graph, docs


def test_delete_pack_removes_source_helper_and_fts_together(seeded):
    sql, graph, docs = seeded

    with principal_scope(_gate._principal(_gate.OWNER)):
        pack_load.delete_pack("pk", graph, docs, _gate._NoVec(), sql=sql)

    for source_id in ("pk-keep", "pk-gone-1", "pk-gone-2"):
        assert _present(docs, source_id) == {"source": False, "helper": False, "fts": False}, source_id
    # Another pack keeps all three rows.
    assert _present(docs, "other-s1") == {"source": True, "helper": True, "fts": True}


def test_incremental_finalize_chunk_delete_removes_helper_and_fts(seeded):
    sql, graph, docs = seeded
    live = {
        "nodes": {},
        "chunks": {"pk-keep": ("keep body", {}), "pk-gone-1": ("first gone body", {})},
        "edges": set(),
        "vec_ids": set(),
        "doc_node_spaces": {},
    }

    with principal_scope(_gate._principal(_gate.OWNER)):
        result = pack_load.incremental_finalize(
            "pk", graph, docs, _gate._NoVec(), live,
            {"n0"}, {"pk-keep"}, set(), True, 1, 1, sql=sql,
        )

    assert result["chunk_del"] == 1, result
    assert _present(docs, "pk-gone-1") == {"source": False, "helper": False, "fts": False}
    assert _present(docs, "pk-keep") == {"source": True, "helper": True, "fts": True}
    assert _present(docs, "other-s1") == {"source": True, "helper": True, "fts": True}


def test_delete_pack_progress_line_names_only_counted_axes(seeded, capsys):
    """#516 G4: the progress line gives the source count that the store returned.
    It carries no FTS count, because the delete boundary does not count FTS rows."""
    sql, graph, docs = seeded

    with principal_scope(_gate._principal(_gate.OWNER)):
        pack_load.delete_pack("pk", graph, docs, _gate._NoVec(), sql=sql)

    lines = [
        line for line in capsys.readouterr().out.splitlines()
        if "삭제:" in line and "doc_sources" in line
    ]
    assert len(lines) == 1, lines
    assert "doc_sources 3개" in lines[0], lines[0]
    assert "fts" not in lines[0].lower(), lines[0]
