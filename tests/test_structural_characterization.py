"""구조적 중복 통합 리팩토링의 회귀 안전망 — 특성화(characterization) 테스트.

목적: "이상적 동작"이 아니라 **현재 코드의 실제 출력을 그대로 박제**한다.
곧 진행할 "구조적 중복 통합" 리팩토링이 동작을 바꾸면 이 테스트가 깨져야 한다.

대상:
1. Neo4j 행 정규화 — 거의 동일한 두 구현(opencrab.pack.neo4j_export vs
   scripts/export_pack_graph_from_neo4j)의 **미묘한 차이를 각각 별도로 박제**.
2. codex_workers validate_bundle — 3워커(landscape/github_trending/soeak)의 verdict 로직.
3. Neo4j 드라이버 초기화 — GraphDatabase.driver(...) 에 전달되는 인자
   (uri/auth/옵션)를 mock으로 박제. 실제 연결은 차단.

모든 값은 실제로 함수를 호출해 확인한 뒤 박았다(추측 없음).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CRABHARNESS_DIR = REPO_ROOT / "crabharness"

# crabharness 패키지(crabharness, codex_workers)는 crabharness/ 디렉토리를
# sys.path 에 추가해야 import 된다. codex_workers 는 crabharness 패키지 안이
# 아니라 형제 디렉토리이므로 `crabharness.codex_workers` 가 아닌
# `codex_workers` 로 import 한다.
if str(CRABHARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(CRABHARNESS_DIR))

# scripts/ 는 패키지가 아니므로 migrate_to_local 직접 import 를 위해 경로 추가
# (단독 실행 시에도 import 되도록 보장).
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module_from_path(name: str, relpath: str):
    """scripts/ 처럼 패키지가 아닌 모듈을 파일 경로로 직접 로드한다."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# 대상 1: Neo4j 행 정규화 — 두 구현
# ===========================================================================

from opencrab.pack.neo4j_export import (  # noqa: E402
    _clean_props,
    _normalise_edge,
    _normalise_node,
    _sha_id,
    _stable_json,
)

# scripts/export_pack_graph_from_neo4j.py 는 argparse top-level 실행이 없고
# (main() 안에서만 parse), top-level 부작용은 `from neo4j import GraphDatabase`
# 뿐이므로 importlib 로 안전하게 로드 가능.
_expg = _load_module_from_path("expg_char", "scripts/export_pack_graph_from_neo4j.py")


# ---------------------------------------------------------------------------
# 1a. _stable_json / jdump — 두 구현은 동일 출력 (sort_keys, ensure_ascii=False)
# ---------------------------------------------------------------------------

def test_stable_json_sorts_keys_and_keeps_unicode():
    value = {"b": 1, "a": 2, "z": [3, 2, 1], "k": "한글"}
    expected = '{"a": 2, "b": 1, "k": "한글", "z": [3, 2, 1]}'
    assert _stable_json(value) == expected


def test_jdump_matches_stable_json():
    # 박제: 두 직렬화 구현은 현재 비트단위로 동일하다.
    value = {"b": 1, "a": 2, "z": [3, 2, 1], "k": "한글"}
    assert _expg.jdump(value) == _stable_json(value)


def test_stable_json_uses_default_str_for_nonserializable():
    class Weird:
        def __str__(self) -> str:
            return "WEIRD"

    assert _stable_json({"x": Weird()}) == '{"x": "WEIRD"}'
    assert _expg.jdump({"x": Weird()}) == '{"x": "WEIRD"}'


# ---------------------------------------------------------------------------
# 1b. _sha_id / sha_id — 동일 알고리즘 (sha256 hexdigest[:16])
# ---------------------------------------------------------------------------

def test_sha_id_format_and_value():
    value = {"b": 1, "a": 2, "z": [3, 2, 1]}
    assert _sha_id("neo4j-node", value) == "neo4j-node:eefcd84b3571238c"


