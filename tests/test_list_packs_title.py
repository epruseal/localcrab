"""list_packs() sample_title 우선순위 검증.

anchor 노드(node_id="dataset:{pack_id}") title > source_package_title > 빈 문자열
임의 노드의 title/name은 반환하지 않는다.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# LocalGraphStore
# ---------------------------------------------------------------------------

@pytest.fixture
def local_store(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore
    s = LocalGraphStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_local_list_packs_uses_anchor_title(local_store):
    local_store.upsert_node("Dataset", "dataset:mypkg", {"pack_id": "mypkg", "title": "My Package"})
    local_store.upsert_node("Thing", "t1", {"pack_id": "mypkg", "name": "some thing"})

    rows = local_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "mypkg")
    assert row["sample_title"] == "My Package"


def test_local_list_packs_anchor_beats_other_titles(local_store):
    local_store.upsert_node("Dataset", "dataset:pkg", {"pack_id": "pkg", "title": "Correct Title"})
    # 다른 노드들도 title/name 보유
    local_store.upsert_node("Part", "p1", {"pack_id": "pkg", "title": "ZZZZZ Wrong Part Title"})
    local_store.upsert_node("Doc", "d1", {"pack_id": "pkg", "name": "AAAA Wrong Doc Name"})

    rows = local_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "pkg")
    assert row["sample_title"] == "Correct Title"


def test_local_list_packs_falls_back_to_source_package_title(local_store):
    # anchor 없음, source_package_title 만 있음
    local_store.upsert_node("Thing", "t1", {
        "pack_id": "extpkg",
        "source_package_title": "External Pack",
        "title": "Random Node Title",
    })

    rows = local_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "extpkg")
    assert row["sample_title"] == "External Pack"


def test_local_list_packs_empty_when_no_anchor_no_pkg_title(local_store):
    # anchor도 source_package_title도 없음 — 임의 노드 라벨 노출 방지
    local_store.upsert_node("Thing", "t1", {"pack_id": "bare", "title": "Should Not Appear"})
    local_store.upsert_node("Thing", "t2", {"pack_id": "bare", "name": "Also Should Not"})

    rows = local_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "bare")
    assert row["sample_title"] == ""


# ---------------------------------------------------------------------------
# KuzuGraphStore
# ---------------------------------------------------------------------------

@pytest.fixture
def kuzu_store(tmp_path):
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore
    s = KuzuGraphStore(db_path=str(tmp_path / "test_kuzu"))
    yield s
    s.close()


def test_kuzu_list_packs_uses_anchor_title(kuzu_store):
    kuzu_store.upsert_node("Dataset", "dataset:mypkg", {"pack_id": "mypkg", "title": "My Package"})
    kuzu_store.upsert_node("Thing", "t1", {"pack_id": "mypkg", "name": "some thing"})

    rows = kuzu_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "mypkg")
    assert row["sample_title"] == "My Package"


def test_kuzu_list_packs_anchor_beats_other_titles(kuzu_store):
    kuzu_store.upsert_node("Dataset", "dataset:pkg", {"pack_id": "pkg", "title": "Correct Title"})
    kuzu_store.upsert_node("Part", "p1", {"pack_id": "pkg", "title": "ZZZZZ Wrong Part Title"})
    kuzu_store.upsert_node("Doc", "d1", {"pack_id": "pkg", "name": "AAAA Wrong Doc Name"})

    rows = kuzu_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "pkg")
    assert row["sample_title"] == "Correct Title"


def test_kuzu_list_packs_falls_back_to_source_package_title(kuzu_store):
    kuzu_store.upsert_node("Thing", "t1", {
        "pack_id": "extpkg",
        "source_package_title": "External Pack",
        "title": "Random Node Title",
    })

    rows = kuzu_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "extpkg")
    assert row["sample_title"] == "External Pack"


def test_kuzu_list_packs_empty_when_no_anchor_no_pkg_title(kuzu_store):
    kuzu_store.upsert_node("Thing", "t1", {"pack_id": "bare", "title": "Should Not Appear"})
    kuzu_store.upsert_node("Thing", "t2", {"pack_id": "bare", "name": "Also Should Not"})

    rows = kuzu_store.list_packs(min_nodes=1)
    row = next(r for r in rows if r["pack_id"] == "bare")
    assert row["sample_title"] == ""
