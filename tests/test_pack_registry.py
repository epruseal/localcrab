from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencrab.ontology.pack_registry import (
    build_candidate_registry,
    choose_packs,
    get_pack,
    load_pack_registry,
)


def _write_manifest(root: Path, pack_id: str, manifest: dict) -> Path:
    stage = root / "packs" / pack_id / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_t1_load_pack_registry_two_manifests(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "nemo-personas-v2", {
        "pack_id": "nemo-personas-v2",
        "title": "NVIDIA Nemotron Personas Korea",
        "version": "2.1.0",
        "description": "한국어 페르소나 9축 온톨로지",
        "source": {"label": "nvidia/Nemotron-Personas-Korea", "url": "https://example"},
        "counts": {"nodes": 5414, "edges": 12715},
    })
    _write_manifest(tmp_path, "unrelated-corpus", {
        "pack_id": "unrelated-corpus",
        "title": "Old construction reports",
        "version": "0.1.0",
        "description": "건설 보고서 모음",
        "counts": {"nodes": 10},
    })

    registry = load_pack_registry(tmp_path)
    pack_ids = {p.pack_id for p in registry}
    assert pack_ids == {"nemo-personas-v2", "unrelated-corpus"}

    nemo = get_pack(tmp_path, "nemo-personas-v2")
    assert nemo is not None
    assert nemo.title.startswith("NVIDIA")
    assert nemo.counts["nodes"] == 5414


def test_t1_load_pack_registry_empty(tmp_path: Path) -> None:
    assert load_pack_registry(tmp_path) == []


def test_t1_load_pack_registry_skips_missing_fields(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "lean-pack", {"pack_id": "lean-pack"})
    registry = load_pack_registry(tmp_path)
    assert len(registry) == 1
    pack = registry[0]
    assert pack.title == ""
    assert pack.counts == {}
    assert pack.keywords == []


def test_t2_choose_packs_top1_with_korean_alias(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "nvidia-nemotron-personas-korea-ontology-v2-1", {
        "pack_id": "nvidia-nemotron-personas-korea-ontology-v2-1",
        "title": "NVIDIA Nemotron Personas Korea — 9-axis stratified ontology pack",
        "description": (
            "한국어 합성 페르소나의 다양성, 분포 한계, 정책 제약, 샘플링 레버를 "
            "질의하면서도 운영 가능한 크기의 그래프를 유지하는 데 유용합니다."
        ),
        "source": {"label": "nvidia/Nemotron-Personas-Korea"},
    })
    _write_manifest(tmp_path, "ko-construction-spec", {
        "pack_id": "ko-construction-spec",
        "title": "한국 건설 시방 표준",
        "description": "방수/방염 시공 표준 문서 모음",
    })
    registry = load_pack_registry(tmp_path)
    candidates = choose_packs("네모트론 페르소나 직업 다양성", registry, limit=1)
    assert candidates, "auto-pack should pick at least one pack"
    pack, score, matched = candidates[0]
    assert pack.pack_id == "nvidia-nemotron-personas-korea-ontology-v2-1"
    assert score > 0
    assert matched


def test_t2_choose_packs_returns_empty_below_threshold(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "weather-data", {
        "pack_id": "weather-data",
        "title": "Daily weather observations",
        "description": "rainfall and humidity",
    })
    registry = load_pack_registry(tmp_path)
    assert choose_packs("페르소나 분포", registry, limit=1, min_score=50.0) == []


def test_t2_choose_packs_explicit_min_score_override(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "tiny", {"pack_id": "tiny", "title": "tiny"})
    registry = load_pack_registry(tmp_path)
    candidates = choose_packs("tiny", registry, limit=1, min_score=0.0)
    assert candidates and candidates[0][0].pack_id == "tiny"


@pytest.mark.parametrize("env_value,expected_min", [(None, 10.0), ("1", 1.0)])
def test_t2_env_min_score_default(monkeypatch, tmp_path: Path, env_value, expected_min) -> None:
    _write_manifest(tmp_path, "tiny", {"pack_id": "tiny", "title": "tiny"})
    registry = load_pack_registry(tmp_path)
    if env_value is None:
        monkeypatch.delenv("OPENCRAB_AUTO_PACK_MIN_SCORE", raising=False)
    else:
        monkeypatch.setenv("OPENCRAB_AUTO_PACK_MIN_SCORE", env_value)
    candidates = choose_packs("tiny", registry, limit=1)
    # The "tiny" pack scores 100 + 50 (pack_id + title), well above either threshold.
    assert candidates and candidates[0][1] >= expected_min


# ---------------------------------------------------------------------------
# #397: build_candidate_registry -- SQL rows are the candidate scope,
# manifests only enrich fields.
# ---------------------------------------------------------------------------


def test_build_candidate_registry_excludes_manifest_only_packs(tmp_path: Path) -> None:
    """A pack_id with a manifest but no SQL row must not enter the registry.

    The SQL packs table is the read-scope authority (#143); admitting a
    manifest-only pack here would open a path around that scope.
    """
    _write_manifest(tmp_path, "manifest-only", {
        "pack_id": "manifest-only",
        "title": "manifest only pack",
    })
    fs_packs = load_pack_registry(tmp_path)
    registry = build_candidate_registry([], fs_packs)
    assert registry == []


def test_build_candidate_registry_sql_row_with_no_manifest_still_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    """The 185-of-186 case: a SQL-registered pack with no manifest at all
    must still be scoreable -- this is the #397 root-cause fix itself."""
    sql_rows = [{"pack_id": "sql-only", "title": "quantum widget research", "description": ""}]
    registry = build_candidate_registry(sql_rows, [])
    assert len(registry) == 1
    assert registry[0].pack_id == "sql-only"
    assert registry[0].title == "quantum widget research"
    candidates = choose_packs("quantum widget research", registry, limit=1)
    assert candidates and candidates[0][0].pack_id == "sql-only"


def test_build_candidate_registry_sql_title_wins_over_manifest(tmp_path: Path) -> None:
    """pack_publish keeps the SQL row current; the manifest is a load-time
    snapshot. A non-empty SQL title/description must win."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "stale manifest title",
        "description": "stale manifest description",
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "fresh sql title", "description": "fresh sql description"}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].title == "fresh sql title"
    assert registry[0].description == "fresh sql description"


def test_build_candidate_registry_manifest_fills_empty_sql_title(tmp_path: Path) -> None:
    """When the SQL row's title/description is empty, the manifest value
    fills it instead of leaving the candidate untitled."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "manifest title",
        "description": "manifest description",
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "", "description": None}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].title == "manifest title"
    assert registry[0].description == "manifest description"


def test_build_candidate_registry_enriches_manifest_only_fields(tmp_path: Path) -> None:
    """source_label/keywords/tags have no SQL equivalent -- they must be
    carried over from the manifest whenever one exists for the pack_id."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "manifest title",
        "source": {"label": "arxiv"},
        "keywords": ["kw1", "kw2"],
        "tags": ["tag1"],
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "sql title", "description": ""}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].source_label == "arxiv"
    assert registry[0].keywords == ["kw1", "kw2"]
    assert registry[0].tags == ["tag1"]
