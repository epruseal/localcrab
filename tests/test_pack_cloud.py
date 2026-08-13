"""``opencrab.pack.cloud`` 계약(2026-08-10 opencrab-dump 이관).

**왜 이 파일이 필요한가.** 이 모듈은 `opencrab.pack.assembler.assemble_pack_v1`
과 완전히 별개인 두 번째 ZIP 생성기다 — manifest 키(`format` vs `format_version`)와
값(`opencrab-cloud-pack-v1` vs `opencrab-pack-v1`)이 다르고, 입력 레이아웃(3-jsonl
평면 디렉터리 vs 스테이징 디렉터리)도 다르다. 둘을 헷갈려 값이 같아지면 소비자
(Cloud 업로드 파이프라인 vs 로컬 재적재)가 산출물을 구분할 수 없다 — 그래서 그
차이 자체를 테스트로 건다.

또 이관 전에는 `pack_slug`만 받아 `BASE_DIR / "by-pack" / pack_slug`를 함수 내부에서
조립했다. 그 `BASE_DIR`가 `Path(__file__).parents[N]` 유도값이라, 패키지가 원래
리포(opencrab-dump)를 벗어나 이 리포(localcrab)에 들어오는 순간 엉뚱한 경로를
가리키게 된다 — import 시점에는 안 터지고 파일을 쓰는 순간 조용히 엉뚱한 곳에
쓴다. 그래서 pack_dir를 인자로 받아야 하고, 그 계약을 여기서 고정한다.

기대값은 이 모듈이 아니라 호출자(구 opencrab-dump `build_pack_zip.py`) 계약과
매뉴얼 계산에서 가져온다 — `build_zip` 자체의 출력을 기대값으로 되읽지 않는다.

**변이 스윕 결과(2026-08-10, `scripts/qa/mutate_module.py`) 202종 중 4종 생존,
전부 등가로 확인됨** — 추론이 아니라 입력 격자로 차분 0을 측정했다.

**이 수치의 범위 한정.** "202종"은 그 도구의 **연산자 집합 안에서만** 참이다. 도구는
비교 연산자·not·and/or·기본값·상수·문장 삭제·표 리터럴·호출 대상을 흔들 뿐,
`ZIP_DEFLATED`->`ZIP_STORED` 같은 **상수 교체**나 `writestr` 순서 재배열 축은 만들지
않는다. 실제로 그 두 축은 스윕이 초록인 채로 적대 검증이 손으로 뚫었고(2026-08-10),
그래서 `TestPhysicalRepresentation` 을 따로 두었다. 복합(두 위치 동시) 변이도 안 한다.
"202종 통과"를 "전부 훑었다"로 읽지 마라 — 그 오독이 이번 라운드 사고의 출발점이었다.

생존 4종의 등가 근거:
  - `mkdir(parents=True, exist_ok=True)`의 두 kwarg를 맞바꾸는 변이: 둘 다 상수
    `True`라 이름이 바뀌어도 값이 같다.
  - dangling edge 판정의 `if not src or not tgt: ...` 선점검(boolop 뒤집기·
    or-기본값 제거·if문 삭제 3종): 이 분기가 하는 일(`skipped_edges += 1;
    continue`)이 바로 다음 `if src not in node_ids or tgt not in node_ids:`
    분기와 **완전히 동일**하고, `graph_nodes`가 이미 truthy id만 걸러 담으므로
    (`TestNodeIdFiltering` 참조) falsy src/tgt는 반드시 후자에도 걸린다. 그래서
    선점검을 없애도(또는 뒤집어도) 최종 `skipped_edges` 값·`graph_edges` 내용이
    바뀌지 않는다. 그 전제(id 필터링)를 `TestNodeIdFiltering`이 못박는다.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from opencrab.pack import assemble_pack_v1, build_cloud_zip
from opencrab.pack.cloud import build_zip


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _pack(tmp_path: Path, name: str, nodes: list[dict], edges: list[dict] | None = None,
          chunks: list[dict] | None = None) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    _write_jsonl(d / "nodes.jsonl", nodes)
    if edges is not None:
        _write_jsonl(d / "edges.jsonl", edges)
    if chunks is not None:
        _write_jsonl(d / "chunks.jsonl", chunks)
    return d


def _n(i, space="concept", node_type="Concept", **props):
    row = {"id": i, "label": i, "node_type": node_type, "space": space}
    if props:
        row["properties"] = props
    return row


def _c(i, text, document_id=""):
    return {"id": i, "document_id": document_id, "text": text}


class TestManifestDiffersFromAssemblePackV1:
    """정본 키가 이 모듈의 존재 이유다 — `assemble_pack_v1`과 값이 같아지면
    두 소비자가 산출물을 구분할 수 없다."""

    def test_format_key_and_value_are_cloud_specific(self, tmp_path):
        d = _pack(tmp_path, "krds", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "hello")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)

        assert manifest["format"] == "opencrab-cloud-pack-v1"
        assert "format_version" not in manifest

    def test_differs_from_assemble_pack_v1_manifest(self, tmp_path):
        cloud_dir = _pack(tmp_path, "krds", [_n("a", space="resource")], edges=[],
                          chunks=[_c("c1", "hello")])
        cloud_manifest = build_zip(cloud_dir, tmp_path / "cloud.zip")

        stage = tmp_path / "stage" / "neo4j"
        stage.mkdir(parents=True)
        _write_jsonl(stage / "opencrab_ingest.jsonl",
                     [{"kind": "node", "payload": {"id": "node:a", "label": "A", "evidence_refs": []}}])
        assemble_pack_v1(stage.parent, tmp_path / "assembled.zip", pack_id="krds-assembled")
        with zipfile.ZipFile(tmp_path / "assembled.zip") as zf:
            assembled_manifest = json.loads(zf.read("manifest.json"))

        assert cloud_manifest["format"] != assembled_manifest.get("format_version")
        assert "format" not in assembled_manifest
        assert "format_version" not in cloud_manifest
        assert set(cloud_manifest) & {"format", "format_version"} == {"format"}
        assert set(assembled_manifest) & {"format", "format_version"} == {"format_version"}


class TestPackDirIsCallerControlled:
    """`pack_dir`은 호출자가 넘긴 그대로 읽어야 한다 — `by-pack`이라는 이름이나
    위치를 이 모듈이 알고 있으면 안 된다."""

    def test_arbitrary_directory_name_works_no_by_pack_assumption(self, tmp_path):
        pack_dir = tmp_path / "completely_unrelated_dirname"
        pack_dir.mkdir()
        _write_jsonl(pack_dir / "nodes.jsonl", [_n("a", space="resource")])
        _write_jsonl(pack_dir / "edges.jsonl", [])
        _write_jsonl(pack_dir / "chunks.jsonl", [_c("c1", "hello")])
        out = tmp_path / "nested" / "deep" / "output.zip"

        manifest = build_zip(pack_dir, out)

        assert out.exists()
        assert manifest["pack_id"] == "completely_unrelated_dirname"
        # by-pack 이라는 이름의 디렉터리가 부작용으로 생기지 않아야 한다
        assert not any(p.name == "by-pack" for p in tmp_path.rglob("*") if p.is_dir())

    def test_out_path_is_exactly_where_caller_points_not_derived(self, tmp_path):
        pack_dir = _pack(tmp_path, "src", [_n("a", space="resource")], edges=[],
                         chunks=[_c("c1", "hi")])
        out = tmp_path / "elsewhere" / "custom_name.zip"

        build_zip(pack_dir, out)

        assert out.exists()
        # pack_dir 옆(같은 부모)에는 아무 zip도 안 생겨야 한다
        assert list(pack_dir.parent.glob("*.zip")) == []

    def test_reexported_from_package_root(self, tmp_path):
        """`opencrab.pack.build_cloud_zip`가 `cloud.build_zip`과 동일 객체인가."""
        assert build_cloud_zip is build_zip


class TestZipStructure:
    def test_namelist_is_exactly_five_files(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "hello")])
        out = tmp_path / "out.zip"
        build_zip(d, out)

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert names == {
            "manifest.json",
            "graph/nodes.jsonl",
            "graph/edges.jsonl",
            "cloud/documents.jsonl",
            "cloud/chunks.jsonl",
        }

    def test_manifest_counts_match_actual_entry_line_counts(self, tmp_path):
        d = _pack(
            tmp_path, "p",
            [_n("a", space="resource"), _n("b", space="resource")],
            edges=[{"id": "e1", "source_id": "a", "target_id": "b"}],
            chunks=[_c("c1", "hello"), _c("c2", "world")],
        )
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)

        with zipfile.ZipFile(out) as zf:
            nodes_lines = zf.read("graph/nodes.jsonl").decode().strip().splitlines()
            edges_lines = zf.read("graph/edges.jsonl").decode().strip().splitlines()
            docs_lines = zf.read("cloud/documents.jsonl").decode().strip().splitlines()
            chunks_lines = zf.read("cloud/chunks.jsonl").decode().strip().splitlines()

        assert manifest["counts"]["nodes"] == len(nodes_lines) == 2
        assert manifest["counts"]["edges"] == len(edges_lines) == 1
        assert manifest["counts"]["documents"] == len(docs_lines) == 2
        assert manifest["counts"]["chunks"] == len(chunks_lines) == 2


class TestEdgeNormalization:
    def test_dangling_edge_is_skipped_and_counted(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")],
                  edges=[{"id": "e1", "source_id": "a", "target_id": "ghost"}],
                  chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")

        assert manifest["counts"]["edges"] == 0
        assert manifest["counts"]["edges_skipped"] == 1

    def test_alias_keys_from_to_are_recognized(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource"), _n("b", space="resource")],
                  edges=[{"id": "e1", "from": "a", "to": "b"}],
                  chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)

        assert manifest["counts"]["edges"] == 1
        assert manifest["counts"]["edges_skipped"] == 0
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert (edge["source"], edge["target"]) == ("a", "b")

    def test_missing_label_defaults_to_relates_to(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource"), _n("b", space="resource")],
                  edges=[{"id": "e1", "source_id": "a", "target_id": "b"}],
                  chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge["label"] == "relates_to"


class TestDocumentAndChunkFiltering:
    def test_textunit_node_excluded_from_documents(self, tmp_path):
        d = _pack(tmp_path, "p",
                  [_n("a", space="resource", node_type="TextUnit"),
                   _n("b", space="resource", node_type="Document")],
                  edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["documents"] == 1

    def test_non_resource_evidence_space_excluded_from_documents(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="concept")], edges=[],
                  chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["documents"] == 0

    def test_blank_chunk_text_excluded(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "hi"), _c("c2", "   "), _c("c3", "")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["chunks"] == 1

    def test_chunk_text_is_stripped(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "  hello  ")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            chunk = json.loads(zf.read("cloud/chunks.jsonl").decode().strip())
        assert chunk["text"] == "hello"


class TestErrorPaths:
    def test_missing_nodes_jsonl_exits(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(SystemExit):
            build_zip(d, tmp_path / "out.zip")

    def test_no_readable_document_or_chunk_exits(self, tmp_path):
        # resource 노드가 없고 청크도 비어 있으면 업로드할 것이 없다
        d = _pack(tmp_path, "p", [_n("a", space="concept")], edges=[], chunks=[])
        with pytest.raises(SystemExit):
            build_zip(d, tmp_path / "out.zip")


class TestPackIdAndTitleDefaults:
    def test_defaults_to_pack_dir_name(self, tmp_path):
        d = _pack(tmp_path, "my-slug", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["pack_id"] == "my-slug"
        assert manifest["title"] == "my-slug"

    def test_overrides_win_over_dir_name(self, tmp_path):
        d = _pack(tmp_path, "my-slug", [_n("a", space="resource")], edges=[],
                  chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip", pack_id="custom-id", title="Custom Title")
        assert manifest["pack_id"] == "custom-id"
        assert manifest["title"] == "Custom Title"


class TestSpaceDistribution:
    def test_counts_nodes_per_space(self, tmp_path):
        d = _pack(tmp_path, "p",
                  [_n("a", space="resource"), _n("b", space="resource"), _n("c", space="concept")],
                  edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["spaces"]["resource"] == 2
        assert manifest["spaces"]["concept"] == 1
        assert manifest["spaces"]["policy"] == 0

    def test_spaces_dict_has_all_nine_canonical_keys(self, tmp_path):
        """키 이름 하나가 다른 값으로 바뀌어도(예: 'subject'->'') 개수 축만 보는
        테스트는 못 잡는다 — 키 집합 자체를 고정한다."""
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert set(manifest["spaces"].keys()) == {
            "subject", "resource", "evidence", "concept", "claim",
            "community", "outcome", "lever", "policy",
        }

    def test_remaining_space_names_are_individually_counted(self, tmp_path):
        nodes = [_n("a", space="subject"), _n("b", space="evidence"), _n("c", space="evidence"),
                 _n("d", space="claim"), _n("e", space="community"), _n("f", space="outcome"),
                 _n("g", space="lever")]
        d = _pack(tmp_path, "p", nodes, edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["spaces"]["subject"] == 1
        assert manifest["spaces"]["evidence"] == 2
        assert manifest["spaces"]["claim"] == 1
        assert manifest["spaces"]["community"] == 1
        assert manifest["spaces"]["outcome"] == 1
        assert manifest["spaces"]["lever"] == 1


class TestManifestStructure:
    def test_top_level_keys_are_exact(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert set(manifest.keys()) == {"format", "pack_id", "title", "created_at", "counts", "spaces"}

    def test_counts_keys_are_exact(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert set(manifest["counts"].keys()) == {"nodes", "edges", "edges_skipped", "documents", "chunks"}

    def test_created_at_matches_expected_timestamp_format(self, tmp_path):
        import re
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["created_at"])


class TestEdgeFullFidelity:
    """개별 필드 하나하나가 아니라 완전히 채워진 입력에서 출력 dict 전체를
    고정한다 — 키 이름·값 리터럴이 하나씩 지워져도 여기서 잡힌다."""

    def test_all_fields_pass_through_exactly(self, tmp_path):
        nodes = [_n("n1"), _n("n2")]
        edges = [{
            "id": "e1", "source_id": "n1", "target_id": "n2", "label": "cites",
            "created_at": "2026-01-01T00:00:00Z", "properties": {"weight": 3},
        }]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge == {
            "id": "e1", "source": "n1", "target": "n2", "label": "cites",
            "created_at": "2026-01-01T00:00:00Z", "properties": {"weight": 3},
        }

    def test_relation_key_used_when_label_absent(self, tmp_path):
        nodes = [_n("n1"), _n("n2")]
        edges = [{"id": "e1", "source_id": "n1", "target_id": "n2", "relation": "cites"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge["label"] == "cites"

    def test_edge_without_properties_defaults_to_empty_dict(self, tmp_path):
        nodes = [_n("n1"), _n("n2")]
        edges = [{"id": "e1", "source_id": "n1", "target_id": "n2"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge["properties"] == {}

    @pytest.mark.parametrize("key", ["source_id", "from_id", "from", "source"])
    def test_each_source_key_variant_is_recognized_alone(self, tmp_path, key):
        nodes = [_n("n1"), _n("n2")]
        edges = [{"id": "e1", key: "n1", "target_id": "n2"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)
        assert manifest["counts"]["edges"] == 1
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge["source"] == "n1"

    @pytest.mark.parametrize("key", ["target_id", "to_id", "to", "target"])
    def test_each_target_key_variant_is_recognized_alone(self, tmp_path, key):
        nodes = [_n("n1"), _n("n2")]
        edges = [{"id": "e1", "source_id": "n1", key: "n2"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)
        assert manifest["counts"]["edges"] == 1
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert edge["target"] == "n2"

    def test_missing_source_entirely_skips_with_exact_increment(self, tmp_path):
        """src 가 완전히 없으면 skipped_edges 가 **정확히** 1 증가해야 한다 —
        '스킵됐다'만 보면 그 안의 `+= 1`이 `+= 2`로 바뀌어도 안 잡힌다."""
        nodes = [_n("n1")]
        edges = [{"id": "e1", "target_id": "n1"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["edges_skipped"] == 1
        assert manifest["counts"]["edges"] == 0


class TestDocumentFullFidelity:
    def test_all_fields_pass_through_exactly(self, tmp_path):
        nodes = [{
            "id": "doc1", "label": "My Title", "node_type": "Report", "space": "resource",
            "created_at": "2026-01-01T00:00:00Z",
            "properties": {"source": "official-site", "url": "http://x", "source_url": "http://x/direct"},
        }]
        d = _pack(tmp_path, "p", nodes, edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc == {
            "id": "doc1", "title": "My Title", "source": "official-site",
            "source_url": "http://x/direct", "space": "resource", "node_type": "Report",
            "pack_id": manifest["pack_id"], "created_at": "2026-01-01T00:00:00Z",
            "properties": nodes[0]["properties"],
        }

    def test_evidence_space_included_but_output_space_field_fixed_to_resource(self, tmp_path):
        nodes = [_n("e1", space="evidence")]
        d = _pack(tmp_path, "p", nodes, edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)
        assert manifest["counts"]["documents"] == 1
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["space"] == "resource"  # 입력이 evidence 여도 출력은 고정값

    def test_source_falls_back_url_then_label(self, tmp_path):
        n1 = {"id": "d1", "label": "L1", "node_type": "Document", "space": "resource",
              "properties": {"source": "S", "url": "U"}}
        n2 = {"id": "d2", "label": "L2", "node_type": "Document", "space": "resource",
              "properties": {"url": "U2"}}
        n3 = {"id": "d3", "label": "L3", "node_type": "Document", "space": "resource",
              "properties": {}}
        d = _pack(tmp_path, "p", [n1, n2, n3], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            docs = {j["id"]: j for j in (
                json.loads(line) for line in zf.read("cloud/documents.jsonl").decode().strip().splitlines())}
        assert docs["d1"]["source"] == "S"
        assert docs["d2"]["source"] == "U2"
        assert docs["d3"]["source"] == "L3"

    def test_source_url_falls_back_url_then_empty(self, tmp_path):
        n1 = {"id": "d1", "label": "L1", "node_type": "Document", "space": "resource",
              "properties": {"source_url": "SU"}}
        n2 = {"id": "d2", "label": "L2", "node_type": "Document", "space": "resource",
              "properties": {"url": "U2"}}
        n3 = {"id": "d3", "label": "L3", "node_type": "Document", "space": "resource",
              "properties": {}}
        d = _pack(tmp_path, "p", [n1, n2, n3], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            docs = {j["id"]: j for j in (
                json.loads(line) for line in zf.read("cloud/documents.jsonl").decode().strip().splitlines())}
        assert docs["d1"]["source_url"] == "SU"
        assert docs["d2"]["source_url"] == "U2"
        assert docs["d3"]["source_url"] == ""

    def test_node_type_defaults_to_document_when_absent(self, tmp_path):
        n = {"id": "d1", "label": "L", "space": "resource"}  # node_type 키 자체가 없음
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["node_type"] == "Document"

    def test_title_and_source_fallback_default_to_empty_when_label_absent(self, tmp_path):
        # label도 properties도 없으면 title뿐 아니라 source 폴백 체인의 마지막 단
        # (`n.get("label", "")`)도 기본값 ""로 떨어져야 한다 — label 있는 입력만
        # 쓰면 이 `.get(key, default)`의 default 인자가 지워져도 안 잡힌다.
        n = {"id": "d1", "space": "resource"}  # label 없음, properties 없음
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["title"] == ""
        assert doc["source"] == ""

    def test_pack_id_override_flows_into_document_pack_id(self, tmp_path):
        n = _n("d1", space="resource")
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out, pack_id="override-id")
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["pack_id"] == "override-id"

    def test_pack_id_defaults_to_pack_dir_name_in_document(self, tmp_path):
        n = _n("d1", space="resource")
        d = _pack(tmp_path, "my-slug-xyz", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["pack_id"] == "my-slug-xyz"


class TestChunkFullFidelity:
    def test_all_fields_pass_through_exactly(self, tmp_path):
        chunks = [{"id": "c1", "document_id": "d1", "text": "hello world",
                   "source": "src-x", "metadata": {"page": 3}}]
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=chunks)
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            chunk = json.loads(zf.read("cloud/chunks.jsonl").decode().strip())
        assert chunk == {"id": "c1", "document_id": "d1", "text": "hello world",
                         "source": "src-x", "metadata": {"page": 3}}

    def test_missing_document_id_defaults_to_empty_string(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[{"id": "c1", "text": "hi"}])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            chunk = json.loads(zf.read("cloud/chunks.jsonl").decode().strip())
        assert chunk["document_id"] == ""

    def test_missing_source_defaults_to_empty_string(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[{"id": "c1", "text": "hi"}])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            chunk = json.loads(zf.read("cloud/chunks.jsonl").decode().strip())
        assert chunk["source"] == ""

    def test_missing_metadata_defaults_to_empty_dict(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[{"id": "c1", "text": "hi"}])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            chunk = json.loads(zf.read("cloud/chunks.jsonl").decode().strip())
        assert chunk["metadata"] == {}

    def test_missing_text_key_entirely_is_excluded_without_crashing(self, tmp_path):
        # text 키 자체가 없음(빈 문자열이 아니라 부재) — `c.get("text") or ""` 의
        # or 기본값이 지워지면 `None.strip()`으로 죽는다.
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[{"id": "c1"}])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["chunks"] == 0


class TestRawByteFormatting:
    def test_non_ascii_chunk_text_stored_unescaped_utf8(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "안녕하세요")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            raw = zf.read("cloud/chunks.jsonl")
        assert "안녕하세요".encode() in raw
        assert b"\\u" not in raw

    def test_manifest_non_ascii_title_stored_unescaped(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out, title="한글 타이틀")
        with zipfile.ZipFile(out) as zf:
            raw = zf.read("manifest.json")
        assert "한글 타이틀".encode() in raw
        assert b"\\u" not in raw

    def test_manifest_json_is_pretty_printed_with_two_space_indent(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            raw = zf.read("manifest.json").decode()
        assert '\n  "format"' in raw


class TestAcceptsStringPaths:
    def test_pack_dir_and_out_path_accept_plain_strings(self, tmp_path):
        """`pack_dir`/`out_path`는 함수 안에서 `Path(...)`로 감싸야 한다 — 안 그러면
        문자열 인자를 받았을 때 `.name`/`.parent` 접근에서 죽는다."""
        d = _pack(tmp_path, "p", [_n("a", space="resource")], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(str(d), str(out))
        assert out.exists()
        assert manifest["pack_id"] == "p"


class TestErrorMessages:
    def test_missing_nodes_message_names_the_path(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(SystemExit) as ei:
            build_zip(d, tmp_path / "out.zip")
        assert "nodes.jsonl" in str(ei.value)
        assert "없음" in str(ei.value)

    def test_empty_pack_message_is_specific(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="concept")], edges=[], chunks=[])
        with pytest.raises(SystemExit) as ei:
            build_zip(d, tmp_path / "out.zip")
        assert "readable document/chunk" in str(ei.value)


class TestPhysicalRepresentation:
    """ZIP 의 **물리 표현**도 계약이다 — 내용이 같아도 표현이 바뀌면 소비자가 깨진다.

    적대 검증이 실측으로 보였다(2026-08-10): `ZIP_DEFLATED` 를 `ZIP_STORED` 로 바꾸면
    산출물이 170KB -> 767KB(4.5배)가 되는데 어느 테스트도 안 잡았고, 엔트리 순서를
    역전시켜도 마찬가지였다. 엔트리 **내용** sha 는 0건 변경이므로 "등가"로 읽을 수도
    있으나, 업로드 비용과 스트리밍 소비자의 읽기 순서는 등가가 아니다.

    내용 계약(`TestZipStructure` 등)은 무엇이 들어 있는지를 걸고, 여기서는 **어떻게
    들어 있는지**를 건다.
    """

    def _out(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("a", space="resource"), _n("b", space="concept")],
                  edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        return out

    def test_every_entry_is_deflated_not_stored(self, tmp_path):
        """무압축으로 바뀌면 배포 비용이 몇 배가 된다. 판정도 해시도 안 바뀌므로
        압축 방식 자체를 건다."""
        with zipfile.ZipFile(self._out(tmp_path)) as zf:
            methods = {i.filename: i.compress_type for i in zf.infolist()}
        assert set(methods.values()) == {zipfile.ZIP_DEFLATED}, \
            f"DEFLATED 가 아닌 엔트리가 있다: {methods}"

    def test_entry_order_is_manifest_then_graph_then_cloud(self, tmp_path):
        """순서를 **리스트로** 단언한다. 집합으로 걸면 역전 변이가 통과한다.

        manifest 가 먼저여야 소비자가 나머지를 읽기 전에 포맷을 판별할 수 있다.
        """
        with zipfile.ZipFile(self._out(tmp_path)) as zf:
            assert zf.namelist() == [
                "manifest.json",
                "graph/nodes.jsonl",
                "graph/edges.jsonl",
                "cloud/documents.jsonl",
                "cloud/chunks.jsonl",
            ]

    def test_node_records_keep_input_order(self, tmp_path):
        """레코드 순서는 입력 순서다 — 정렬하거나 뒤집지 않는다.

        호출자의 3-jsonl 과 ZIP 안의 `graph/nodes.jsonl` 을 줄 단위로 대사하는
        소비자가 있으므로 순서가 바뀌면 그 대사가 통째로 어긋난다.
        """
        ids = ["z1", "a1", "m1"]                       # 일부러 사전순이 아니다
        d = _pack(tmp_path, "ord", [_n(i, space="resource") for i in ids],
                  edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "ord.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            got = [json.loads(x)["id"]
                   for x in zf.read("graph/nodes.jsonl").decode().splitlines()]
        assert got == ids, f"입력 순서가 안 지켜졌다: {got}"

    def test_manifest_indent_is_exactly_two_for_nested_levels(self, tmp_path):
        """indent=2 를 **중첩 단계까지** 건다. 최상위 키 하나만 보면 indent=4 도 통과한다."""
        with zipfile.ZipFile(self._out(tmp_path)) as zf:
            raw = zf.read("manifest.json").decode()
        assert '\n  "format"' in raw, "최상위 들여쓰기가 2칸이 아니다"
        assert '\n    "nodes"' in raw, "중첩 들여쓰기가 2칸 단위가 아니다"


class TestSpacesCountsOnlyGraphNodes:
    """`spaces` 집계의 **출처**가 `graph_nodes` 인가(원본 `nodes` 가 아니라).

    id 없는 노드는 `graph/nodes.jsonl` 에 안 실린다. 그런데 `spaces` 를 원본에서 세면
    manifest 가 "resource 2개"라고 하는데 실제 엔트리에는 1개만 있게 된다 —
    manifest 와 본문이 어긋나고, 그 어긋남은 counts 만 보는 테스트로는 안 보인다.
    """

    def test_node_without_id_is_excluded_from_space_distribution(self, tmp_path):
        d = _pack(tmp_path, "p",
                  [{"id": "", "label": "no-id", "space": "resource"},
                   _n("a", space="resource")],
                  edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["spaces"]["resource"] == 1, \
            "id 없는 노드가 space 분포에 잡혔다 — manifest 와 본문이 어긋난다"
        assert manifest["counts"]["nodes"] == 1
        assert sum(manifest["spaces"].values()) == manifest["counts"]["nodes"], \
            "space 합계와 노드 수가 안 맞는다"


class TestAbsentOptionalFieldsBecomeNoneNotMissing:
    """없는 선택 필드는 **키를 지우는 게 아니라 `None`** 으로 실린다.

    소비자가 `e["created_at"]` 로 읽으면 키가 없을 때 `KeyError` 로 죽는다. 값이
    `None` 인 것과 키가 없는 것은 다른 계약이고, 전자가 이 포맷의 계약이다.
    """

    def test_edge_created_at_is_none_when_absent(self, tmp_path):
        nodes = [_n("n1"), _n("n2")]
        edges = [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "cites"}]
        d = _pack(tmp_path, "p", nodes, edges=edges, chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            edge = json.loads(zf.read("graph/edges.jsonl").decode().strip())
        assert "created_at" in edge, "키 자체가 사라졌다 — 소비자가 KeyError 로 죽는다"
        assert edge["created_at"] is None

    def test_document_created_at_is_none_when_absent(self, tmp_path):
        d = _pack(tmp_path, "p", [_n("r1", space="resource")],
                  edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert "created_at" in doc and doc["created_at"] is None


class TestLegacyTopLevelPropsAbsorbed:
    """레거시 호환: 2026-08-03 이전 생산자는 커스텀 필드(url·source_url 등)를 노드
    최상위에 펼쳤다. 이 모듈은 종전엔 `n.get("properties", {})`만 읽어 그 필드들이
    documents.jsonl 에 조용히 실리지 않았다(PR 리뷰 N1, 2026-08-13). 로더
    (`opencrab.pack.normalize.transform_node`)와 같은 정본 흡수 규칙
    (`opencrab.pack.schema.absorb_legacy_top_level`)을 재사용해야 한다."""

    def test_legacy_top_level_url_reaches_source_and_source_url(self, tmp_path):
        n = {"id": "d1", "label": "L1", "node_type": "Document", "space": "resource",
             "url": "http://legacy-top-level/doc"}  # properties 키 자체가 없다
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["source_url"] == "http://legacy-top-level/doc", \
            "최상위 url이 흡수되지 않았다 — 로더의 레거시 흡수 규칙과 어긋난다"
        assert doc["source"] == "http://legacy-top-level/doc"
        assert doc["properties"]["url"] == "http://legacy-top-level/doc", \
            "흡수된 필드가 출력 properties에도 실려야 한다"

    def test_nested_properties_win_over_legacy_top_level_on_key_collision(self, tmp_path):
        """정본 위치는 중첩이다 — 최상위와 중첩에 같은 키가 있으면 중첩이 이긴다."""
        n = {"id": "d1", "label": "L1", "node_type": "Document", "space": "resource",
             "url": "http://top-level",                    # 최상위(레거시)
             "properties": {"url": "http://nested"}}        # 중첩(정본)
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            doc = json.loads(zf.read("cloud/documents.jsonl").decode().strip())
        assert doc["properties"]["url"] == "http://nested"
        assert doc["source_url"] == "http://nested"

    def test_existing_nested_only_layout_is_byte_identical(self, tmp_path):
        """스트라이(최상위 커스텀 필드)가 없는 기존 레이아웃은 흡수 로직 도입
        **이전과 바이트 단위로 동일**해야 한다 — 회귀 방지."""
        n = {
            "id": "doc1", "label": "My Title", "node_type": "Report", "space": "resource",
            "created_at": "2026-01-01T00:00:00Z",
            "properties": {"source": "official-site", "url": "http://x", "source_url": "http://x/direct"},
        }
        d = _pack(tmp_path, "p", [n], edges=[], chunks=[_c("c1", "hi")])
        out = tmp_path / "out.zip"
        manifest = build_zip(d, out)
        with zipfile.ZipFile(out) as zf:
            raw = zf.read("cloud/documents.jsonl")
        expected = json.dumps({
            "id": "doc1", "title": "My Title", "source": "official-site",
            "source_url": "http://x/direct", "space": "resource", "node_type": "Report",
            "pack_id": manifest["pack_id"], "created_at": "2026-01-01T00:00:00Z",
            "properties": n["properties"],
        }, ensure_ascii=False).encode() + b"\n"
        assert raw == expected, "스트라이 없는 노드의 출력 바이트가 흡수 로직 도입으로 바뀌었다"


class TestNodeIdFiltering:
    """이 불변식(falsy id는 `graph_nodes`/`node_ids`에 결코 들어가지 않는다)이
    모듈 docstring에 적어 둔 등가 변이 4종의 전제다 — dangling edge 판정의
    `if not src or not tgt:` 선점검이, 뒤따르는 `if src not in node_ids or
    tgt not in node_ids:`로 완전히 흡수되는 이유가 바로 이것이다."""

    def test_node_without_truthy_id_is_excluded_from_graph_nodes(self, tmp_path):
        d = _pack(tmp_path, "p", [{"id": "", "label": "no-id"}, _n("a", space="resource")],
                  edges=[], chunks=[_c("c1", "hi")])
        manifest = build_zip(d, tmp_path / "out.zip")
        assert manifest["counts"]["nodes"] == 1  # id="" 노드는 제외된다
