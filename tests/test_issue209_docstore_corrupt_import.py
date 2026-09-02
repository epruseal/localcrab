"""Issue #209 regression: ``scripts/import_obsidian_vault.py`` calls
``docs.upsert_source(...)`` directly, outside ``OntologyBuilder`` (no
``except`` around that call). With a corrupt ``sources.json``, the whole
import run must abort with ``CorruptCollectionError`` and the corrupt file
must stay byte-identical -- not be silently replaced by a fresh one-row
file, which is what the old ``_load`` (catch-and-return-``{}``) let happen.

Reuses the module-loading + mock pattern of
``tests/test_store_receipt_callers.py`` (``_load_module_from_path``,
``_run_import``'s note/monkeypatch setup) but swaps ``LocalDocStore`` for a
real, pre-corrupted instance instead of a ``MagicMock``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencrab.stores.local_doc_store import CorruptCollectionError, LocalDocStore

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

GARBAGE_MIDDLE = b'{"a": {"b": 1}, XXXX not json XXXX "c": 2}'


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


def test_import_vault_aborts_on_corrupt_sources_json(tmp_path, monkeypatch):
    note_path = tmp_path / "note.md"
    note_path.write_text("Just a plain note with no links or tags.", encoding="utf-8")

    import_mod = _load_module_from_path(
        "_o209_import_obsidian_vault", SCRIPTS_DIR / "import_obsidian_vault.py"
    )

    note = import_mod.build_note_record(tmp_path, note_path)
    assert note.folders == []
    assert note.tags == []
    assert note.wikilinks == []

    real_docs = LocalDocStore(str(tmp_path / "docs"))
    sources_path = real_docs._collection_path("sources")
    with open(sources_path, "wb") as f:
        f.write(GARBAGE_MIDDLE)

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = {"stores": {"graph": "ok", "docs": "ok", "sql": "ok"}}
    fake_builder.add_edge.return_value = {"stores": {"graph": "ok", "docs": "ok", "sql": "ok"}}

    monkeypatch.setattr(import_mod, "OntologyBuilder", MagicMock(return_value=fake_builder))
    monkeypatch.setattr(import_mod, "Neo4jStore", MagicMock())
    monkeypatch.setattr(import_mod, "LocalDocStore", lambda *a, **kw: real_docs)
    monkeypatch.setattr(import_mod, "SQLStore", MagicMock())

    from opencrab.auth import Principal, principal_scope

    with principal_scope(Principal(user_id="test-user", is_local=True, disabled=False)):
        with pytest.raises(CorruptCollectionError) as excinfo:
            import_mod._import_vault_unlocked(
                vault_root=tmp_path,
                neo4j_uri="bolt://x",
                neo4j_user="u",
                neo4j_password="p",
                neo4j_database="d",
                local_data_dir=tmp_path,
                notes=[note],
            )

    assert excinfo.value.collection == "sources"

    with open(sources_path, "rb") as f:
        after = f.read()
    assert after == GARBAGE_MIDDLE
