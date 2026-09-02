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
import ntpath
import os
import random
import string
from pathlib import Path

import pytest
import yaml

from opencrab.execution import action_registry
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
    # Both loaders are process-global @cache; clear each side before and after.
    loader.load_type_schema.cache_clear()
    action_registry.load_action_schema.cache_clear()
    yield tmp_path, types_dir, packs_dir
    loader.load_type_schema.cache_clear()
    action_registry.load_action_schema.cache_clear()


def write_manifest(packs_dir: Path, name: str, types: list[str]) -> None:
    (packs_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"name": name, "version": "1.0.0", "spaces": ["concept"], "types": types},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


# Names that must never reach the filesystem as a path component.
UNSAFE_NAMES = [
    # separators, relative and absolute forms
    "../escape", "a/b", "a\\b", "", ".", "..", "C:evil",
    # Windows-reserved characters (":" also makes an alternate data stream)
    "a:b", "a?b", "a*b", 'a"b', "a<b", "a>b", "a|b", "a\x00b", "a\x01b",
    # DOS device names -- on Windows these address a device, not a file here
    "CON", "con", "NUL", "COM1", "AUX.foo", "CONIN$", "CONOUT$", "COM\xb9", "LPT\xb3",
    "CON .txt",
    # Trailing dot/space. Refused on the LOGICAL name, which is stricter than
    # the "<name>.yaml" filename needs -- see safe_schema_name for why that is
    # deliberate. TestUninstallMarkerRoundTrip below pins the secondary reason
    # and shows it does NOT apply uniformly: "Foo." round-trips, "Foo " does not.
    "Foo.", "Foo ",
]

# Written out independently of the implementation on purpose: an equality
# assertion against these catches ANY single addition to or deletion from the
# module's own sets, including entries that some other check happens to cover.
EXPECTED_RESERVED_CHARS = frozenset(
    {chr(i) for i in range(32)} | {'"', "*", ":", "<", ">", "?", "|", "/", "\\"}
)
EXPECTED_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{c}" for c in "123456789\xb9\xb2\xb3"}
    | {f"LPT{c}" for c in "123456789\xb9\xb2\xb3"}
)


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


class TestUninstallMarkerRoundTrip:
    """Characterisation of today's generated-file marker, not a wish about it.

    ``safe_schema_name``'s docstring gives, as a secondary reason for refusing
    trailing dot/space names, that some of them break their own uninstall:
    the marker is the raw substring ``pack: <name>`` and PyYAML quotes a value
    it cannot leave bare. These cases pin that claim -- including the case
    where it does NOT hold -- so the docstring cannot quietly go stale. When
    the marker is changed to compare the parsed value, these fail and the
    docstring has to be revisited with them.

    The name guard is bypassed on purpose: these names cannot reach
    install_pack otherwise, and the point is to characterise what would
    happen if they did.
    """

    @pytest.mark.parametrize(
        ("pack_name", "quoted_by_yaml"),
        [("Foo.", False), ("Foo ", True), ("...", True)],
    )
    def test_marker_matches_only_when_yaml_leaves_the_value_bare(
        self, packs_env, monkeypatch, pack_name, quoted_by_yaml
    ):
        tmp_root, types_dir, packs_dir = packs_env
        monkeypatch.setattr(pack_registry, "safe_schema_name", lambda _name: True)
        write_manifest(packs_dir, pack_name, ["Normal"])

        assert pack_registry.install_pack(pack_name)["created"] == ["Normal"]
        schema = types_dir / "Normal.yaml"
        content = schema.read_text(encoding="utf-8")
        assert (f"pack: '{pack_name}'" in content) is quoted_by_yaml, content
        assert (f"pack: {pack_name}" in content) is not quoted_by_yaml, content

        result = pack_registry.uninstall_pack(pack_name)

        if quoted_by_yaml:
            assert result["removed"] == []
            assert result["kept_user_customised"] == ["Normal"]
            assert schema.exists(), "quoted marker: the file install created survives"
        else:
            assert result["removed"] == ["Normal"]
            assert not schema.exists()