def test_sha_id_matches_between_implementations():
    value = {"b": 1, "a": 2, "z": [3, 2, 1]}
    assert _expg.sha_id("neo4j-node", value) == _sha_id("neo4j-node", value)


# ---------------------------------------------------------------------------
# 1c. _clean_props vs clean_props — 핵심 차이: identity vs 복사본
# ---------------------------------------------------------------------------

def test_clean_props_returns_same_object_identity():
    # opencrab._clean_props 는 dict 를 그대로(동일 객체) 반환한다.
    src = {"x": 1}
    assert _clean_props(src) is src


def test_script_clean_props_returns_shallow_copy():
    # scripts.clean_props 는 dict(value) 로 얕은 복사본을 반환한다 (identity != src).
    src = {"x": 1}
    out = _expg.clean_props(src)
    assert out is not src
    assert out == {"x": 1}


@pytest.mark.parametrize("bad", [[1, 2], None, "str", 42])
def test_both_clean_props_coerce_nondict_to_empty(bad):
    assert _clean_props(bad) == {}
    assert _expg.clean_props(bad) == {}


# ---------------------------------------------------------------------------
# 1d. _normalise_node vs normalise_node
#     핵심 차이 박제:
#       - opencrab: row.get("props") (graceful), space 추론 없음(LABEL_TO_SPACE 미사용),
#         ontology_space/type/evidence_ids fallback 지원, node_type=labels[0]
#       - script: record["props"] (KeyError on missing), LABEL_TO_SPACE 로 space 추론,
#         node_type 은 LABELS 우선순위, ontology_space/evidence_ids fallback 없음
# ---------------------------------------------------------------------------

def test_normalise_node_opencrab_full_row():
    row = {"props": {"id": "n1", "name": "Doc A", "node_type": None}, "labels": ["Document", "Thing"]}
    assert _normalise_node(row) == {
        "kind": "node",
        "payload": {
            "id": "n1",
            "label": "Doc A",
            "space": "",  # opencrab 은 LABEL_TO_SPACE 를 쓰지 않아 빈 문자열
            "node_type": "Document",  # labels[0]
            "labels": ["Document", "Thing"],
            "properties": {"id": "n1", "name": "Doc A", "node_type": None},
            "evidence_refs": [],
        },
    }


def test_normalise_node_script_full_row_uses_label_to_space():
    row = {"props": {"id": "n1", "name": "Doc A", "node_type": None}, "labels": ["Document", "Thing"]}
    assert _expg.normalise_node(row) == {
        "kind": "node",
        "payload": {
            "id": "n1",
            "label": "Doc A",
            "space": "resource",  # LABEL_TO_SPACE["Document"]
            "node_type": "Document",
            "labels": ["Document", "Thing"],
            "properties": {"id": "n1", "name": "Doc A", "node_type": None},
            "evidence_refs": [],
        },
    }


def test_normalise_node_space_difference_persona():
    # 동일 입력에서 space 결과가 갈린다: opencrab "" vs script "subject".
    row = {"props": {"id": "n2", "label": "Persona X"}, "labels": ["Persona"]}
    assert _normalise_node(row)["payload"]["space"] == ""
    assert _expg.normalise_node(row)["payload"]["space"] == "subject"


def test_normalise_node_opencrab_ontology_space_and_type_fallback():
    # opencrab 만 ontology_space / props["type"] fallback 을 지원한다.
    row = {"props": {"id": "n3", "ontology_space": "subject", "type": "Custom"}, "labels": []}
    payload = _normalise_node(row)["payload"]
    assert payload["space"] == "subject"
    assert payload["node_type"] == "Custom"


def test_normalise_node_script_ignores_ontology_space_and_type():
    # script 는 ontology_space 도 props["type"] 도 보지 않는다.
    row = {"props": {"id": "n3", "ontology_space": "subject", "type": "Custom"}, "labels": []}
    payload = _expg.normalise_node(row)["payload"]
    assert payload["space"] == ""
    assert payload["node_type"] == ""


