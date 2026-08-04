"""팩 생산자(opencrab.pack.build) 테스트.

이관 전 이 코드는 opencrab-dump 에 있었고 테스트가 없었다. 계약(schema)과 같은 리포로
옮겨온 목적이 "생산자와 소비자를 한 스위트에서 묶는 것"이므로, 여기서는 생산자가 계약을
지키는지와 이관에서 바뀐 두 가지(출력 루트, remap 함정 검사)를 고정한다.
"""

import json
import os

import pytest

from opencrab.pack import build as build_mod
from opencrab.pack.build import Pack
from opencrab.pack.jsonl_io import iter_jsonl
from opencrab.pack.schema import NODE_TYPE_OVERRIDE, PackSchemaError


@pytest.fixture(autouse=True)
def _no_host_default(monkeypatch):
    """호스트 리포(opencrab-dump shim)가 주입하는 기본값에서 테스트를 격리한다."""
    monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", None)
    monkeypatch.delenv("PACK_OUT_ROOT", raising=False)


@pytest.fixture
def pack(tmp_path):
    return Pack("t", "제목", out_root=str(tmp_path))


# ---------------------------------------------------------------------------
# 출력 루트 — 이관에서 바뀐 지점
# ---------------------------------------------------------------------------

class TestOutRoot:
    def test_explicit_out_root(self, tmp_path):
        p = Pack("t", "제목", out_root=str(tmp_path))
        assert p.out == tmp_path / "t"
        assert p.out.is_dir()

    def test_env_out_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path))
        assert Pack("t", "제목").out == tmp_path / "t"

    def test_explicit_arg_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path / "env"))
        assert Pack("t", "제목", out_root=str(tmp_path / "arg")).out == tmp_path / "arg" / "t"

    def test_missing_out_root_raises(self):
        """자동 유도는 리포를 넘는 순간 조용히 틀린 곳에 쓴다(mkdir(exist_ok=True) 라 예외도 없다).

        이관 전에는 `Path(__file__).parents[2] / 'by-pack'` 이었고, 이 파일이 localcrab 으로
        온 지금 그 코드가 남아 있었다면 localcrab 리포 안에 by-pack 을 만들었을 것이다.
        """
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_empty_env_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", "")
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_host_default_out_root_is_used(self, tmp_path, monkeypatch):
        """호스트 리포가 주입하는 기본값. opencrab-dump shim 이 이 경로로 by-pack 을 준다."""
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path))
        assert Pack("t", "제목").out == tmp_path / "t"

    def test_env_beats_host_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path / "host"))
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path / "env"))
        assert Pack("t", "제목").out == tmp_path / "env" / "t"

    def test_does_not_write_pack_out_root_into_environ(self, tmp_path, monkeypatch):
        """**회귀 검사.** 한때 shim 이 `os.environ.setdefault('PACK_OUT_ROOT', ...)` 로
        기본값을 넣었다. 그러면 `os.environ.get('PACK_OUT_ROOT')` 로 "호출자가 드라이런을
        지정했는가"를 판정하던 코드가 항상 참이 되어, 운영 빌드가 git 추적 산출물
        (chatgpt/canonical/, *-prep/00_index/) 대신 스크래치 경로로 조용히 새 나갔다.
        exit 0 에 성공 배너까지 나서 아무도 몰랐다(2026-08-04 검증에서 발각).

        출력 루트 해석은 프로세스 전역 상태를 건드리면 안 된다.
        """
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path))
        Pack("t", "제목")
        assert os.environ.get("PACK_OUT_ROOT") is None


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------