class TestSchemaListingsOmitRefusedNames:
    """A listing must not advertise a stem the name check will refuse.

    ``list_registered_types`` and ``list_registered_actions`` glob their
    directory and hand back the stem, and that stem goes straight back in as
    the load argument. Advertising one the check refuses makes the listing
    claim a schema is registered while the load returns None -- and for
    actions that silently drops every required-parameter check, because
    validate_action_params treats a missing schema as "unregistered, allowed".
    """

    @staticmethod
    def _populate(directory):
        for stem in ("Normal", "CON", "a:b"):
            (directory / f"{stem}.yaml").write_text(
                yaml.safe_dump({"type": stem, "properties": {}}), encoding="utf-8"
            )

    def test_type_listing_omits_refused_stems(self, packs_env, monkeypatch, tmp_path):
        schemas = tmp_path / "sch"
        schemas.mkdir()
        self._populate(schemas)
        monkeypatch.setattr(loader, "SCHEMAS_DIR", schemas)
        loader.load_type_schema.cache_clear()

        assert loader.list_registered_types() == ["Normal"]

    def test_action_listing_omits_refused_stems(self, monkeypatch, tmp_path):
        actions = tmp_path / "act"
        actions.mkdir()
        self._populate(actions)
        monkeypatch.setattr(action_registry, "ACTIONS_DIR", actions)
        action_registry.load_action_schema.cache_clear()
        try:
            assert action_registry.list_registered_actions() == ["Normal"]
        finally:
            action_registry.load_action_schema.cache_clear()

    def test_every_listed_name_passes_the_name_check(self, monkeypatch, tmp_path):
        """The contract this establishes -- deliberately not "every listed name loads".

        That stronger claim is false: a safe stem holding an empty YAML is
        listed and still loads as None. Name safety is what this pins.
        """
        schemas = tmp_path / "sch"
        actions = tmp_path / "act"
        schemas.mkdir()
        actions.mkdir()
        self._populate(schemas)
        self._populate(actions)
        monkeypatch.setattr(loader, "SCHEMAS_DIR", schemas)
        monkeypatch.setattr(action_registry, "ACTIONS_DIR", actions)
        loader.load_type_schema.cache_clear()
        action_registry.load_action_schema.cache_clear()
        try:
            listed = loader.list_registered_types() + action_registry.list_registered_actions()
            assert listed, "expected the fixture to leave something listed"
            assert [n for n in listed if not loader.safe_schema_name(n)] == []
        finally:
            loader.load_type_schema.cache_clear()
            action_registry.load_action_schema.cache_clear()

    def test_the_repositorys_own_schemas_all_survive_the_filter(self):
        """Nothing shipped is dropped. Compared against the glob, not a list."""
        on_disk = sorted(p.stem for p in loader.SCHEMAS_DIR.glob("*.yaml"))
        assert loader.list_registered_types() == on_disk
        actions_on_disk = sorted(p.stem for p in action_registry.ACTIONS_DIR.glob("*.yaml"))
        assert action_registry.list_registered_actions() == actions_on_disk


