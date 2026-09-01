"""Mapping-file contract of ``scripts/migrate_graph_identity.py`` (issue #258).

The merge branch of the CLI mapping parser used to demand a source object with
exactly ``node_type`` and ``node_id`` while reading a third ``digest`` key from
the very same object, so no JSON document could describe a merge.  These tests
pin the canonical file format down and prove that parsing a mapping file yields
byte-identical plan bytes to the equivalent in-process request.

Every fixture lives in an OS temporary directory; no live graph is touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from opencrab.common.graph_identity import (
    DryRunMigrationRequest,
    ExplicitMerge,
    ExplicitRename,
    GraphMigrationConflict,
    LegacyNodeKey,
    PropertyResolution,
)
from opencrab.stores.local_graph_store import LocalGraphStore
from tests.issue80_migration import FixtureHandle

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import migrate_graph_identity as cli  # noqa: E402

AGENT_A = LegacyNodeKey("Agent", "a")
PERSON_A = LegacyNodeKey("Person", "a")
PERSON_B = LegacyNodeKey("Person", "b")


class _Graph:
    """A disposable legacy graph with one duplicate bare node id."""

    def __init__(self, fixture: FixtureHandle) -> None:
        self.fixture = fixture
        self.db_path = fixture.db_path
        store = LocalGraphStore(str(fixture.db_path))
        try:
            inventory = store.inspect_graph_identity()
        finally:
            store.close()
        self.source_fingerprint = inventory.source_fingerprint
        self.digests = {row.key: row.digest for row in inventory.nodes}

    def plan_bytes(
        self,
        mappings: tuple[Any, ...],
        resolutions: tuple[PropertyResolution, ...] = (),
    ) -> bytes:
        store = LocalGraphStore(str(self.db_path))
        try:
            receipt = store.migrate_graph_identity(
                DryRunMigrationRequest(self.source_fingerprint, mappings, resolutions)
            )
        finally:
            store.close()
        return receipt.plan_bytes

    def merge(self) -> ExplicitMerge:
        return ExplicitMerge(
            ((AGENT_A, self.digests[AGENT_A]), (PERSON_A, self.digests[PERSON_A])),
            "a", "Person", None, None,
        )

    def merge_json(self) -> dict[str, Any]:
        return {
            "kind": "merge",
            "sources": [
                {"source": _key_json(AGENT_A), "source_digest": self.digests[AGENT_A]},
                {"source": _key_json(PERSON_A), "source_digest": self.digests[PERSON_A]},
            ],
            "target": {"node_id": "a", "node_type": "Person"},
        }


def _key_json(key: LegacyNodeKey) -> dict[str, str]:
    return {"node_type": key.node_type, "node_id": key.node_id}


@pytest.fixture()
def graph() -> Iterator[_Graph]:
    fixture = FixtureHandle.create()
    fixture.create_legacy()
    fixture.seed(nodes=(
        ("Agent", "a", None, {"name": "same"}),
        ("Person", "a", None, {"name": "same"}),
        ("Person", "b", None, {"name": "bee"}),
    ))
    try:
        yield _Graph(fixture)
    finally:
        shutil.rmtree(fixture.root, ignore_errors=True)


def _write(tmp_path: Path, payload: dict[str, Any], name: str = "mapping.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _cli_plan_bytes(graph: _Graph, mapping: Path, tmp_path: Path) -> bytes:
    plan_out = tmp_path / f"{mapping.stem}.plan"
    assert cli.main([
        "--db-path", str(graph.db_path),
        "--mapping-file", str(mapping),
        "--plan-out", str(plan_out),
    ]) == 0
    return plan_out.read_bytes()


def test_merge_mapping_file_is_equivalent_to_in_process_request(
    graph: _Graph, tmp_path: Path
) -> None:
    """The documented merge format parses into the same plan as ExplicitMerge."""
    mapping = _write(tmp_path, {"mappings": [graph.merge_json()]})
    assert _cli_plan_bytes(graph, mapping, tmp_path) == graph.plan_bytes((graph.merge(),))


def test_merge_mapping_file_runs_as_a_process(graph: _Graph, tmp_path: Path) -> None:
    """A real process run emits a receipt and the same canonical plan bytes."""
    mapping = _write(tmp_path, {"mappings": [graph.merge_json()]})
    plan_out = tmp_path / "subprocess.plan"
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "migrate_graph_identity.py"),
            "--db-path", str(graph.db_path),
            "--mapping-file", str(mapping),
            "--plan-out", str(plan_out),
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["phase"] == "dry_run"
    merges = [entry for entry in receipt["mapping_result"] if entry["kind"] == "merge"]
    assert len(merges) == 1
    # A receipt flattens each source, so its mappings are not a mapping file:
    # pasting one back in is what issue #258 was about.
    assert all(set(source) == {"node_type", "node_id", "digest"} for source in merges[0]["sources"])
    assert plan_out.read_bytes() == graph.plan_bytes((graph.merge(),))


def test_mixed_rename_and_merge_mapping_file(graph: _Graph, tmp_path: Path) -> None:
    """One file may carry both mapping kinds without either shape leaking."""
    rename_json = {
        "kind": "rename",
        "source": _key_json(PERSON_B),
        "source_digest": graph.digests[PERSON_B],
        "target": {"node_id": "bee", "node_type": "Person"},
    }
    mapping = _write(tmp_path, {"mappings": [graph.merge_json(), rename_json]})
    expected = graph.plan_bytes((
        graph.merge(),
        ExplicitRename(PERSON_B, graph.digests[PERSON_B], "bee", "Person", None, None),
    ))
    assert _cli_plan_bytes(graph, mapping, tmp_path) == expected


def test_merge_mapping_file_with_property_resolution(graph: _Graph, tmp_path: Path) -> None:
    """Property resolutions keep their own nested source shape alongside merges."""
    mapping = _write(tmp_path, {
        "mappings": [graph.merge_json()],
        "property_resolutions": [{
            "source": _key_json(AGENT_A),
            "source_property": "name",
            "source_value": "same",
            "target_property": "alias",
        }],
    })
    expected = graph.plan_bytes(
        (graph.merge(),), (PropertyResolution(AGENT_A, "name", "same", "alias"),)
    )
    assert _cli_plan_bytes(graph, mapping, tmp_path) == expected


def test_rename_only_mapping_file_does_not_regress(graph: _Graph, tmp_path: Path) -> None:
    """The rename shape that already worked keeps producing the same plan."""
    mapping = _write(tmp_path, {"mappings": [
        {
            "kind": "rename",
            "source": _key_json(AGENT_A),
            "source_digest": graph.digests[AGENT_A],
            "target": {"node_id": "renamed-a", "node_type": "Agent"},
        },
        {
            "kind": "rename",
            "source": _key_json(PERSON_A),
            "source_digest": graph.digests[PERSON_A],
            "target": {"node_id": "a", "node_type": "Person"},
        },
    ]})
    expected = graph.plan_bytes((
        ExplicitRename(AGENT_A, graph.digests[AGENT_A], "renamed-a", "Agent", None, None),
        ExplicitRename(PERSON_A, graph.digests[PERSON_A], "a", "Person", None, None),
    ))
    assert _cli_plan_bytes(graph, mapping, tmp_path) == expected


def test_mapping_file_members_are_optional(tmp_path: Path) -> None:
    """Both list members may be absent; the parser then plans nothing explicit."""
    assert cli._mapping_file(_write(tmp_path, {})) == ((), ())


def test_single_source_merge_is_left_for_the_store_to_reject(
    graph: _Graph, tmp_path: Path
) -> None:
    """The parser accepts merge cardinality and the store owns the rule."""
    mapping = _write(tmp_path, {"mappings": [{
        "kind": "merge",
        "sources": [{"source": _key_json(AGENT_A), "source_digest": graph.digests[AGENT_A]}],
        "target": {"node_id": "a", "node_type": "Person"},
    }]})
    mappings, resolutions = cli._mapping_file(mapping)
    assert resolutions == ()
    assert len(mappings[0].sources) == 1
    with pytest.raises(GraphMigrationConflict, match="merge requires at least two sources"):
        cli.main([
            "--db-path", str(graph.db_path),
            "--mapping-file", str(mapping),
        ])


_SOURCE_ENTRY = "merge source must contain exactly source and source_digest"
_DIGEST = "source_digest must be a non-empty string"
_KEY = _key_json(AGENT_A)
_OTHER_KEY = _key_json(PERSON_A)
_HEX = "0" * 64


def _merge(sources: Any) -> dict[str, Any]:
    return {
        "kind": "merge",
        "sources": sources,
        "target": {"node_id": "a", "node_type": "Person"},
    }


def _rename(**overrides: Any) -> dict[str, Any]:
    item = {
        "kind": "rename",
        "source": _KEY,
        "source_digest": _HEX,
        "target": {"node_id": "a", "node_type": "Agent"},
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(("payload", "message"), [
    pytest.param(
        {"mappings": [_merge([{**_KEY, "digest": _HEX}, {**_OTHER_KEY, "digest": _HEX}])]},
        _SOURCE_ENTRY, id="merge-flat-source-with-digest",
    ),
    pytest.param(
        {"mappings": [_merge([{"source": _KEY}, {"source": _OTHER_KEY}])]},
        _SOURCE_ENTRY, id="merge-source-digest-missing",
    ),
    pytest.param(
        {"mappings": [_merge([{"source": _KEY, "source_digest": _HEX, "note": "x"}])]},
        _SOURCE_ENTRY, id="merge-source-extra-key",
    ),
    pytest.param(
        {"mappings": [_merge([{"source": _KEY, "source_digest": 7}])]},
        _DIGEST, id="merge-source-digest-not-a-string",
    ),
    pytest.param(
        {"mappings": [_merge([{"source": _KEY, "source_digest": ""}])]},
        _DIGEST, id="merge-source-digest-empty",
    ),
    pytest.param(
        {"mappings": [_merge({"source": _KEY, "source_digest": _HEX})]},
        "sources must be a list", id="merge-sources-not-a-list",
    ),
    pytest.param(
        {"mappings": {"kind": "merge"}},
        "mappings must be a list", id="mappings-not-a-list",
    ),
    pytest.param(
        {"mappings": [], "property_resolutions": {"source": _KEY}},
        "property_resolutions must be a list", id="property-resolutions-not-a-list",
    ),
    pytest.param(
        {"mappings": [_rename(source_digest=None)]},
        _DIGEST, id="rename-source-digest-null",
    ),
    pytest.param(
        {"mappings": [_rename(source_digest=7)]},
        _DIGEST, id="rename-source-digest-not-a-string",
    ),
    pytest.param(
        {"mappings": [_rename(source_digest="")]},
        _DIGEST, id="rename-source-digest-empty",
    ),
])
def test_malformed_mapping_files_are_rejected_with_a_named_reason(
    tmp_path: Path, payload: dict[str, Any], message: str
) -> None:
    """Every rejected shape names the field it is about, never a foreign one."""
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match=message):
        cli._mapping_file(path)


def test_rename_source_digest_key_absence_is_a_value_error(tmp_path: Path) -> None:
    """A missing rename digest key reports the field, not a bare KeyError."""
    item = _rename()
    del item["source_digest"]
    path = _write(tmp_path, {"mappings": [item]})
    with pytest.raises(ValueError, match=_DIGEST):
        cli._mapping_file(path)