def test_normalise_node_evidence_refs_fallback_difference():
    # opencrab 은 evidence_ids -> evidence_refs fallback, script 는 evidence_refs 만.
    row = {"props": {"id": "n4", "evidence_ids": ["e1"]}, "labels": ["Evidence"]}
    assert _normalise_node(row)["payload"]["evidence_refs"] == ["e1"]
    assert _expg.normalise_node(row)["payload"]["evidence_refs"] == []


def test_normalise_node_node_type_priority_difference():
    # labels=["Foo","Evidence"]: opencrab=labels[0]="Foo",
    # script=LABELS 우선순위로 "Evidence".
    row = {"props": {"id": "n6"}, "labels": ["Foo", "Evidence"]}
    assert _normalise_node(row)["payload"]["node_type"] == "Foo"
    assert _expg.normalise_node(row)["payload"]["node_type"] == "Evidence"


def test_normalise_node_no_id_uses_sha_fallback_both():
    # id 부재 시 둘 다 sha 기반 id (동일 값).
    row = {"props": {"name": "anon"}, "labels": ["Persona"]}
    nid_oc = _normalise_node(row)["payload"]["id"]
    nid_sc = _expg.normalise_node(row)["payload"]["id"]
    assert nid_oc == "neo4j-node:2aee2ff09778d50e"
    assert nid_sc == nid_oc


def test_normalise_node_missing_props_key_difference():
    # 에러 동작 차이: opencrab 은 graceful(빈 dict 처리), script 는 KeyError.
    assert _normalise_node({}) == {
        "kind": "node",
        "payload": {
            "id": "neo4j-node:44136fa355b3678a",
            "label": "neo4j-node:44136fa355b3678a",
            "space": "",
            "node_type": "",
            "labels": [],
            "properties": {},
            "evidence_refs": [],
        },
    }
    with pytest.raises(KeyError):
        _expg.normalise_node({})


# ---------------------------------------------------------------------------
# 1e. _normalise_edge vs normalise_edge
#     핵심 차이 박제:
#       - opencrab: from_id/to_id = _node_id(props) (id/node_id/uuid -> sha),
#         relation = row.relation or rel_props.relation, .get() graceful,
#         space 추론 없음
#       - script: from_id = source_props.id or rel_props.from_id,
#         relation = str(record["relation"]).lower() (None -> "none"),
#         record["rel_props"] KeyError on missing, LABEL_TO_SPACE 로 space 추론
# ---------------------------------------------------------------------------

def test_normalise_edge_full_row_identical_when_ids_and_spaces_present():
    # source/target 에 id+space 가 다 있으면 두 구현이 동일 결과를 낸다(현 상태 박제).
    erow = {
        "source_props": {"id": "s1", "space": "concept"},
        "target_props": {"id": "t1", "space": "policy"},
        "rel_props": {"confidence": 0.9, "evidence_refs": ["e1"]},
        "relation": "CONSTRAINS",
        "source_labels": ["Entity"],
        "target_labels": ["Policy"],
    }
    expected = {
        "kind": "edge",
        "payload": {
            "from_id": "s1",
            "to_id": "t1",
            "from_space": "concept",
            "to_space": "policy",
            "relation": "constrains",
            "properties": {"confidence": 0.9, "evidence_refs": ["e1"]},
            "confidence": 0.9,
            "evidence_refs": ["e1"],
            "source_labels": ["Entity"],
            "target_labels": ["Policy"],
            "id": "neo4j-edge:d02587770989d957",
        },
    }
    assert _normalise_edge(erow) == expected
    assert _expg.normalise_edge(erow) == expected