class TestListingAndLookupAgree:
    """list_packs must not advertise a pack that the name check will refuse.

    Otherwise install reports the self-contradictory
    "Pack 'CON' not found. Available: ['CON']" and a client can keep picking
    a pack that can never be installed. This is the same consistency rule
    that keeps symlink-following on reads.
    """

    def test_listing_omits_a_pack_whose_name_the_guard_refuses(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "CON", ["Normal"])

        assert [p["name"] for p in pack_registry.list_packs()] == ["ok"]

    def test_available_list_does_not_offer_a_refused_name(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "CON", ["Normal"])

        result = pack_registry.install_pack("CON")

        assert "error" in result, result
        # The head of the message names the pack the caller asked for; it is
        # the "Available:" list that must not offer it back.
        available = result["error"].split("Available:", 1)[1]
        assert "CON" not in available, result["error"]

    def test_the_declared_name_is_what_is_checked_not_the_filename(self, packs_env):
        """A safe filename with a refused name inside must still be omitted.

        Without this, an implementation that checks ``path.stem`` passes the
        other tests while leaving the contradiction in place.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        (packs_dir / "safe-file.yaml").write_text(
            yaml.safe_dump(
                {"name": "CON", "version": "1.0.0", "spaces": ["concept"], "types": ["Normal"]}
            ),
            encoding="utf-8",
        )

        assert [p["name"] for p in pack_registry.list_packs()] == ["ok"]

    def test_listing_omits_a_pack_whose_type_names_install_would_refuse(self, packs_env):
        """A safe pack name is not enough -- install refuses the whole manifest.

        Four manifests spread the offending type across three indices -- 0
        (bad and mixed_first), 1 (mixed_last) and 2 (mixed_mid) -- so that a
        check of only the first element, only the last, or only both ends
        fails. Slices longer than the fixtures are closed by the identity spy
        below, not by these.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "bad", ["../escape"])
        write_manifest(packs_dir, "mixed_first", ["../escape", "Normal"])
        write_manifest(packs_dir, "mixed_mid", ["Normal", "Safe", "../escape", "Other"])
        write_manifest(packs_dir, "mixed_last", ["Normal", "../escape"])

        assert [p["name"] for p in pack_registry.list_packs()] == ["ok"]

    def test_a_bad_type_past_the_tenth_is_still_refused(self, packs_env):
        """The regression that was actually observed, pinned by a fixture.

        An earlier version of the spy below compared the argument's contents
        against a five-type manifest, and a check of only types[:10] passed it
        while an eleven-type manifest with the offender last really did get
        listed. Ten safe names ahead of one unsafe name pins that directly.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "late", [f"Safe{i}" for i in range(10)] + ["../escape"])

        assert [p["name"] for p in pack_registry.list_packs()] == ["ok"]

    def test_the_whole_type_list_is_vetted_not_a_slice_of_it(self, packs_env, monkeypatch):
        """Pin that the listing hands _unsafe_names the parsed list ITSELF.

        Two things are pinned, and the spy records both at call time:

        - identity: the argument IS the parsed list object. Every copy and
          slice builds a new object, so any check that passes a copied or
          sliced list fails this, whatever its length;
        - a snapshot of the contents: the object still equals what was
          declared when the check runs, so an in-place edit of the list is
          caught up to the length of this fixture.

        What this does not pin: an in-place cut past this fixture's length
        (del types[10:]) leaves a five-type list intact and passes here. No
        single finite fixture pins every in-place cut at every position; the
        eleven-type regression above pins the one that was observed.
        """
        tmp_root, types_dir, packs_dir = packs_env
        declared = ["A", "B", "C", "D", "E"]
        write_manifest(packs_dir, "wide", declared)

        parsed: list = []
        real_safe_load = yaml.safe_load

        def capture(stream):
            data = real_safe_load(stream)
            parsed.append(data)
            return data

        seen: list = []
        real_unsafe_names = pack_registry._unsafe_names

        def spy(names):
            seen.append((names, list(names)))
            return real_unsafe_names(names)

        monkeypatch.setattr(yaml, "safe_load", capture)
        monkeypatch.setattr(pack_registry, "_unsafe_names", spy)
        pack_registry.list_packs()

        assert parsed and parsed[-1]["types"] == declared, parsed
        assert any(
            arg is parsed[-1]["types"] and snapshot == declared
            for arg, snapshot in seen
        ), (
            "the type list was copied, sliced or edited before being vetted; "
            f"got {seen!r} for {parsed[-1]['types']!r}"
        )

    def test_a_refused_manifest_is_named_in_the_warning(self, packs_env, caplog):
        """Hiding it from the listing must not hide WHY -- the log carries both.

        Checked per record, not against the whole caplog text: the filename and
        the offending type names have to travel together to be useful.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "bad", ["../escape"])

        with caplog.at_level("WARNING"):
            pack_registry.list_packs()

        assert any(
            "bad.yaml" in r.getMessage() and "../escape" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_hidden_pack_can_still_be_asked_about_directly(self, packs_env):
        """Omitted from the listing, but a direct install still explains itself."""
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "bad", ["../escape"])

        result = pack_registry.install_pack("bad")

        assert "error" in result and "../escape" in result["error"], result

    def test_every_listed_pack_installs_without_an_error(self, packs_env):
        """Holds for this fixture, and is not claimed beyond it.

        A manifest whose declared name differs from its filename still lists
        under one name and resolves under another; that predates this branch
        and is out of scope, so the assertion stays scoped to what is set up
        here rather than being stated as a general law.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "bad", ["../escape"])
        write_manifest(packs_dir, "CON", ["Normal"])

        listed = [p["name"] for p in pack_registry.list_packs()]
        assert listed, "expected the fixture to leave something listed"
        for name in listed:
            assert "error" not in pack_registry.install_pack(name), name

    def test_a_safely_named_pack_is_still_listed_and_loadable(self, packs_env):
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "ok", ["Normal"])
        write_manifest(packs_dir, "CON", ["Normal"])

        assert pack_registry.get_pack("ok") is not None
        assert pack_registry.install_pack("ok")["created"] == ["Normal"]

    def test_bundled_packs_are_all_still_listed(self):
        """The real pack directory, unfiltered by the new skip."""
        assert sorted(p["name"] for p in pack_registry.list_packs()) == [
            "biomedical",
            "legal",
            "saas",
        ]


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

    def test_reserved_pack_name_is_refused_even_though_the_manifest_exists(self, packs_env):
        """A real CON.yaml manifest, so only the name check can be what refuses it.

        Without this, dropping the name check from ``get_pack`` survives: the
        only other pack-name case points outside, which fails on absence too.
        """
        tmp_root, types_dir, packs_dir = packs_env
        write_manifest(packs_dir, "CON", ["Normal"])
        assert (packs_dir / "CON.yaml").is_file()
        before = snapshot(tmp_root)

        assert pack_registry.get_pack("CON") is None
        install = pack_registry.install_pack("CON")
        uninstall = pack_registry.uninstall_pack("CON", force=True)

        assert "error" in install and "not found" in install["error"].lower(), install
        assert "error" in uninstall and "not found" in uninstall["error"].lower(), uninstall
        assert not (types_dir / "Normal.yaml").exists()
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

        A deliberate decision, kept after adversarial review raised it:
        blocking this would make a symlinked pack visible to the list tool but
        "not found" to install, an inconsistency with no gain against name
        injection -- which the name check already stops. Planting the link
        needs write access to the pack directory, which is a different threat
        from an unsafe name. Pinned here so a later change to that policy has
        to be a deliberate one.
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

    @pytest.mark.parametrize(
        "good",
        [
            "Normal", "한글타입", "Disease", "with space", "a-b_c.d", "х",
            # near-misses for the reserved-name set: matched exactly, never by prefix
            "Console", "Contract", "Nullable", "Auxiliary", "Printer",
            "COM0", "COM10", "LPT0", "LPT10",
            # DEL is NOT rejected: it is not in the Windows reserved set this
            # mirrors, and rejecting it would be a separate policy with no
            # rationale in this change. Pinned so the choice stays deliberate.
            "a\x7fb",
        ],
    )
    def test_accepts(self, good):
        assert loader.safe_schema_name(good) is True

    def test_rejects_non_strings(self):
        assert loader.safe_schema_name(None) is False
        assert loader.safe_schema_name(3) is False


class TestReservedFilenameRules:
    """Deterministic coverage of the reserved sets, run on every Python.

    The CPython parity test below skips on 3.11, which is the required CI, and
    a random sample would not reliably kill the deletion of one entry. These
    assertions target ``_is_reserved_filename`` directly rather than
    ``safe_schema_name`` because ":", "/" and "\\" would still be refused by
    the PurePath comparison, so a deletion of those from the character set
    would otherwise survive.
    """

    def test_reserved_character_set_matches_expected_exactly(self):
        assert loader._RESERVED_CHARS == EXPECTED_RESERVED_CHARS

    def test_reserved_name_set_matches_expected_exactly(self):
        assert loader._RESERVED_NAMES == EXPECTED_RESERVED_NAMES

    @pytest.mark.parametrize("ch", sorted(EXPECTED_RESERVED_CHARS))
    def test_every_reserved_character_is_refused(self, ch):
        assert loader._is_reserved_filename(f"a{ch}b") is True

    @pytest.mark.parametrize("reserved", sorted(EXPECTED_RESERVED_NAMES))
    def test_every_reserved_device_name_is_refused(self, reserved):
        for variant in (reserved, reserved.lower(), f"{reserved}.yaml", f"{reserved} .txt"):
            assert loader._is_reserved_filename(variant) is True, variant
        assert loader.safe_schema_name(reserved) is False

    def test_dot_and_dotdot_are_not_treated_as_reserved_filenames(self):
        """CPython excludes them here; safe_schema_name rejects them separately."""
        assert loader._is_reserved_filename(".") is False
        assert loader._is_reserved_filename("..") is False
        assert loader.safe_schema_name(".") is False
        assert loader.safe_schema_name("..") is False

    @pytest.mark.skipif(
        not hasattr(ntpath, "_isreservedname"),
        reason="ntpath._isreservedname is 3.13+; the deterministic tests above cover 3.11",
    )
    def test_matches_cpython_isreservedname(self):
        """Warn if the vendored copy drifts from the definition it was taken from."""
        rng = random.Random(20260902)
        alphabet = string.ascii_letters + "0123456789 ._$" + '"*:<>?|/\\' + "\x00\x01\x1f\x7f¹²³"
        cases = list(UNSAFE_NAMES) + ["Normal", "한글타입", "COM0", "LPT10", "nul .txt", "prn.a.b"]
        cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8))) for _ in range(2000)]
        mismatched = [
            c for c in cases if c and loader._is_reserved_filename(c) != ntpath._isreservedname(c)
        ]
        assert mismatched == []