class TestNode:
    def test_props_go_into_nested_properties(self, pack):
        """91만 필드 사고의 회귀 검사 — 최상위로 펼치면 적재기가 통째로 못 읽었다."""
        pack.node("n1", "L", "Concept", "concept", {"mst": "268103"})
        rec = pack.nodes[0]
        assert rec["properties"] == {"mst": "268103"}
        assert "mst" not in {k for k in rec if k != "properties"}

    def test_no_properties_key_when_props_empty(self, pack):
        pack.node("n1", "L", "Concept", "concept")
        assert "properties" not in pack.nodes[0]

    def test_duplicate_id_is_ignored(self, pack):
        pack.node("n1", "A", "Concept", "concept")
        pack.node("n1", "B", "Concept", "concept")
        assert len(pack.nodes) == 1
        assert pack.nodes[0]["label"] == "A"

    def test_label_truncated_to_300(self, pack):
        pack.node("n1", "가" * 500, "Concept", "concept")
        assert len(pack.nodes[0]["label"]) == 300

    def test_source_type_override_is_hoisted_not_duplicated(self, pack):
        """팩 기본값을 노드별로 덮는 것은 의도된 기능. 중첩에 중복으로 남으면 안 된다."""
        pack.node("n1", "L", "Concept", "concept", {"source_type": "self-authored"})
        rec = pack.nodes[0]
        assert rec["source_type"] == "self-authored"
        assert "properties" not in rec

    def test_reserved_key_rejected_via_schema(self, pack):
        with pytest.raises(PackSchemaError, match="예약 키"):
            pack.node("n1", "L", "Concept", "concept", {"id": "x"})

    def test_pre_wrapped_props_rejected_via_schema(self, pack):
        with pytest.raises(PackSchemaError, match="평면 dict"):
            pack.node("n1", "L", "Concept", "concept", {"properties": {}})

    def test_schema_error_is_a_value_error(self, pack):
        """기존 빌더가 `except ValueError` 로 잡는다 — 계층을 깨면 조용히 안 잡힌다."""
        with pytest.raises(ValueError):
            pack.node("n1", "L", "Concept", "concept", {"space": "x"})

    def test_caller_props_dict_not_mutated(self, pack):
        """source_type 을 pop 하므로 호출자 dict 를 직접 건드리면 안 된다."""
        props = {"source_type": "self-authored", "a": 1}
        pack.node("n1", "L", "Concept", "concept", props)
        assert props == {"source_type": "self-authored", "a": 1}


# ---------------------------------------------------------------------------
# 엣지 grammar 정합화
# ---------------------------------------------------------------------------

class TestEdge:
    def test_allowed_relation_is_lowercased(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "CONTAINS")
        assert pack.edges[0]["label"] == "contains"
        # 대소문자만 달라도 source_label 이 남는다(characterization). 조건이 `rel != raw`
        # 라서 그렇다 — 원본 표기를 잃지 않는 쪽이 안전하므로 그대로 고정한다.
        assert pack.edges[0]["properties"] == {"source_label": "CONTAINS"}

    def test_exactly_matching_relation_leaves_properties_empty(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "contains")
        assert pack.edges[0]["properties"] == {}

    def test_unfit_label_is_replaced_and_original_preserved(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "슬쩍만든라벨")
        assert pack.edges[0]["label"] == "contains"
        assert pack.edges[0]["properties"]["source_label"] == "슬쩍만든라벨"

    def test_direction_reversed_when_only_inverse_pair_exists(self, pack):
        """(evidence, resource) 는 grammar 에 없고 (resource, evidence) 는 있다."""
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("e", "r", "contains")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == ("r", "e")

    def test_claim_to_evidence_keeps_direction_for_loader_reversal(self, pack):
        """KEEP 공간쌍. 파일에는 claim->evidence 로 적히고 적재기가 뒤집는다 —
        파일과 라이브의 엣지 수가 달라 보이는 원인이 이것이다."""
        pack.node("c", "C", "Claim", "claim")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("c", "e", "supports")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == ("c", "e")
        assert pack.edges[0]["label"] == "EVIDENCED_BY"

    def test_incompatible_pair_is_dropped_and_counted(self, pack):
        pack.node("co", "CO", "Community", "community")
        pack.node("s", "S", "Org", "subject")
        pack.edge("co", "s", "owns")
        assert pack.edges == []
        assert sum(pack._eskip.values()) == 1

    def test_duplicate_edge_deduped(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "contains")
        pack.edge("r", "e", "contains")
        assert len(pack.edges) == 1

    def test_edge_between_unknown_nodes_passes_through_unvalidated(self, pack):
        """space 를 모르면 grammar 검사를 못 한다. 그 통과분을 validate() 가 잡는다."""
        pack.edge("unknown-a", "unknown-b", "whatever")
        assert pack.edges[0]["label"] == "whatever"


class TestEv:
    def test_creates_node_chunk_and_contains_edge(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "  본문  ")
        assert len(pack.nodes) == 2 and len(pack.chunks) == 1
        assert pack.chunks[0]["text"] == "본문"
        assert [e["label"] for e in pack.edges] == ["contains"]

    def test_extra_goes_to_chunk_meta_and_node_props_to_node(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문", extra={"page": 3}, node_props={"mst": "1"})
        assert pack.chunks[0]["metadata"]["page"] == 3
        assert "page" not in (pack.nodes[1].get("properties") or {})
        assert pack.nodes[1]["properties"] == {"mst": "1"}
        assert "mst" not in pack.chunks[0]["metadata"]

    def test_evidence_index_increments_per_document(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "1", "a")
        pack.ev("e2", doc, "2", "b")
        assert [c["metadata"]["evidence_index"] for c in pack.chunks] == [1, 2]

    def test_char_end_is_untrimmed_length(self, pack):
        """text 는 strip 해서 싣지만 char_end 는 원문 길이다(characterization)."""
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "  ab  ")
        assert pack.chunks[0]["metadata"]["char_end"] == 6
        assert pack.chunks[0]["text"] == "ab"

    def test_reregistering_same_eid_is_a_silent_noop(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "a", node_props={"k": 1})
        pack.ev("e1", doc, "다른라벨", "b", node_props={"k": 2})
        assert len(pack.chunks) == 1