def test_normalise_edge_from_to_id_fallback_difference():
    # id 부재 시: opencrab=sha 기반 node id, script=rel_props.from_id/to_id.
    erow = {
        "source_props": {"name": "no-id-src"},
        "target_props": {"name": "no-id-tgt"},
        "rel_props": {"from_id": "RID_FROM", "to_id": "RID_TO"},
        "relation": "LINKS",
        "source_labels": ["Document"],
        "target_labels": ["Persona"],
    }
    oc = _normalise_edge(erow)["payload"]
    sc = _expg.normalise_edge(erow)["payload"]
    assert oc["from_id"] == "neo4j-node:60955656f4b558aa"
    assert oc["to_id"] == "neo4j-node:a81ae281631a8e87"
    assert sc["from_id"] == "RID_FROM"
    assert sc["to_id"] == "RID_TO"


def test_normalise_edge_relation_none_difference():
    # relation=None: opencrab 은 rel_props.relation fallback("fallback"),
    # script 는 str(None).lower()="none".
    erow = {
        "source_props": {"id": "s"},
        "target_props": {"id": "t"},
        "rel_props": {"relation": "FALLBACK"},
        "relation": None,
        "source_labels": [],
        "target_labels": [],
    }
    assert _normalise_edge(erow)["payload"]["relation"] == "fallback"
    assert _expg.normalise_edge(erow)["payload"]["relation"] == "none"


def test_normalise_edge_space_inference_difference():
    # space 부재 시: opencrab="" (추론 없음), script=LABEL_TO_SPACE 추론.
    erow = {
        "source_props": {"id": "s"},
        "target_props": {"id": "t"},
        "rel_props": {},
        "relation": "R",
        "source_labels": ["Document"],
        "target_labels": ["Persona"],
    }
    oc = _normalise_edge(erow)["payload"]
    sc = _expg.normalise_edge(erow)["payload"]
    assert oc["from_space"] == "" and oc["to_space"] == ""
    assert sc["from_space"] == "resource" and sc["to_space"] == "subject"


def test_normalise_edge_id_from_rel_props_id():
    # rel_props["id"] 존재 시 둘 다 그 값을 쓴다 (opencrab 은 _edge_id 가
    # payload["properties"]=rel_props 에서 id 를 찾기 때문, script 는 명시적으로).
    erow = {
        "source_props": {"id": "s"},
        "target_props": {"id": "t"},
        "rel_props": {"id": "REL_ID_X"},
        "relation": "R",
        "source_labels": [],
        "target_labels": [],
    }
    assert _normalise_edge(erow)["payload"]["id"] == "REL_ID_X"
    assert _expg.normalise_edge(erow)["payload"]["id"] == "REL_ID_X"


def test_normalise_edge_missing_keys_difference():
    # 에러 동작 차이: opencrab graceful, script KeyError('rel_props').
    payload = _normalise_edge({})["payload"]
    assert payload["relation"] == ""
    assert payload["from_space"] == "" and payload["to_space"] == ""
    assert payload["confidence"] is None
    assert payload["evidence_refs"] == []
    with pytest.raises(KeyError):
        _expg.normalise_edge({})


# ===========================================================================
# 대상 2: codex_workers validate_bundle (3 워커)
#
# semantic 스코어링: ANTHROPIC_API_KEY 가 없으면 heuristic fallback 경로로
# 결정적 동작한다. semantic_questions 가 비어 있으면 heuristic 은 0.5 를 반환.
# 결정성 보장을 위해 monkeypatch 로 ANTHROPIC_API_KEY 를 제거한다.
# ===========================================================================

from crabharness.models import (  # noqa: E402
    ArtifactBundle,
    MissionSpec,
    MissionSuccessCriteria,
)
from codex_workers.github_trending.adapter import validate_bundle as gh_validate  # noqa: E402
from codex_workers.landscape.adapter import validate_bundle as ls_validate  # noqa: E402
from codex_workers.soeak.adapter import validate_soeak_bundle as so_validate  # noqa: E402


