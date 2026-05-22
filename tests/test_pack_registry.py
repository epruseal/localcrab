from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencrab.ontology.pack_registry import (
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