# ---------------------------------------------------------------------------
# validate — 이관으로 되살아난 검사
# ---------------------------------------------------------------------------

class TestValidateRemapHazard:
    def test_hazard_is_detected(self, pack, capsys):
        """선언 space 와 적재기 remap 후 space 가 다르면 연결 엣지가 전량 skip 된다.

        **이관 전 이 검사는 사실상 꺼져 있었다.** 표를 적재기에서 지연 import 하고
        `except Exception: _NTO = None` 으로 삼켰기 때문에, 적재기를 import 할 수 없는
        빌드 환경(= 대부분)에서는 통째로 건너뛰었다. 표가 schema 로 오면서 항상 돈다.
        """
        assert NODE_TYPE_OVERRIDE["TextUnit"] == ("concept", "Topic")
        pack.node("n1", "L", "TextUnit", "evidence")
        pack.validate()
        assert "remap 함정" in capsys.readouterr().out

    def test_hazard_blocks_in_strict_mode(self, pack):
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError, match="remap 함정"):
            pack.validate(strict=True)

    def test_matching_type_is_not_a_hazard(self, pack, capsys):
        pack.node("n1", "L", "TextEvidence", "evidence")
        pack.validate()
        assert "remap 함정" not in capsys.readouterr().out

    def test_strict_can_be_enabled_by_env(self, pack, monkeypatch):
        monkeypatch.setenv("PACK_LIB_STRICT", "1")
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError):
            pack.validate()


class TestValidateOther:
    def test_dangling_edge_blocks_only_in_strict(self, pack):
        pack.edge("ghost-a", "ghost-b", "related_to")
        pack.validate()                       # 경고만
        with pytest.raises(ValueError, match="dangling"):
            pack.validate(strict=True)

    def test_grammar_violation_reported(self, pack, capsys):
        pack.edge("ghost-a", "ghost-b", "related_to")   # space 미확정으로 검증 우회
        pack.node("ghost-a", "A", "Concept", "concept")
        pack.node("ghost-b", "B", "Evidence", "evidence")
        pack.validate()
        assert "grammar 위반" in capsys.readouterr().out

    def test_empty_spaces_warned(self, pack, capsys):
        pack.node("n1", "L", "Concept", "concept")
        pack.validate()
        assert "9-space 비어있음" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------

class TestSave:
    def test_writes_three_logical_files(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        pack.save()
        assert [r["id"] for r in iter_jsonl(pack.out / "chunks.jsonl")] == ["e1"]
        assert len(list(iter_jsonl(pack.out / "nodes.jsonl"))) == 2
        assert len(list(iter_jsonl(pack.out / "edges.jsonl"))) == 1

    def test_save_runs_validate(self, pack):
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError, match="remap 함정"):
            pack.validate(strict=True)

    def test_output_is_utf8_unescaped(self, pack):
        pack.node("n1", "한글 라벨", "Concept", "concept")
        pack.save()
        assert "한글 라벨" in (pack.out / "nodes.jsonl").read_text(encoding="utf-8")

    def test_uid_is_deterministic_and_slug_namespaced(self, tmp_path):
        a = Pack("pack-a", "A", out_root=str(tmp_path))
        b = Pack("pack-b", "B", out_root=str(tmp_path))
        assert a.uid("x", 1) == Pack("pack-a", "A", out_root=str(tmp_path)).uid("x", 1)
        assert a.uid("x", 1) != b.uid("x", 1)

    def test_created_at_is_uniform_within_a_pack(self, pack):
        """전 레코드에 같은 빌드 시각이 박힌다 — 재빌드 diff 검증이 이 성질에 의존한다."""
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        stamps = {r["created_at"] for r in pack.nodes + pack.edges + pack.chunks}
        assert len(stamps) == 1

    def test_module_reexports_json_for_legacy_builders(self):
        """`pack_lib.json.dumps(...)` 를 쓰는 빌더가 있다(실측 3곳)."""
        from opencrab.pack import build
        assert build.json is json