@pytest.fixture(autouse=True)
def _no_anthropic_key(monkeypatch):
    # semantic 경로를 결정적인 heuristic fallback 으로 고정한다.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _mission(required_fields=None, threshold=0.8, min_semantic=0.0):
    return MissionSpec(
        mission_id="m",
        objective="o",
        target_object="t",
        success_criteria=MissionSuccessCriteria(
            required_fields=required_fields if required_fields is not None else [],
            completeness_threshold=threshold,
            min_semantic_score=min_semantic,
        ),
    )


def _bundle(summary):
    return ArtifactBundle(run_id="r", mission_id="m", worker_id="w", job_id="j", summary=summary)


# ---------------------------------------------------------------------------
# 2a. github_trending.validate_bundle
# ---------------------------------------------------------------------------

def test_gh_validate_full_pass():
    r = gh_validate(_bundle({"repos_count": 5}), _mission(["repos"]))
    assert (r.status, r.completeness_score, r.semantic_score) == ("pass", 1.0, 0.5)
    assert r.semantic_verdict == "keep"
    assert r.next_action == "promote"
    assert r.issues == []


def test_gh_validate_empty_bundle_fails():
    # repos_count=0 -> passed=0 & issues -> fail/reject.
    r = gh_validate(_bundle({"repos_count": 0}), _mission(["repos"]))
    assert (r.status, r.completeness_score, r.next_action) == ("fail", 0.0, "reject")
    assert r.semantic_verdict == "discard"
    assert [i.code for i in r.issues] == ["missing_repos"]


def test_gh_validate_partial_completeness_retry():
    # required=["repos","stars"]: repos ok, stars(미정의 필드) 부재 -> 0.5 < 0.8 -> retry.
    r = gh_validate(_bundle({"repos_count": 5}), _mission(["repos", "stars"]))
    assert (r.status, r.completeness_score, r.next_action) == ("retry", 0.5, "retry")
    assert r.semantic_verdict == "discard"
    assert [i.code for i in r.issues] == ["missing_stars"]


def test_gh_validate_empty_required_fields_defaults_to_repos():
    # required_fields=[] -> ["repos"] fallback.
    r = gh_validate(_bundle({"repos_count": 5}), _mission([]))
    assert (r.status, r.completeness_score) == ("pass", 1.0)


# ---------------------------------------------------------------------------
# 2b. soeak.validate_soeak_bundle
# ---------------------------------------------------------------------------

def test_soeak_validate_full_pass():
    bundle = _bundle(
        {
            "bidders_count": 3,
            "reserve_price_count": 2,
            "db_exists": True,
            "case": {"winner_rate": 0.9},
            "progress": {},
        }
    )
    r = so_validate(bundle, _mission(["bidders", "reserve_prices"]))
    assert (r.status, r.completeness_score, r.semantic_score) == ("pass", 1.0, 0.5)
    assert (r.semantic_verdict, r.next_action) == ("keep", "promote")
    assert r.issues == []


def test_soeak_validate_db_missing_and_zero_counts_fails():
    bundle = _bundle({"bidders_count": 0, "reserve_price_count": 0, "db_exists": False})
    r = so_validate(bundle, _mission(["bidders", "reserve_prices"]))
    assert (r.status, r.completeness_score, r.next_action) == ("fail", 0.0, "reject")
    assert [i.code for i in r.issues] == ["missing_bidders", "missing_reserve_prices", "missing_db"]


def test_soeak_validate_fatal_progress_forces_retry():
    # completeness=1.0 이지만 fatal_progress error issue -> status retry(pass 아님).
    bundle = _bundle(
        {
            "bidders_count": 3,
            "reserve_price_count": 2,
            "db_exists": True,
            "progress": {"fatal": True},
        }
    )
    r = so_validate(bundle, _mission(["bidders", "reserve_prices"]))
    assert (r.status, r.completeness_score, r.next_action) == ("retry", 1.0, "retry")
    assert r.semantic_verdict == "keep"  # verdict 는 completeness/semantic 기준이라 keep
    assert [i.code for i in r.issues] == ["fatal_progress"]


