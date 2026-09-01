"""#109: schema pack type/pack names must never address a path outside their directory.

Every test here works ONLY inside ``tmp_path``: ``_TYPES_DIR``/``_PACKS_DIR``
are monkeypatched to temporary directories, so the repository's real
``opencrab/schemas/types/`` is never read or written.

The "nothing escaped" assertion is a before/after snapshot of the WHOLE tmp
tree, not a bare "no file outside the types dir" check -- the pack manifests
themselves already live outside the types dir, so that weaker check would
pass vacuously. The snapshot records symlinks by their target
(``os.readlink``) rather than by their content, so it never follows a link
and therefore also catches a write that lands on a link's target.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml

from opencrab.schemas import loader, pack_registry

# --------------------------------------------------------------------------
# snapshot helpers
# --------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, str]:
    """Map every entry under *root* to a content-independent fingerprint.

    Symlinks are recorded as ``symlink:<target>`` without being followed, so
    a write that escapes *through* a link shows up as a change to the link's
    target entry (or as a brand-new entry) rather than being masked.
    """
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for entry in list(dirnames) + list(filenames):
            path = Path(dirpath) / entry
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = f"symlink:{os.readlink(path)}"
            elif path.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def assert_outside_unchanged(before: dict[str, str], after: dict[str, str], inside: str) -> None:
    """Assert nothing changed outside the ``inside`` subtree (a relative prefix)."""
    prefix = inside.rstrip("/") + "/"

    def outside(snap: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in snap.items() if k != inside.rstrip("/") and not k.startswith(prefix)}

    assert outside(after) == outside(before), (
        "an operation touched the tree outside "
        f"{inside!r}: added/changed={ {k: v for k, v in outside(after).items() if before.get(k) != v} }, "
        f"removed={sorted(set(outside(before)) - set(outside(after)))}"
    )


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def packs_env(tmp_path, monkeypatch):
    """Isolated types/ and packs/ directories wired into pack_registry."""
    types_dir = tmp_path / "types"
    packs_dir = tmp_path / "packs"
    types_dir.mkdir()
    packs_dir.mkdir()
    monkeypatch.setattr(pack_registry, "_TYPES_DIR", types_dir)
    monkeypatch.setattr(pack_registry, "_PACKS_DIR", packs_dir)
    monkeypatch.setattr(loader, "SCHEMAS_DIR", types_dir)
    loader.load_type_schema.cache_clear()
    yield tmp_path, types_dir, packs_dir
    loader.load_type_schema.cache_clear()


def write_manifest(packs_dir: Path, name: str, types: list[str]) -> None:
    (packs_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"name": name, "version": "1.0.0", "spaces": ["concept"], "types": types},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


# Names that must never reach the filesystem as a path component.
UNSAFE_NAMES = ["../escape", "a/b", "a\\b", "", ".", "..", "a\x00b", "a:b", "C:evil"]


# --------------------------------------------------------------------------
# 1. normal path keeps working
# --------------------------------------------------------------------------


class TestNormalTypeNamesStillWork:
    def test_ascii_and_unicode_type_names_install_inside_types_dir(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal", "한글타입"])

        result = pack_registry.install_pack("ok")

        assert "error" not in result, result
        assert sorted(result["created"]) == sorted(["Normal", "한글타입"])
        assert (types_dir / "Normal.yaml").is_file()
        assert (types_dir / "한글타입.yaml").is_file()
        # Round-trips through the loader, i.e. the file is where the loader looks.
        assert loader.load_yaml_schema(types_dir, "한글타입")["type"] == "한글타입"

    def test_normal_type_names_uninstall_cleanly(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal", "한글타입"])
        pack_registry.install_pack("ok")

        result = pack_registry.uninstall_pack("ok")

        assert "error" not in result, result
        assert sorted(result["removed"]) == sorted(["Normal", "한글타입"])
        assert not (types_dir / "Normal.yaml").exists()
        assert not (types_dir / "한글타입.yaml").exists()

    def test_every_bundled_pack_type_name_is_accepted(self):
        """The three shipped packs must not be rejected by the new check.

        Type names are read from the manifests rather than hard-coded, so a
        pack gaining a type is covered without editing this test.
        """
        packs_dir = Path(pack_registry.__file__).parent / "packs"
        manifests = sorted(packs_dir.glob("*.yaml"))
        assert manifests, "expected the repository to ship schema pack manifests"
        for manifest in manifests:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            types = data.get("types") or []
            assert types, f"{manifest.name} declares no types"
            for node_type in types:
                assert loader.safe_schema_name(node_type), (
                    f"bundled pack {data['name']!r} type {node_type!r} was rejected"
                )


# --------------------------------------------------------------------------
# 2. install must not write outside the types dir
# --------------------------------------------------------------------------


class TestInstallRejectsEscapingTypeNames:
    @pytest.mark.parametrize("bad", UNSAFE_NAMES)
    def test_unsafe_type_name_is_rejected_and_writes_nothing(self, packs_env, bad):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "bad", [bad])
        before = snapshot(tmp_root)

        result = pack_registry.install_pack("bad")

        assert "error" in result, result
        assert not result.get("created")
        assert_outside_unchanged(before, snapshot(tmp_root), "types")
        assert list(types_dir.iterdir()) == []

    def test_absolute_type_name_is_rejected(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        # Points inside tmp so a regression cannot touch anything real.
        write_manifest(packs_dir, "abs", [str(tmp_root / "abs_escape")])
        before = snapshot(tmp_root)

        result = pack_registry.install_pack("abs")

        assert "error" in result, result
        assert not (tmp_root / "abs_escape.yaml").exists()
        assert_outside_unchanged(before, snapshot(tmp_root), "types")

    def test_one_unsafe_name_rejects_the_whole_manifest(self, packs_env):
        """No partial install: a safe type in a bad manifest is not written either."""
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "mixed", ["Normal", "../escape", "한글타입"])
        before = snapshot(tmp_root)

        result = pack_registry.install_pack("mixed")

        assert "error" in result, result
        assert "../escape" in result["error"]
        assert not (types_dir / "Normal.yaml").exists()
        assert not (types_dir / "한글타입.yaml").exists()
        assert_outside_unchanged(before, snapshot(tmp_root), "types")


# --------------------------------------------------------------------------
# 3. uninstall must not delete outside the types dir
# --------------------------------------------------------------------------


class TestUninstallRejectsEscapingTypeNames:
    def test_forced_uninstall_does_not_delete_outside_the_types_dir(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        victim = tmp_root / "victim.yaml"
        victim.write_text("data that lives outside the types directory\n", encoding="utf-8")
        write_manifest(packs_dir, "delpack", ["../victim"])
        before = snapshot(tmp_root)

        result = pack_registry.uninstall_pack("delpack", force=True)

        assert "error" in result, result
        assert not result.get("removed")
        assert victim.exists(), "uninstall deleted a file outside the types directory"
        assert_outside_unchanged(before, snapshot(tmp_root), "types")

    def test_one_unsafe_name_rejects_the_whole_uninstall(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        pack_registry.install_pack("ok")
        assert (types_dir / "Normal.yaml").is_file()

        write_manifest(packs_dir, "mixed", ["Normal", "../victim"])
        result = pack_registry.uninstall_pack("mixed", force=True)

        assert "error" in result, result
        assert not result.get("removed")
        assert (types_dir / "Normal.yaml").is_file(), "a safe type was deleted despite the refusal"


# --------------------------------------------------------------------------
# 4. the pack name itself is a path component too
# --------------------------------------------------------------------------


class TestPackNameCannotEscapePacksDir:
    @pytest.fixture
    def outside_pack(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        outside = tmp_root / "outside"
        outside.mkdir()
        write_manifest(outside, "sneaky", ["FromOutside"])
        return tmp_root, types_dir, packs_dir, outside

    def test_get_pack_does_not_read_outside_the_packs_dir(self, outside_pack):
        assert pack_registry.get_pack("../outside/sneaky") is None

    def test_install_pack_refuses_an_escaping_pack_name(self, outside_pack):
        tmp_root, types_dir, packs_dir, outside = outside_pack
        before = snapshot(tmp_root)

        result = pack_registry.install_pack("../outside/sneaky")

        assert "error" in result, result
        assert "not found" in result["error"].lower()
        assert not (types_dir / "FromOutside.yaml").exists()
        assert_outside_unchanged(before, snapshot(tmp_root), "types")

    def test_uninstall_pack_refuses_an_escaping_pack_name(self, outside_pack):
        tmp_root, types_dir, packs_dir, outside = outside_pack
        before = snapshot(tmp_root)

        result = pack_registry.uninstall_pack("../outside/sneaky", force=True)

        assert "error" in result, result
        assert (outside / "sneaky.yaml").exists()
        assert_outside_unchanged(before, snapshot(tmp_root), "types")


# --------------------------------------------------------------------------
# 5. the shared loader and the install probe
# --------------------------------------------------------------------------


class TestLoaderRejectsEscapingNames:
    def test_load_yaml_schema_does_not_read_outside_its_directory(self, tmp_path):
        (tmp_path / "leak.yaml").write_text(yaml.safe_dump({"type": "leaked"}), encoding="utf-8")
        schemas = tmp_path / "sch"
        schemas.mkdir()

        assert loader.load_yaml_schema(schemas, "../leak") is None

    @pytest.mark.parametrize("bad", UNSAFE_NAMES)
    def test_load_yaml_schema_returns_none_without_raising(self, tmp_path, bad):
        schemas = tmp_path / "sch"
        schemas.mkdir()
        assert loader.load_yaml_schema(schemas, bad) is None

    def test_load_yaml_schema_still_loads_a_safe_name(self, tmp_path):
        schemas = tmp_path / "sch"
        schemas.mkdir()
        (schemas / "한글타입.yaml").write_text(
            yaml.safe_dump({"type": "한글타입"}, allow_unicode=True), encoding="utf-8"
        )
        assert loader.load_yaml_schema(schemas, "한글타입") == {"type": "한글타입"}

    def test_is_pack_installed_treats_unsafe_type_names_as_not_installed(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        (tmp_root / "escape.yaml").write_text("outside\n", encoding="utf-8")

        assert pack_registry._is_pack_installed("whatever", ["../escape"]) is False


# --------------------------------------------------------------------------
# 6. symlinks: a safe NAME can still resolve outside
# --------------------------------------------------------------------------


class TestSymlinkContainment:
    def test_install_does_not_create_a_file_through_a_dangling_outward_symlink(self, packs_env):
        """A broken symlink reads as ``exists() == False``, so install treats it as new.

        Without the containment check, ``write_text`` follows the link and
        creates the file OUTSIDE the types directory.
        """
        tmp_root, types_dir, packs_dir = packs_env
        target = tmp_root / "outside_new.yaml"
        os.symlink(str(target), types_dir / "Broken.yaml")
        assert not target.exists()
        write_manifest(packs_dir, "sym", ["Broken"])
        before = snapshot(tmp_root)

        result = pack_registry.install_pack("sym")

        assert not target.exists(), "install wrote through a symlink to outside the types dir"
        assert result.get("skipped") == ["Broken"], result
        assert not result.get("created")
        assert_outside_unchanged(before, snapshot(tmp_root), "types")

    def test_uninstall_keeps_a_type_whose_path_resolves_outside(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        target = tmp_root / "outside_existing.yaml"
        target.write_text("pack: sym\n", encoding="utf-8")
        os.symlink(str(target), types_dir / "Linked.yaml")
        write_manifest(packs_dir, "sym", ["Linked"])

        result = pack_registry.uninstall_pack("sym", force=True)

        assert target.exists(), "uninstall removed a link whose target is outside"
        assert result.get("removed") == []
        assert result.get("kept_user_customised") == ["Linked"]

    def test_resolves_inside_returns_a_bool_for_a_symlink_loop(self, tmp_path):
        """A self-referential link must not leak an exception out of the check."""
        os.symlink("Loop.yaml", tmp_path / "Loop.yaml")
        assert isinstance(loader.resolves_inside(tmp_path / "Loop.yaml", tmp_path), bool)


class TestReadPathsStillFollowSymlinks:
    def test_get_pack_loads_a_manifest_that_is_a_symlink_to_outside(self, packs_env):
        """Reads keep following symlinks, matching ``list_packs``'s glob.

        Blocking this would make a symlinked pack visible to the list tool but
        "not found" to install -- an inconsistency with no gain against name
        injection, which layer 1 already stops.
        """
        tmp_root, types_dir, packs_dir = packs_env
        elsewhere = tmp_root / "elsewhere"
        elsewhere.mkdir()
        write_manifest(elsewhere, "linked", ["Normal"])
        os.symlink(str(elsewhere / "linked.yaml"), packs_dir / "linked.yaml")

        pack = pack_registry.get_pack("linked")

        assert pack is not None and pack["name"] == "linked"
        assert [p["name"] for p in pack_registry.list_packs()] == ["linked"]


# --------------------------------------------------------------------------
# 7. the name check itself
# --------------------------------------------------------------------------


class TestSafeSchemaName:
    @pytest.mark.parametrize("bad", UNSAFE_NAMES + ["/abs", "sub/nested", "trailing/"])
    def test_rejects(self, bad):
        assert loader.safe_schema_name(bad) is False

    @pytest.mark.parametrize("good", ["Normal", "한글타입", "Disease", "with space", "a-b_c.d", "х"])
    def test_accepts(self, good):
        assert loader.safe_schema_name(good) is True

    def test_rejects_non_strings(self):
        assert loader.safe_schema_name(None) is False
        assert loader.safe_schema_name(3) is False
