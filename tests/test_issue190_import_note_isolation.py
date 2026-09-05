"""이슈 #190 회귀 게이트.

``scripts/import_obsidian_vault.py``는 노트 1건 처리 중 예외가 나면 전체
vault 임포트를 중단시켰다. 이 파일은 이슈 #190 설계(3라운드 검증 끝에
합의됨)가 못박은 치명/항목 국소 분류가 실제로 그렇게 동작하는지 잠근다.

핵심 대조군은 "같은 예외 타입이라도 격리 여부가 반대인 두 경우"다:

- ``ValueError``: identity probe가 진짜 "foreign" id 충돌로 확정한 것은
  항목 국소(격리)이지만, identity probe 자체가 backend 장애로
  "unverifiable"을 반환해 생긴 ``ValueError``(고정 문구
  "cannot verify existing ownership on this backend" 포함)는 치명(전파).
- ``OSError``: ``_collect_notes``에서 파일 1건을 못 읽는 것은 항목 국소
  (격리)이지만, 쓰기 루프 안에서(``LocalDocStore.upsert_source`` 등) 나는
  것은 스토어 전체의 쓰기 불가를 뜻하므로 치명(전파).

``tests/test_store_receipt_callers.py``의 ``_load_module_from_path``/
``_run_import`` 관례를 재사용하되, 이 파일 전용 ``sys.modules`` 이름으로
로드해 다른 테스트 파일과 격리한다.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencrab.common.graph_identity import GraphWriteUnavailable, NodeIdentityConflict
from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

OK_RECEIPT = {"stores": {"graph": "ok", "docs": "ok (id=d1)", "sql": "ok"}}
FAIL_RECEIPT = {"stores": {"graph": "error: boom", "docs": "ok (id=d1)", "sql": "ok"}}

_UNVERIFIABLE_MARKER = "cannot verify existing ownership on this backend"


def _load_module_from_path(name: str, path: Path) -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture()
def import_mod():
    return _load_module_from_path(
        "_o190_import_obsidian_vault", SCRIPTS_DIR / "import_obsidian_vault.py"
    )


def _write_note(tmp_path: Path, rel_path: str, text: str = "content") -> Path:
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _wire_stores(import_mod, monkeypatch, fake_builder):
    monkeypatch.setattr(import_mod, "OntologyBuilder", MagicMock(return_value=fake_builder))
    monkeypatch.setattr(import_mod, "Neo4jStore", MagicMock())
    monkeypatch.setattr(import_mod, "LocalDocStore", MagicMock())
    monkeypatch.setattr(import_mod, "SQLStore", MagicMock())


def _run_unlocked(import_mod, tmp_path, notes):
    from opencrab.auth import Principal, principal_scope

    with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
        return import_mod._import_vault_unlocked(
            vault_root=tmp_path,
            neo4j_uri="bolt://x",
            neo4j_user="u",
            neo4j_password="p",
            neo4j_database="d",
            local_data_dir=tmp_path,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# 대조군: 전부 성공 (기준 10)
# ---------------------------------------------------------------------------


def test_all_notes_succeed_no_failures(import_mod, tmp_path, monkeypatch):
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
    ]
    fake_builder = MagicMock()
    fake_builder.add_node.return_value = OK_RECEIPT
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    result = _run_unlocked(import_mod, tmp_path, notes)

    assert result["notes"] == 2
    assert result["failed_notes"] == []
    assert result["failed_items"] == []
    assert result["notes_failed"] == 0
    assert result["node_write_failures"] == 0
    assert result["edge_write_failures"] == 0


# ---------------------------------------------------------------------------
# 기준 1: 항목 국소 예외가 나머지 노트를 막지 않는다
# ---------------------------------------------------------------------------


def test_node_identity_conflict_isolates_one_note(import_mod, tmp_path, monkeypatch):
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "c.md")),
    ]

    def add_node_side_effect(*args, **kwargs):
        if kwargs.get("properties", {}).get("source_path") == "b.md" and kwargs.get("node_type") == "Document":
            raise NodeIdentityConflict("b.md conflicts with another pack's node")
        return OK_RECEIPT

    fake_builder = MagicMock()
    fake_builder.add_node.side_effect = add_node_side_effect
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    result = _run_unlocked(import_mod, tmp_path, notes)

    assert result["notes"] == 3
    assert result["notes_failed"] == 1
    assert result["failed_notes"][0]["item"] == "note:b.md"
    assert "conflicts with another pack" in result["failed_notes"][0]["error"]
    # a.md, c.md 는 각각 add_node 3건씩 전부 성공했다 (b.md 는 첫 add_node에서
    # 예외가 나 나머지 처리가 스킵되므로 3건 중 0건만 기록됨).
    assert result["nodes_written"] == 6


# ---------------------------------------------------------------------------
# 기준 2/3: 치명 예외는 즉시 전체 중단된다
# ---------------------------------------------------------------------------


def test_graph_write_unavailable_aborts_entire_run(import_mod, tmp_path, monkeypatch):
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
    ]

    calls = []

    def add_node_side_effect(*args, **kwargs):
        calls.append(kwargs.get("properties", {}).get("source_path"))
        if kwargs.get("properties", {}).get("source_path") == "a.md" and kwargs.get("node_type") == "Document":
            raise GraphWriteUnavailable("graph backend unreachable")
        return OK_RECEIPT

    fake_builder = MagicMock()
    fake_builder.add_node.side_effect = add_node_side_effect
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    with pytest.raises(GraphWriteUnavailable):
        _run_unlocked(import_mod, tmp_path, notes)

    # b.md 는 전혀 처리되지 않았다 (즉시 중단).
    assert "b.md" not in calls


def test_pack_registry_unavailable_runtime_error_aborts(import_mod, tmp_path, monkeypatch):
    """기준 3: authorize()/resolve_write_pack 이 던지는 순수 RuntimeError."""
    notes = [import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md"))]

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = OK_RECEIPT
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    import opencrab.pack.ownership as ownership_mod

    def boom(*args, **kwargs):
        raise RuntimeError("pack registry unavailable; refusing the write (ownership cannot be verified)")

    monkeypatch.setattr(ownership_mod, "resolve_write_pack", boom)

    from opencrab.auth import Principal, principal_scope

    with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
        with pytest.raises(RuntimeError, match="pack registry unavailable"):
            import_mod._import_vault_unlocked(
                vault_root=tmp_path,
                neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p", neo4j_database="d",
                local_data_dir=tmp_path,
                notes=notes,
            )


# ---------------------------------------------------------------------------
# 기준 4: PackNotFoundError / PackForbiddenError 도 즉시 전체 중단
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_cls", [PackNotFoundError, PackForbiddenError])
def test_pack_ownership_errors_abort_entire_run(import_mod, tmp_path, monkeypatch, exc_cls):
    notes = [import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md"))]

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = OK_RECEIPT
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    import opencrab.pack.ownership as ownership_mod

    def boom(*args, **kwargs):
        raise exc_cls("no such pack")

    monkeypatch.setattr(ownership_mod, "resolve_write_pack", boom)

    from opencrab.auth import Principal, principal_scope

    with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
        with pytest.raises(exc_cls):
            import_mod._import_vault_unlocked(
                vault_root=tmp_path,
                neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p", neo4j_database="d",
                local_data_dir=tmp_path,
                notes=notes,
            )


# ---------------------------------------------------------------------------
# 기준 6: identity-probe-unverifiable ValueError 는 격리되지 않고 전체 중단된다
# (v2 지적 1번의 직접 대조군)
# ---------------------------------------------------------------------------


def test_identity_unverifiable_value_error_aborts_not_isolated(import_mod, tmp_path, monkeypatch):
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
    ]

    calls = []

    def add_node_side_effect(*args, **kwargs):
        calls.append(kwargs.get("properties", {}).get("source_path"))
        if kwargs.get("properties", {}).get("source_path") == "a.md" and kwargs.get("node_type") == "Document":
            raise ValueError(f"node a.md: {_UNVERIFIABLE_MARKER}")
        return OK_RECEIPT

    fake_builder = MagicMock()
    fake_builder.add_node.side_effect = add_node_side_effect
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    with pytest.raises(ValueError, match=_UNVERIFIABLE_MARKER):
        _run_unlocked(import_mod, tmp_path, notes)

    assert "b.md" not in calls


def test_identity_foreign_value_error_is_isolated_by_contrast(import_mod, tmp_path, monkeypatch):
    """같은 ValueError 타입이라도 고정 문구가 없으면(진짜 foreign 충돌) 격리된다."""
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
    ]

    def add_node_side_effect(*args, **kwargs):
        if kwargs.get("properties", {}).get("source_path") == "a.md" and kwargs.get("node_type") == "Document":
            raise ValueError("node a.md: id already owned by another pack")
        return OK_RECEIPT

    fake_builder = MagicMock()
    fake_builder.add_node.side_effect = add_node_side_effect
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    result = _run_unlocked(import_mod, tmp_path, notes)

    assert result["notes_failed"] == 1
    assert result["failed_notes"][0]["item"] == "note:a.md"


# ---------------------------------------------------------------------------
# 기준 7 vs 11: 쓰기 루프의 OSError 는 전파, 수집 루프의 OSError 는 격리
# ---------------------------------------------------------------------------


def test_write_phase_os_error_aborts_entire_run(import_mod, tmp_path, monkeypatch):
    notes = [
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "a.md")),
        import_mod.build_note_record(tmp_path, _write_note(tmp_path, "b.md")),
    ]

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = OK_RECEIPT
    fake_builder.add_edge.return_value = OK_RECEIPT
    monkeypatch.setattr(import_mod, "OntologyBuilder", MagicMock(return_value=fake_builder))
    monkeypatch.setattr(import_mod, "Neo4jStore", MagicMock())
    monkeypatch.setattr(import_mod, "SQLStore", MagicMock())

    fake_docs = MagicMock()
    fake_docs.upsert_source.side_effect = OSError("No space left on device")
    monkeypatch.setattr(import_mod, "LocalDocStore", MagicMock(return_value=fake_docs))

    with pytest.raises(OSError, match="No space left"):
        _run_unlocked(import_mod, tmp_path, notes)

    # 첫 노트에서 스토어 자체가 못 쓰는 상태이므로 두 번째 노트의 upsert_source는
    # 호출되지 않는다 (즉시 중단).
    assert fake_docs.upsert_source.call_count == 1


def test_collect_phase_os_error_is_isolated(import_mod, tmp_path, monkeypatch):
    _write_note(tmp_path, "a.md")
    _write_note(tmp_path, "b.md")
    _write_note(tmp_path, "c.md")

    real_build_note_record = import_mod.build_note_record

    def flaky_build(root, path):
        if path.name == "b.md":
            raise OSError("permission denied")
        return real_build_note_record(root, path)

    monkeypatch.setattr(import_mod, "build_note_record", flaky_build)

    notes, failed = import_mod._collect_notes(tmp_path)

    assert {n.rel_path for n in notes} == {"a.md", "c.md"}
    assert len(failed) == 1
    assert failed[0]["item"] == "note:b.md"
    assert "permission denied" in failed[0]["error"]


# ---------------------------------------------------------------------------
# 기준 5: #209 회귀는 수정 없이 그대로 통과한다 (여기서는 참조만; 실제 확인은
# tests/test_issue209_docstore_corrupt_import.py 자체 실행으로 한다)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 기준 12: 폴더/태그/미해결링크 실패가 서로 독립적으로 failed_items 에 담긴다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failing_loop", "match_props", "expected_item"),
    [
        pytest.param(
            "folder",
            lambda props: props.get("obsidian_path") == "sub",
            "folder:sub",
            id="folder-fails-tag-and-link-continue",
        ),
        pytest.param(
            "tag",
            lambda props: props.get("obsidian_kind") == "tag",
            "tag:mytag",
            id="tag-fails-folder-and-link-continue",
        ),
        pytest.param(
            "link",
            lambda props: props.get("obsidian_kind") == "wikilink_stub",
            "link:missing-note",
            id="link-fails-folder-and-tag-continue",
        ),
    ],
)
def test_folder_tag_link_failures_are_independent(
    import_mod, tmp_path, monkeypatch, failing_loop, match_props, expected_item,
):
    """폴더, 태그, 미해결링크 루프는 각각 독립된 격리 블록이다(기준 12).

    세 루프 중 하나만 실패시켜도 나머지 둘은 항상 성공해야 한다. 실패를
    돌아가며 세 위치 모두에 주입해 어느 한 방향만 증명하는 일이 없게 한다.
    """
    note = import_mod.build_note_record(
        tmp_path, _write_note(tmp_path, "sub/note.md", text="#mytag\n\n[[missing-note]]"),
    )
    assert note.folders == ["sub"]
    assert note.tags == ["mytag"]
    assert note.wikilinks == ["missing-note"]

    def add_node_side_effect(*args, **kwargs):
        props = kwargs.get("properties", {})
        if match_props(props):
            raise NodeIdentityConflict(f"{failing_loop} conflicts")
        return OK_RECEIPT

    fake_builder = MagicMock()
    fake_builder.add_node.side_effect = add_node_side_effect
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    result = _run_unlocked(import_mod, tmp_path, [note])

    assert result["failed_items"] == [{"item": expected_item, "error": f"{failing_loop} conflicts"}]
    # 실패하지 않은 나머지 두 루프는 그 실패와 무관하게 계속 처리되어 성공한다.
    assert result["folder_topics"] == 1
    assert result["tag_topics"] == 1
    assert result["unresolved_link_topics"] == 1
    assert result["node_write_failures"] == 0
    # 실패한 노드 1건을 뺀 나머지(폴더/태그/링크/Document/TextUnit/노트 Topic)가
    # 실제로 기록됐음을 카운트로 직접 확인한다(간접 증명인 위 세 단언을 보강).
    assert result["nodes_written"] == 5


# ---------------------------------------------------------------------------
# 기준 13: 기존 store-receipt 실패 + 신규 격리 실패가 함께 나도 종료 코드는 3
# ---------------------------------------------------------------------------


def test_main_exits_3_when_receipt_and_isolation_failures_combine(import_mod, monkeypatch):
    combined_result = {
        "notes": 2,
        "nodes_written": 1,
        "edges_written": 1,
        "node_write_failures": 1,
        "edge_write_failures": 0,
        "folder_topics": 0,
        "tag_topics": 0,
        "unresolved_link_topics": 0,
        "failed_items": [],
        "failed_notes": [{"item": "note:b.md", "error": "conflict"}],
        "notes_failed": 1,
    }
    monkeypatch.setattr(import_mod, "import_vault", MagicMock(return_value=combined_result))
    monkeypatch.setattr(
        sys, "argv",
        ["import_obsidian_vault.py", "--vault-root", "/tmp/vault"],
    )

    with pytest.raises(SystemExit) as excinfo:
        import_mod.main()

    assert excinfo.value.code == 3


def test_main_exits_cleanly_when_all_succeed(import_mod, monkeypatch, capsys):
    clean_result = {
        "notes": 2,
        "nodes_written": 6,
        "edges_written": 4,
        "node_write_failures": 0,
        "edge_write_failures": 0,
        "folder_topics": 0,
        "tag_topics": 0,
        "unresolved_link_topics": 0,
        "failed_items": [],
        "failed_notes": [],
        "notes_failed": 0,
    }
    monkeypatch.setattr(import_mod, "import_vault", MagicMock(return_value=clean_result))
    monkeypatch.setattr(
        sys, "argv",
        ["import_obsidian_vault.py", "--vault-root", "/tmp/vault"],
    )

    import_mod.main()  # SystemExit 을 던지지 않아야 한다

    # 기준 10: 대조군 실행의 stderr 에는 WARNING/FAILED 계열 출력이 전혀 없다.
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err
    assert "FAILED" not in captured.err


# ---------------------------------------------------------------------------
# 기준 14: import_vault() 는 여전히 write_lock 획득 이전에 _collect_notes 를 호출한다
# ---------------------------------------------------------------------------


def test_import_vault_collects_before_acquiring_lock(import_mod, tmp_path, monkeypatch):
    lock_spy = MagicMock()

    def collect_boom(vault_root):
        raise RuntimeError("collect failed before any lock")

    monkeypatch.setattr(import_mod, "_collect_notes", collect_boom)
    monkeypatch.setattr(import_mod, "write_lock", lock_spy)

    with pytest.raises(RuntimeError, match="collect failed before any lock"):
        import_mod.import_vault(
            vault_root=tmp_path,
            neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p", neo4j_database="d",
            local_data_dir=tmp_path,
        )

    lock_spy.assert_not_called()


# ---------------------------------------------------------------------------
# 기준 15: import_vault() 직접 호출 — _collect_notes 자체의 실패가 병합된다
# ---------------------------------------------------------------------------


def test_import_vault_merges_collection_failures(import_mod, tmp_path, monkeypatch):
    _write_note(tmp_path, "a.md")
    _write_note(tmp_path, "b.md")

    real_build_note_record = import_mod.build_note_record

    def flaky_build(root, path):
        if path.name == "b.md":
            raise OSError("cannot read b.md")
        return real_build_note_record(root, path)

    monkeypatch.setattr(import_mod, "build_note_record", flaky_build)

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = OK_RECEIPT
    fake_builder.add_edge.return_value = OK_RECEIPT
    _wire_stores(import_mod, monkeypatch, fake_builder)

    import opencrab.auth as auth_mod
    from opencrab.auth import Principal

    monkeypatch.setattr(
        auth_mod, "require_local_principal",
        lambda: Principal(user_id="test-user", is_local=True, disabled=False),
    )

    result = import_mod.import_vault(
        vault_root=tmp_path,
        neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p", neo4j_database="d",
        local_data_dir=tmp_path,
    )

    assert result["notes"] == 1
    assert result["failed_notes"] == [{"item": "note:b.md", "error": "cannot read b.md"}]
    assert result["notes_failed"] == 1