def test_soeak_validate_empty_required_fields_zero_completeness():
    # soeak 는 landscape/gh 와 달리 required_fields 에 'or' fallback 이 없다.
    # required=[] -> 루프 미실행 -> passed=0, checks_total=1 -> completeness 0.0.
    # passed==0 이지만 issues 가 비어 fail 조건(passed==0 and issues) 미충족 -> retry.
    bundle = _bundle({"bidders_count": 3, "reserve_price_count": 2, "db_exists": True})
    r = so_validate(bundle, _mission([]))
    assert (r.status, r.completeness_score, r.next_action) == ("retry", 0.0, "retry")
    assert r.semantic_verdict == "discard"
    assert r.issues == []


def test_soeak_validate_custom_case_field_present():
    # 'bidders'/'reserve_prices' 외 필드는 case dict 에서 조회.
    bundle = _bundle({"db_exists": True, "case": {"winner_rate": 0.5}})
    r = so_validate(bundle, _mission(["winner_rate"]))
    assert (r.status, r.completeness_score, r.next_action) == ("pass", 1.0, "promote")
    assert r.issues == []


# ---------------------------------------------------------------------------
# 2c. landscape.validate_bundle
#     semantic_score = max(heuristic, _domain_semantic_score) 이므로 도메인
#     스코어가 결정적으로 우세할 수 있다. full-pass 케이스의 0.613 박제.
# ---------------------------------------------------------------------------

def _landscape_full_summary():
    return {
        "documents": [{"x": 1}],
        "use_cases": [
            {
                "title": "u",
                "statement": "s",
                "category": "construction_ai",
                "publisher": "P",
                "outcomes": ["o1"],
                "capabilities": ["c1"],
            }
        ],
        "categories": ["construction_ai", "landscape_ai"],
        "publishers": ["P"],
    }


def test_landscape_validate_full_pass_with_domain_semantic_score():
    r = ls_validate(_bundle(_landscape_full_summary()), _mission(threshold=0.8))
    assert (r.status, r.completeness_score) == ("pass", 1.0)
    # _domain_semantic_score: 0.2 + 0.3 + (1/4)*0.2 + (1/6)*0.15 + (1/4)*0.15 = 0.6125 -> 0.613
    assert r.semantic_score == 0.613
    assert (r.semantic_verdict, r.next_action) == ("keep", "promote")
    assert r.issues == []


def test_landscape_validate_empty_bundle_fails():
    # documents/use_cases 둘 다 비면 status=fail, next=reject (특수 분기).
    r = ls_validate(_bundle({"documents": [], "use_cases": [], "categories": []}), _mission(threshold=0.8))
    assert (r.status, r.completeness_score, r.next_action) == ("fail", 0.0, "reject")
    assert r.semantic_score == 0.5  # heuristic (no questions), domain score 0 -> max=0.5
    assert r.semantic_verdict == "discard"
    assert [i.code for i in r.issues] == [
        "missing_source_documents",
        "missing_use_cases",
        "insufficient_category_coverage",
    ]


def test_landscape_validate_single_category_warning_still_passes():
    # docs+use_cases 있고 카테고리 1개 -> warning issue 만 -> error 아님 -> pass.
    summary = {
        "documents": [{"x": 1}],
        "use_cases": [{"category": "construction_ai"}],
        "categories": ["construction_ai"],
        "publishers": [],
    }
    r = ls_validate(_bundle(summary), _mission(threshold=0.8))
    assert (r.status, r.completeness_score, r.next_action) == ("pass", 1.0, "promote")
    assert r.semantic_score == 0.5
    assert [i.code for i in r.issues] == ["insufficient_category_coverage"]


def test_landscape_validate_min_semantic_gate_blocks_pass():
    # min_semantic_score=0.9 > 0.613 -> status retry, verdict discard.
    r = ls_validate(_bundle(_landscape_full_summary()), _mission(threshold=0.8, min_semantic=0.9))
    assert (r.status, r.completeness_score, r.semantic_score) == ("retry", 1.0, 0.613)
    assert (r.semantic_verdict, r.next_action) == ("discard", "retry")


