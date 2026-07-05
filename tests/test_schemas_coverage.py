"""
Contract tests for opencrab.schemas.loader.

Covers the schema-optional load path, the registry listing, and the
reload/cache-invalidation contract. reload_schema()'s docstring previously
claimed per-type invalidation while the implementation (functools.cache has
no per-key eviction) always clears the whole cache — the mandated fix
corrects the docstring to state the true (clear-all) behavior rather than
attempting a per-key cache, which pack_registry.py's caller does not need.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencrab.schemas import loader


@pytest.fixture(autouse=True)
def _clear_cache():
    """functools.cache is process-global; isolate tests from each other."""
    loader.load_type_schema.cache_clear()
    yield
    loader.load_type_schema.cache_clear()


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestReloadPicksUpChanges:
    def test_reload_schema_reflects_changed_yaml_on_disk(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)
        path = tmp_path / "Widget.yaml"
        path.write_text("node_type: Widget\nrequired: []\noptional: [a]\n", encoding="utf-8")

        first = loader.load_type_schema("Widget")
        assert first["optional"] == ["a"]

        path.write_text("node_type: Widget\nrequired: []\noptional: [a, b]\n", encoding="utf-8")

        # Without reload, the cached (stale) value would still be returned.
        assert loader.load_type_schema("Widget")["optional"] == ["a"]

        updated = loader.reload_schema("Widget")
        assert updated["optional"] == ["a", "b"]


class TestListRegisteredTypes:
    def test_list_registered_types_returns_sorted_stems(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)
        (tmp_path / "Zebra.yaml").write_text("node_type: Zebra\n", encoding="utf-8")
        (tmp_path / "Apple.yaml").write_text("node_type: Apple\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

        types = loader.list_registered_types()

        assert types == ["Apple", "Zebra"]


# ---------------------------------------------------------------------------
# Error: unregistered type — schema-optional pattern, no exception
# ---------------------------------------------------------------------------


class TestUnknownType:
    def test_load_type_schema_unknown_type_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)

        assert loader.load_type_schema("NoSuchType") is None

    def test_reload_schema_unknown_type_returns_none_and_clears_cache(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)
        (tmp_path / "Known.yaml").write_text("node_type: Known\n", encoding="utf-8")

        assert loader.load_type_schema("Known") is not None
        # Reloading an unrelated, unknown type still clears the whole cache
        # (documented clear-all behavior) and returns None for that type.
        assert loader.reload_schema("Unknown") is None
        assert loader.load_type_schema("Known") is not None  # reloads fine from disk


# ---------------------------------------------------------------------------
# Edge: empty types dir
# ---------------------------------------------------------------------------


class TestEmptyTypesDir:
    def test_empty_dir_list_registered_types_is_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)

        assert loader.list_registered_types() == []

    def test_missing_dir_list_registered_types_is_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path / "does-not-exist")

        assert loader.list_registered_types() == []

    def test_reload_clears_all_not_just_named_type(self, tmp_path: Path, monkeypatch):
        """Mandated-fix contract: reload_schema(node_type) clears the ENTIRE
        cache (functools.cache has no per-key eviction), not just node_type's
        entry. Verify a second, unrelated cached type is also reloaded."""
        monkeypatch.setattr(loader, "SCHEMAS_DIR", tmp_path)
        (tmp_path / "A.yaml").write_text("node_type: A\noptional: [x]\n", encoding="utf-8")
        (tmp_path / "B.yaml").write_text("node_type: B\noptional: [y]\n", encoding="utf-8")

        loader.load_type_schema("A")
        loader.load_type_schema("B")

        (tmp_path / "B.yaml").write_text("node_type: B\noptional: [y, z]\n", encoding="utf-8")

        # Reloading A must also pick up B's on-disk change, proving the
        # cache-clear is global, not scoped to "A".
        loader.reload_schema("A")
        assert loader.load_type_schema("B")["optional"] == ["y", "z"]
