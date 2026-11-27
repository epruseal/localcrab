"""규칙 관계의 문법, 적재 및 깊이 1 확장을 검증한다."""

import json
from itertools import product

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.grammar.manifest import SPACES
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.query import HybridQuery
from opencrab.pack.build import Pack
from opencrab.pack.gates.grammar_fit import fits, predict_grammar_fit
from opencrab.pack.load import load_edges, load_nodes
from opencrab.pack.normalize import resolve_edge
from opencrab.pack.ownership import create_pack
from opencrab.pack.schema import ALLOWED
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from opencrab.stores.sqlite_vec_store import SqliteVecStore
from tests._pack_fixtures import ensure_test_user

RELATIONS = [
    ("claim", "lever", "parameterized_by"),
    ("policy", "lever", "constrains"),
    ("evidence", "policy", "contradicts"),
    ("lever", "outcome", "stabilizes"),
]


@pytest.mark.parametrize("source,target,relation", RELATIONS)
@pytest.mark.parametrize("uppercase", [False, True])
def test_rule_relation_uses_canonical_grammar(source, target, relation, uppercase):
    label = relation.upper() if uppercase else relation
    assert relation in ALLOWED[(source, target)]
    assert resolve_edge(label, source, target) == (source, relation, target, False)
    assert fits(label, source, target)
    assert not fits(label, target, source)


@pytest.mark.parametrize("source,target,relation", RELATIONS)
def test_pack_builder_preserves_rule_relation(tmp_path, source, target, relation):
    pack = Pack("synthetic", "합성 관계 시험", out_root=str(tmp_path))
    for node_id, space in [("a", source), ("b", target)]:
        node_type = "Evidence" if space == "evidence" else SPACES[space]["node_types"][0]
        pack.node(node_id, node_id, node_type, space)
    pack.edge("a", "b", relation.upper())
    assert [(e["source_id"], e["label"], e["target_id"]) for e in pack.edges] == [
        ("a", relation, "b")
    ]
    report = predict_grammar_fit(pack.nodes, pack.edges)
    assert report["pass"] is True
    assert report["violations"] == report["missing_endpoint"] == 0


@pytest.mark.parametrize("source,target", list(product(SPACES, repeat=2)))
def test_pair_overrides_preserve_other_spaces(source, target):
    expected_stabilizes = "stabilizes" if (source, target) == ("lever", "outcome") else "optimizes"
    expected_constrains = {
        ("policy", "resource"): "restricts",
        ("concept", "outcome"): "constrains",
        ("policy", "lever"): "constrains",
    }.get((source, target), "governs")
    assert resolve_edge("STABILIZES", source, target) == (source, expected_stabilizes, target, False)
    assert resolve_edge("CONSTRAINS", source, target) == (source, expected_constrains, target, False)


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.fixture
def stored_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    sql = SQLStore(f"sqlite:///{tmp_path / 'registry.db'}")
    ensure_test_user(sql, "rule-user")
    create_pack(sql, "rule-user", "synthetic")
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "docs.db"))
    vec = SqliteVecStore(str(tmp_path / "vectors.db"), lambda texts: [[1.0, 0.0] for _ in texts], 2)
    assert graph.available and docs.available and vec.available
    nodes = [
        {"id": space, "label": f"synthetic {space}", "space": space, "node_type": node_type}
        for space, node_type in [("claim", "Claim"), ("policy", "Policy"),
                                 ("lever", "Lever"), ("evidence", "Evidence"),
                                 ("outcome", "Outcome"), ("resource", "Document")]
    ]
    edges = [
        {"source_id": source, "target_id": target, "label": relation.upper()}
        for source, target, relation in RELATIONS
    ]
    edges += [{"source_id": "policy", "target_id": "resource", "label": "CONSTRAINS"}]
    report = predict_grammar_fit(nodes, edges)
    assert report["pass"] is True
    assert report["violations"] == report["missing_endpoint"] == 0
    builder = OntologyBuilder(graph, docs, sql)
    with principal_scope(Principal(user_id="rule-user", is_local=True, disabled=False)):
        id_map = {}
        assert load_nodes("synthetic", _jsonl(tmp_path / "nodes.jsonl", nodes), builder, id_map) == (len(nodes), 0, 0)
        assert load_edges("synthetic", _jsonl(tmp_path / "edges.jsonl", edges), builder, id_map) == (len(edges), 0, 0)
        yield graph, docs, vec
    vec.close()
    docs.close()
    graph.close()
    sql._engine.dispose()


def test_loaded_edges_preserve_meaning_and_source_label(stored_rules):
    graph, _, _ = stored_rules
    expected = {(source, relation, target) for source, target, relation in RELATIONS}
    expected.add(("policy", "restricts", "resource"))
    rows = graph.export_edges(pack_id="synthetic")
    assert {(r["source_props"]["id"], r["relation"], r["target_props"]["id"]) for r in rows} == expected
    for row in rows:
        original = "CONSTRAINS" if row["relation"] == "restricts" else row["relation"].upper()
        assert row["rel_props"]["source_label"] == original


@pytest.mark.parametrize("anchor,expected", [
    ("claim", {"lever": "parameterized_by"}),
    ("policy", {"lever": "constrains", "evidence": "contradicts", "resource": "restricts"}),
])
def test_rule_anchor_query_expands_exactly_one_hop(stored_rules, anchor, expected):
    graph, docs, vec = stored_rules
    vec.add_texts([f"synthetic {anchor}"], [{"pack_id": "synthetic", "space": anchor}], [anchor])
    query = HybridQuery(vec, graph, doc_store=docs)
    try:
        result = query.query("synthetic", pack_ids=["synthetic"], graph_depth=1,
                             limit=10, use_bm25=False, use_fts=False, use_rerank=False)
        neighbors = {r.node_id: r for r in result if r.source == "graph"}
        assert set(neighbors) == set(expected)
        assert "outcome" not in neighbors
        for node_id, relation in expected.items():
            context = neighbors[node_id].graph_context
            assert context["anchor_id"] == anchor
            assert context["depth"] == 1
            assert context["relation_type"] == relation
            assert neighbors[node_id].raw_score == 0.5
    finally:
        query.shutdown_bm25()