# ===========================================================================
# 대상 3: Neo4j 드라이버 초기화 — GraphDatabase.driver(...) 인자 박제
#
# 실제 서버 연결은 불가하므로 driver 생성 부분만 mock 하여 전달 인자
# (uri, auth 튜플, 옵션)를 박제한다. 연결/세션은 MagicMock 으로 차단.
# ===========================================================================

def _make_capturing_driver(capture: dict) -> MagicMock:
    """driver() 호출 인자를 capture 에 기록하고 연결/세션을 mock 하는 fake."""

    def fake_driver(uri, **kwargs):
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        driver = MagicMock()
        driver.__enter__.return_value = driver
        driver.__exit__.return_value = False
        driver.verify_connectivity.return_value = None
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        # export/import 의 session.run(...) 은 iterable 이거나 .data()/.single() 호출됨.
        session.run.return_value = iter([])
        driver.session.return_value = session
        return driver

    gd = MagicMock()
    gd.driver.side_effect = fake_driver
    return gd


# ---------------------------------------------------------------------------
# 3a. opencrab.stores.neo4j_store.Neo4jStore — 함수 내부 `from neo4j import
#     GraphDatabase` 이므로 neo4j.GraphDatabase 를 patch 한다.
# ---------------------------------------------------------------------------

def test_neo4j_store_driver_args_no_database():
    import neo4j

    import opencrab.stores.neo4j_store as ns

    capture: dict = {}

    def fake_driver(uri, **kwargs):
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        driver = MagicMock()
        sess = driver.session.return_value.__enter__.return_value
        sess.run.return_value = None
        return driver

    with patch.object(neo4j, "GraphDatabase") as gd:
        gd.driver.side_effect = fake_driver
        store = ns.Neo4jStore("bolt://h:7687", "neo4j", "pw")

    assert store.available is True
    assert capture["uri"] == "bolt://h:7687"
    assert capture["kwargs"] == {"auth": ("neo4j", "pw")}


def test_neo4j_store_driver_args_with_database():
    # database 지정 시 driver 인자 자체는 동일(database 는 session() 에서 사용).
    import neo4j

    import opencrab.stores.neo4j_store as ns

    capture: dict = {}

    def fake_driver(uri, **kwargs):
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        driver = MagicMock()
        sess = driver.session.return_value.__enter__.return_value
        sess.run.return_value = None
        return driver

    with patch.object(neo4j, "GraphDatabase") as gd:
        gd.driver.side_effect = fake_driver
        store = ns.Neo4jStore("bolt://h:7687", "neo4j", "pw", database="db1")

    assert store.available is True
    assert capture["uri"] == "bolt://h:7687"
    assert capture["kwargs"] == {"auth": ("neo4j", "pw")}
    # database 는 session(**{"database": "db1"}) 로 전달된다.
    store._driver.session.assert_called_with(database="db1")


# ---------------------------------------------------------------------------
# 3b. scripts/export_pack_graph_from_neo4j.py:main() — top-level
#     `from neo4j import GraphDatabase` 이므로 모듈 속성 GraphDatabase 를 patch.
# ---------------------------------------------------------------------------

def test_export_script_driver_args(tmp_path):
    output = tmp_path / "out.jsonl"
    capture: dict = {}
    argv = [
        "prog",
        "--output", str(output),
        "--uri", "bolt://eh:7687",
        "--user", "u1",
        "--password", "p1",
        "--fetch-size", "99",
    ]
    fake_gd = _make_capturing_driver(capture)
    with patch.object(_expg, "GraphDatabase", fake_gd), patch.object(sys, "argv", argv):
        rc = _expg.main()

    assert rc == 0
    assert capture["uri"] == "bolt://eh:7687"
    assert capture["kwargs"] == {
        "auth": ("u1", "p1"),
        "fetch_size": 99,
        "max_connection_lifetime": 3600,
    }


