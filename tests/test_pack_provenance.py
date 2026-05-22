from __future__ import annotations

from opencrab.ontology.pack_provenance import (
    infer_pack_id,
    infer_pack_id_from_path,
    matches_pack_filter,
)


def test_t4_infer_pack_id_from_path_standard_layout() -> None:
    path = "/home/asdf/.openclaw/workspace/data/localcrab/packs/test-pack/stage/README.md"
    assert infer_pack_id_from_path(path) == "test-pack"


def test_t4_infer_pack_id_from_path_packs_only() -> None:
    assert infer_pack_id_from_path("packs/abc/stage/file.md") == "abc"


def test_t4_infer_pack_id_from_path_no_match() -> None:
    assert infer_pack_id_from_path("/tmp/random/file.md") is None
    assert infer_pack_id_from_path("") is None
    assert infer_pack_id_from_path(None) is None  # type: ignore[arg-type]


def test_t5_infer_pack_id_metadata_priority() -> None:
    item = {"metadata": {"pack_id": "from-metadata"}, "properties": {"pack_id": "from-props"}}
    assert infer_pack_id(item) == "from-metadata"


def test_t5_infer_pack_id_properties_fallback() -> None:
    item = {"properties": {"pack_id": "from-props"}}
    assert infer_pack_id(item) == "from-props"


def test_t5_infer_pack_id_from_source_path() -> None:
    item = {"metadata": {"source_path": "/tmp/packs/pack-x/stage/a.md"}}
    assert infer_pack_id(item) == "pack-x"


def test_t5_infer_pack_id_from_node_id() -> None:
    item = {"node_id": "/abs/packs/pack-y/stage/node-1"}
    assert infer_pack_id(item) == "pack-y"


def test_t5_infer_pack_id_none() -> None:
    assert infer_pack_id({}) is None
    assert infer_pack_id(None) is None


def test_t5_matches_pack_filter_pass_when_no_filter() -> None:
    assert matches_pack_filter({"metadata": {}}, pack_ids=None) is True
    assert matches_pack_filter({"metadata": {}}, pack_ids=[]) is True


def test_t5_matches_pack_filter_unpackaged_strict_default() -> None:
    item = {"metadata": {}}
    assert matches_pack_filter(item, pack_ids=["A"]) is False


def test_t5_matches_pack_filter_unpackaged_opt_in() -> None:
    item = {"metadata": {}}
    assert matches_pack_filter(item, pack_ids=["A"], include_unpackaged=True) is True


def test_t5_matches_pack_filter_member_passes() -> None:
    item = {"metadata": {"pack_id": "A"}}
    assert matches_pack_filter(item, pack_ids=["A", "B"]) is True


def test_t5_matches_pack_filter_foreign_rejected() -> None:
    item = {"metadata": {"pack_id": "Z"}}
    assert matches_pack_filter(item, pack_ids=["A"], include_unpackaged=True) is False