# ---------------------------------------------------------------------------
# 3c. scripts/import_pack_graph_to_neo4j.py:main() — top-level import.
#     --validate-only 로 import 경로를 건너뛰고 driver 인자만 박제.
# ---------------------------------------------------------------------------

# import_pack 모듈도 top-level 부작용이 `from neo4j import GraphDatabase` 뿐이라
# importlib 로 로드 가능.
_imp_pkg = _load_module_from_path("imp_pkg_char", "scripts/import_pack_graph_to_neo4j.py")


def test_import_script_driver_args(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    capture: dict = {}
    argv = [
        "prog",
        "--stage", str(stage),
        "--uri", "bolt://imp:7687",
        "--user", "iu",
        "--password", "ip",
        "--batch-size", "55",
        "--validate-only",
    ]
    fake_gd = _make_capturing_driver(capture)

    def fake_driver(uri, **kwargs):
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        driver = MagicMock()
        driver.__enter__.return_value = driver
        driver.__exit__.return_value = False
        sess = MagicMock()
        sess.__enter__.return_value = sess
        sess.__exit__.return_value = False
        driver.session.return_value = sess
        return driver

    fake_gd.driver.side_effect = fake_driver
    with patch.object(_imp_pkg, "GraphDatabase", fake_gd), patch.object(sys, "argv", argv):
        rc = _imp_pkg.main()

    assert rc == 0
    assert capture["uri"] == "bolt://imp:7687"
    assert capture["kwargs"] == {
        "auth": ("iu", "ip"),
        "max_connection_lifetime": 3600,
    }


# ---------------------------------------------------------------------------
# 3d. scripts/migrate_to_local.py:preflight() — 함수 내부 `from neo4j import
#     GraphDatabase`. preflight 는 Neo4j 성공 후 다른 소스(Mongo/Chroma/PG)
#     연결을 시도하고, 실패가 쌓이면 sys.exit(1) 한다. 따라서 다른 소스
#     라이브러리 진입점을 차단하고 SystemExit 안에서 Neo4j driver 인자만 박제.
# ---------------------------------------------------------------------------

def test_migrate_preflight_neo4j_driver_args():
    import chromadb
    import neo4j
    import pymongo
    import sqlalchemy

    import migrate_to_local as mig  # scripts/ 는 test_migrate_to_local 가 sys.path 추가

    capture: dict = {}

    def fake_driver(uri, **kwargs):
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        driver = MagicMock()
        sess = driver.session.return_value.__enter__.return_value
        sess.run.return_value.consume.return_value = None
        sess.run.return_value.single.return_value = {"c": 42}
        return driver

    def blocked(*args, **kwargs):
        raise RuntimeError("source connection blocked in test")

    args = argparse.Namespace(
        neo4j_uri="bolt://mig:7687",
        neo4j_user="mu",
        neo4j_pass="mp",
        mongo_uri="x",
        mongo_db="d",
        chroma_host="x",
        chroma_port=1,
        chroma_collection="c",
        pg_url="x",
    )

    with patch.object(neo4j, "GraphDatabase") as gd, \
            patch.object(pymongo, "MongoClient", side_effect=blocked), \
            patch.object(chromadb, "HttpClient", side_effect=blocked), \
            patch.object(sqlalchemy, "create_engine", side_effect=blocked):
        gd.driver.side_effect = fake_driver
        # 다른 소스가 모두 실패하므로 preflight 는 sys.exit(1) 한다.
        with pytest.raises(SystemExit) as excinfo:
            mig.preflight(args)

    assert excinfo.value.code == 1
    # 핵심: Neo4j driver 에 전달된 인자만 박제 (auth 튜플, 옵션 없음).
    assert capture["uri"] == "bolt://mig:7687"
    assert capture["kwargs"] == {"auth": ("mu", "mp")}
