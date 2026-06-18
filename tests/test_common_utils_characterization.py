"""Characterization tests for duplicated common utilities.

These tests pin down the CURRENT observable behaviour of utility functions and
constants that are duplicated across the codebase, ahead of a planned
"common util extraction" refactor. They are NOT specifications of ideal
behaviour: every assertion captures what the code produces *today* (golden
values were obtained by actually calling each implementation). After the
refactor consolidates these into shared helpers, these tests must keep passing
unchanged — that is the regression safety net.

Notable cross-implementation differences deliberately recorded below:
  * stable-id helpers diverge: neo4j_export / export script use SHA256[:16] with
    a ``prefix:digest`` shape and hash a sorted-JSON encoding, while the
    obsidian importer uses SHA1[:16] with a ``prefix-digest`` shape and hashes
    the raw string. dedupe._compute_id uses SHA256[:16] with NO prefix.
  * slugify helpers diverge: mcp.tools._slugify and landscape.adapter._slug
    strip non-[a-z0-9] (Korean characters are dropped), with different empty
    fallbacks ("pack" vs "item"); the obsidian importer's slugify keeps Korean
    ([a-z0-9가-힣]) and falls back to "node".
  * _now_iso (5 definitions) produces an aware ISO string ending "+00:00";
    dedupe's inline timestamp is naive + literal "Z".
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
# The real ``crabharness`` package lives at crabharness/crabharness/; adding the
# crabharness/ dir to sys.path makes ``import crabharness`` resolve to it (and
# ``import codex_workers`` to its sibling). This matches how the runtime imports
# itself (e.g. ``from crabharness.models import ...``) and is the same strategy
# used by tests/test_structural_characterization.py, so both files agree on what
# sys.modules["crabharness"] points at when the full suite runs together.
CRABHARNESS_DIR = REPO_ROOT / "crabharness"
if str(CRABHARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(CRABHARNESS_DIR))


# ---------------------------------------------------------------------------
# Helpers to load modules that are not importable as packages
# ---------------------------------------------------------------------------


def _load_module_from_path(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass()/typing resolution looks the module up in
    # sys.modules via __module__, which fails for an unregistered module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


# ===========================================================================
# 1. _now_iso  (timestamp helpers)
# ===========================================================================

# Aware UTC ISO-8601 with microseconds and a "+00:00" offset, e.g.
# "2026-06-18T05:54:43.835470+00:00". Microseconds may be absent if the
# fractional part is exactly zero, so the pattern allows for that.
_AWARE_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00$"
)
# Naive ISO-8601 with a trailing literal "Z" (NOT a real offset), e.g.
# "2026-06-18T05:54:43.835548Z".
_NAIVE_Z_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _all_now_iso_funcs():
    # After the common-util extraction the 5 call-site modules all delegate to
    # the single shared ``now_iso`` (re-exported into each module namespace via
    # their import). Pinning them here still asserts every call site converged
    # on the same helper; the golden format assertions below are unchanged.
    from opencrab.billing.hooks import now_iso as billing_now
    from opencrab.execution.approvals import now_iso as approvals_now
    from opencrab.execution.workflow import now_iso as workflow_now
    from opencrab.ontology.identity import now_iso as identity_now
    from opencrab.ontology.promotion import now_iso as promotion_now

    return {
        "workflow": workflow_now,
        "approvals": approvals_now,
        "identity": identity_now,
        "promotion": promotion_now,
        "billing": billing_now,
    }


@pytest.mark.parametrize("label", ["workflow", "approvals", "identity", "promotion", "billing"])
def test_now_iso_aware_offset_format(label):
    """All 5 _now_iso definitions emit an aware "+00:00" offset ISO string."""
    func = _all_now_iso_funcs()[label]
    value = func()
    assert _AWARE_ISO_RE.match(value), f"{label} produced {value!r}"
    # Sanity: it must NOT use the naive trailing-Z form.
    assert not value.endswith("Z")


def test_now_iso_all_five_share_one_format():
    """The 5 definitions are structurally identical (same regex matches all)."""
    values = {label: f() for label, f in _all_now_iso_funcs().items()}
    for label, value in values.items():
        assert _AWARE_ISO_RE.match(value), f"{label}: {value!r}"
    # Every value parses back to an aware datetime at +00:00.
    from datetime import datetime, timezone

    for label, value in values.items():
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None, label
        assert parsed.utcoffset() == timezone.utc.utcoffset(None), label


def test_dedupe_inline_timestamp_uses_naive_Z_format():
    """dedupe.py inline timestamps differ: naive + literal "Z" (no offset).

    This is the divergence the refactor will have to reconcile. We pin the
    current shape by reproducing the exact expression used inline at
    crabharness/crabharness/dedupe.py:53,93.
    """
    from datetime import datetime

    value = datetime.utcnow().isoformat() + "Z"
    assert _NAIVE_Z_ISO_RE.match(value), value
    assert value.endswith("Z")
    # And it is NOT the aware offset form the _now_iso helpers produce.
    assert not _AWARE_ISO_RE.match(value)


# ===========================================================================
# 2. file_sha256 family (5 implementations)
# ===========================================================================

# Golden digests for known content (verified against hashlib directly).
_SHA256_HELLO_WORLD = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
_SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SHA256_BINARY_0_255 = "40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880"
# 1 MiB + 5 bytes of b"x" — exercises the 1 MiB chunk boundary (read loop).
_SHA256_BIG = "87fdbc9d8a44bce194d4cc22e90e6c21dd71179b6ecd0ac1c2af0b34b85e2e18"


def _file_sha256_impls():
    """Return {label: callable(Path) -> str} for all 5 implementations.

    Three live in importable packages; two live in scripts/ which is not a
    package, so they are loaded by file path (with a stubbed pyarrow for the
    nemotron builder which imports pyarrow.parquet at module load).
    """
    # media/pack now delegate to the shared ``file_sha256`` (re-exported into
    # each module namespace via import). The two scripts/ impls still ship their
    # own ``sha256_file`` (consolidated later, in the structural phase).
    from opencrab.media.image_context import file_sha256 as image_sha
    from opencrab.media.ocr import file_sha256 as ocr_sha
    from opencrab.pack.assembler import file_sha256 as assembler_sha

    impls = {
        "image_context._sha256": image_sha,
        "ocr._sha256": ocr_sha,
        "assembler._sha256": assembler_sha,
    }

    # export_pack_graph_from_neo4j.sha256_file — needs the `neo4j` package,
    # which is installed in this environment, so it loads directly.
    export_mod = _load_module_from_path(
        "_char_export_pack", SCRIPTS_DIR / "export_pack_graph_from_neo4j.py"
    )
    impls["export_pack_graph.sha256_file"] = export_mod.sha256_file

    # build_nemotron_personas_korea_pack.sha256_file — imports pyarrow.parquet
    # at module scope, which is not installed; stub it so the module loads.
    stub_pa = types.ModuleType("pyarrow")
    stub_paq = types.ModuleType("pyarrow.parquet")
    saved = {k: sys.modules.get(k) for k in ("pyarrow", "pyarrow.parquet")}
    sys.modules["pyarrow"] = stub_pa
    sys.modules["pyarrow.parquet"] = stub_paq
    try:
        nemotron_mod = _load_module_from_path(
            "_char_build_nemotron", SCRIPTS_DIR / "build_nemotron_personas_korea_pack.py"
        )
        impls["build_nemotron.sha256_file"] = nemotron_mod.sha256_file
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return impls


@pytest.fixture(scope="module")
def sha256_impls():
    return _file_sha256_impls()


def test_file_sha256_has_five_implementations(sha256_impls):
    assert set(sha256_impls) == {
        "image_context._sha256",
        "ocr._sha256",
        "assembler._sha256",
        "export_pack_graph.sha256_file",
        "build_nemotron.sha256_file",
    }


def test_file_sha256_known_content(tmp_path, sha256_impls):
    p = tmp_path / "hello.txt"
    p.write_bytes(b"hello world")
    for label, fn in sha256_impls.items():
        assert fn(p) == _SHA256_HELLO_WORLD, label


def test_file_sha256_empty_file(tmp_path, sha256_impls):
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    for label, fn in sha256_impls.items():
        assert fn(p) == _SHA256_EMPTY, label


def test_file_sha256_chunk_boundary_over_1mib(tmp_path, sha256_impls):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * (1024 * 1024 + 5))
    for label, fn in sha256_impls.items():
        assert fn(p) == _SHA256_BIG, label


def test_file_sha256_binary_all_bytes(tmp_path, sha256_impls):
    p = tmp_path / "bin.dat"
    p.write_bytes(bytes(range(256)))
    for label, fn in sha256_impls.items():
        assert fn(p) == _SHA256_BINARY_0_255, label


def test_file_sha256_all_impls_agree(tmp_path, sha256_impls):
    """The five implementations are byte-for-byte equivalent on arbitrary data."""
    p = tmp_path / "mixed.dat"
    p.write_bytes(b"\x00\x01mixed \xed\x95\x9c\xea\xb8\x80 content" * 1000)
    results = {label: fn(p) for label, fn in sha256_impls.items()}
    assert len(set(results.values())) == 1, results


# ===========================================================================
# 3. stable_id / sha_id / _compute_id family
# ===========================================================================
#
# Golden values pin each implementation's algorithm + truncation + prefix shape.
# These intentionally DIFFER between implementations; the divergence is the
# whole point of recording them before the refactor.


def _load_obsidian_module():
    """scripts/import_obsidian_vault.py imports opencrab packages only; it is
    importable directly via file path (no missing third-party deps)."""
    return _load_module_from_path(
        "_char_obsidian", SCRIPTS_DIR / "import_obsidian_vault.py"
    )


def _load_landscape_adapter():
    """crabharness/codex_workers/landscape/adapter.py imports crabharness.models
    and crabharness.semantic, which are NOT present in this checkout. Stub them
    so the module (and its pure _slug helper) can be loaded."""
    models = types.ModuleType("crabharness.models")
    for n in (
        "ArtifactBundle",
        "ArtifactFile",
        "DelegationJob",
        "MissionSpec",
        "PromotionEdge",
        "PromotionNode",
        "PromotionPackage",
        "ValidationIssue",
        "ValidationReport",
    ):
        setattr(models, n, type(n, (object,), {}))
    semantic = types.ModuleType("crabharness.semantic")
    semantic.determine_autoresearch_verdict = lambda *a, **k: None
    semantic.score_bundle_semantically = lambda *a, **k: None

    saved = {
        k: sys.modules.get(k)
        for k in ("crabharness.models", "crabharness.semantic")
    }
    sys.modules["crabharness.models"] = models
    sys.modules["crabharness.semantic"] = semantic
    try:
        return _load_module_from_path(
            "_char_landscape_adapter",
            REPO_ROOT / "crabharness" / "codex_workers" / "landscape" / "adapter.py",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_neo4j_export_sha_id_golden():
    """opencrab.pack.neo4j_export._sha_id — SHA256[:16] over sorted-JSON,
    "prefix:digest" shape."""
    from opencrab.pack.neo4j_export import _sha_id

    # str input is JSON-encoded as a quoted string before hashing.
    assert _sha_id("neo4j-node", "hello") == "neo4j-node:5aa762ae383fbb72"
    assert _sha_id("neo4j-node", {"id": "x", "name": "한글"}) == "neo4j-node:2fed141e36c8429b"
    assert _sha_id("p", "") == "p:12ae32cb1ec02d01"
    digest = _sha_id("x", {"a": 1})
    # 16 hex chars after the single ":" separator.
    prefix, _, hexpart = digest.partition(":")
    assert prefix == "x"
    assert len(hexpart) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", hexpart)


def test_export_script_sha_id_matches_neo4j_export():
    """scripts/export_pack_graph_from_neo4j.sha_id is equivalent to the
    package _sha_id (same SHA256[:16], "prefix:digest", sorted-JSON)."""
    from opencrab.pack.neo4j_export import _sha_id

    export_mod = _load_module_from_path(
        "_char_export_pack2", SCRIPTS_DIR / "export_pack_graph_from_neo4j.py"
    )
    for prefix, value in [
        ("neo4j-node", {"id": "x", "name": "한글"}),
        ("neo4j-edge", {"a": [1, 2, 3]}),
        ("p", "hello"),
        ("p", ""),
    ]:
        assert export_mod.sha_id(prefix, value) == _sha_id(prefix, value)
    # Concrete golden, mirroring neo4j_export.
    assert export_mod.sha_id("neo4j-node", {"id": "x", "name": "한글"}) == "neo4j-node:2fed141e36c8429b"


def test_obsidian_sha_id_golden_sha1_dash_shape():
    """scripts/import_obsidian_vault.sha_id DIVERGES: SHA1[:16] over the RAW
    string (no JSON), "prefix-digest" shape (dash, not colon)."""
    obs = _load_obsidian_module()
    assert obs.sha_id("obsidian", "hello") == "obsidian-aaf4c61ddcc5e8a2"
    assert obs.sha_id("p", "") == "p-da39a3ee5e6b4b0d"
    assert obs.sha_id("p", "한글나무") == "p-2edff1eac8bc6a8e"
    digest = obs.sha_id("x", "anything")
    prefix, _, hexpart = digest.partition("-")
    assert prefix == "x"
    assert len(hexpart) == 16
    # Confirm it is SHA1, not SHA256, of the raw bytes.
    assert hexpart == hashlib.sha1(b"anything").hexdigest()[:16]
    # And confirm it is NOT the SHA256-of-JSON form used by neo4j_export.
    assert hexpart != hashlib.sha256(b'"anything"').hexdigest()[:16]


def test_dedupe_compute_id_golden_no_prefix():
    """crabharness.dedupe._compute_id DIVERGES: SHA256[:16] of
    "source|key" with NO prefix, returns the bare 16-char hex."""
    from crabharness.dedupe import _compute_id

    assert _compute_id("a", "b") == "0eab8a0a3380abf4"
    assert _compute_id("", "") == "cbe5cfdf7c2118a9"
    assert _compute_id("소스", "키") == "4ce68715c15a3ecf"
    out = _compute_id("src", "key")
    assert len(out) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", out)
    assert ":" not in out and "-" not in out
    # Confirm the exact construction: SHA256 of "source|key".
    assert _compute_id("a", "b") == hashlib.sha256(b"a|b").hexdigest()[:16]


def test_stable_id_family_divergence_summary():
    """Single test that contrasts all three id shapes side by side, so the
    refactor can see exactly what must be preserved or unified."""
    from crabharness.dedupe import _compute_id

    from opencrab.pack.neo4j_export import _sha_id

    obs = _load_obsidian_module()

    neo = _sha_id("p", "hello")        # SHA256[:16] of '"hello"', colon
    obsidian = obs.sha_id("p", "hello")  # SHA1[:16] of 'hello', dash
    dedupe = _compute_id("p", "hello")   # SHA256[:16] of 'p|hello', no prefix

    assert neo == "p:5aa762ae383fbb72"
    assert obsidian == "p-aaf4c61ddcc5e8a2"
    assert dedupe == hashlib.sha256(b"p|hello").hexdigest()[:16]
    # All three differ from each other.
    assert len({neo, obsidian, dedupe}) == 3


# ===========================================================================
# 4. _slugify / _slug family
# ===========================================================================


def test_mcp_slugify_golden():
    """opencrab.mcp.tools._slugify — drops non-[a-z0-9], fallback "pack"."""
    from opencrab.mcp.tools import _slugify

    assert _slugify("Hello World") == "hello-world"
    assert _slugify("") == "pack"
    assert _slugify("   ") == "pack"
    assert _slugify("a@@b !!!") == "a-b"
    assert _slugify("  __Foo Bar__  ") == "foo-bar"
    # Korean is stripped entirely (only "test" survives).
    assert _slugify("한글 Test 노드") == "test"
    # A purely-Korean title collapses to the fallback.
    assert _slugify("한글노드") == "pack"
    assert _slugify("a___---  b") == "a-b"
    assert _slugify("UPPER") == "upper"


def test_landscape_slug_golden():
    """crabharness.codex_workers.landscape.adapter._slug — same regex as
    mcp._slugify but fallback is "item" (not "pack")."""
    adapter = _load_landscape_adapter()
    _slug = adapter._slug

    assert _slug("Hello World") == "hello-world"
    assert _slug("") == "item"
    assert _slug("   ") == "item"
    assert _slug("a@@b !!!") == "a-b"
    assert _slug("  __Foo Bar__  ") == "foo-bar"
    assert _slug("한글 Test 노드") == "test"
    assert _slug("한글노드") == "item"
    assert _slug("a___---  b") == "a-b"
    assert _slug("UPPER") == "upper"


def test_obsidian_slugify_golden_keeps_korean():
    """scripts/import_obsidian_vault.slugify DIVERGES: keeps Korean
    ([a-z0-9가-힣]), fallback "node"."""
    obs = _load_obsidian_module()
    _slug = obs.slugify

    assert _slug("Hello World") == "hello-world"
    assert _slug("") == "node"
    assert _slug("   ") == "node"
    assert _slug("a@@b !!!") == "a-b"
    assert _slug("  __Foo Bar__  ") == "foo-bar"
    # Korean is PRESERVED here (unlike the other two slug helpers).
    assert _slug("한글 Test 노드") == "한글-test-노드"
    assert _slug("한글노드") == "한글노드"
    assert _slug("a___---  b") == "a-b"
    assert _slug("UPPER") == "upper"


def test_slug_family_korean_divergence():
    """Side-by-side: same Korean input, three different outputs/fallbacks."""
    from opencrab.mcp.tools import _slugify

    adapter = _load_landscape_adapter()
    obs = _load_obsidian_module()

    text = "한글 노드"
    assert _slugify(text) == "pack"        # Korean dropped -> empty -> fallback
    assert adapter._slug(text) == "item"   # Korean dropped -> empty -> fallback
    assert obs.slugify(text) == "한글-노드"  # Korean kept


# ===========================================================================
# 5. _l2_normalize (2 implementations)
# ===========================================================================


def _l2_impls():
    # Both embedding modules now delegate to the shared ``l2_normalize``
    # (re-exported into each module namespace via import). Golden values below
    # are unchanged.
    from opencrab.stores.llamacpp_embedding import l2_normalize as llama
    from opencrab.stores.openai_embedding import l2_normalize as oai

    return {"openai": oai, "llamacpp": llama}


@pytest.mark.parametrize("label", ["openai", "llamacpp"])
def test_l2_normalize_unit_vector(label):
    fn = _l2_impls()[label]
    out = fn([3.0, 4.0])
    assert out == [0.6, 0.8]
    norm = sum(x * x for x in out) ** 0.5
    assert abs(norm - 1.0) < 1e-9


@pytest.mark.parametrize("label", ["openai", "llamacpp"])
def test_l2_normalize_zero_vector_returns_original(label):
    """Norm below 1e-9 threshold -> original vector returned unchanged."""
    fn = _l2_impls()[label]
    assert fn([0.0, 0.0]) == [0.0, 0.0]
    # Empty vector: norm == 0 -> returns the (empty) original.
    assert fn([]) == []
    # Sub-threshold magnitude is also returned as-is (not normalised).
    assert fn([1e-10]) == [1e-10]


@pytest.mark.parametrize("label", ["openai", "llamacpp"])
def test_l2_normalize_single_element(label):
    fn = _l2_impls()[label]
    assert fn([5.0]) == [1.0]


def test_l2_normalize_both_impls_agree():
    oai = _l2_impls()["openai"]
    llama = _l2_impls()["llamacpp"]
    for vec in ([3.0, 4.0], [0.0, 0.0], [5.0], [], [1e-10], [1.0, 2.0, 2.0]):
        assert oai(list(vec)) == llama(list(vec)), vec


# ===========================================================================
# 6. parse_props / _parse (2 implementations)
# ===========================================================================


def _parse_impls():
    """Return {label: callable(str|None) -> dict}.

    After the common-util extraction both graph stores delegate to the shared
    ``parse_props`` (re-exported into each module namespace via import:
    kuzu keeps the ``_parse`` alias, local imports ``parse_props``). Golden
    values below are unchanged."""
    from opencrab.stores.kuzu_graph_store import _parse as kuzu_parse
    from opencrab.stores.local_graph_store import parse_props as local_parse

    return {"local._parse_props": local_parse, "kuzu._parse": kuzu_parse}


@pytest.fixture(scope="module")
def parse_impls():
    return _parse_impls()


def test_parse_props_valid_dict(parse_impls):
    for label, fn in parse_impls.items():
        assert fn('{"a": 1, "b": "한글"}') == {"a": 1, "b": "한글"}, label


def test_parse_props_none_and_empty(parse_impls):
    for label, fn in parse_impls.items():
        assert fn(None) == {}, label
        assert fn("") == {}, label


def test_parse_props_non_dict_json_returns_empty(parse_impls):
    # Lists, scalars and JSON null all collapse to {} (not a dict).
    for label, fn in parse_impls.items():
        assert fn("[1,2,3]") == {}, label
        assert fn("42") == {}, label
        assert fn('"hi"') == {}, label
        assert fn("null") == {}, label


def test_parse_props_broken_json_returns_empty(parse_impls):
    for label, fn in parse_impls.items():
        assert fn("{not json") == {}, label


def test_parse_props_both_impls_agree(parse_impls):
    local = parse_impls["local._parse_props"]
    kuzu = parse_impls["kuzu._parse"]
    cases = [
        '{"a": 1, "b": "한글"}',
        None,
        "",
        "[1,2,3]",
        "42",
        '"hi"',
        "{not json",
        "null",
        '{"nested": {"x": [1, 2]}}',
    ]
    for raw in cases:
        assert local(raw) == kuzu(raw), raw


# ===========================================================================
# 7. EMBEDDING_FUNCTION_NAME == "kure_v1" (3 sources)
# ===========================================================================


def test_embedding_function_name_constant_matches():
    """The shared ``EMBEDDING_FUNCTION_NAME`` constant is "kure_v1" and is
    re-exported into both embedding modules' namespaces via import."""
    from opencrab.stores.llamacpp_embedding import EMBEDDING_FUNCTION_NAME as llama_name
    from opencrab.stores.openai_embedding import EMBEDDING_FUNCTION_NAME as oai_name

    assert oai_name == "kure_v1"
    assert llama_name == "kure_v1"


def test_embedding_name_method_returns_kure_v1():
    """All three EF .name() methods return the same "kure_v1" string.

    Constructors are I/O-free (no network/model load at construction time), so
    we can instantiate directly. Resilient delegates to primary.name()."""
    from opencrab.stores.llamacpp_embedding import LlamaCppEmbeddingFunction
    from opencrab.stores.openai_embedding import OpenAIEmbeddingFunction
    from opencrab.stores.resilient_embedding import ResilientEmbeddingFunction

    oai = OpenAIEmbeddingFunction(api_base="http://localhost/v1", model="kure")
    llama = LlamaCppEmbeddingFunction(gguf_path="/nonexistent/model.gguf")
    resilient = ResilientEmbeddingFunction(primary=oai, fallback=llama)

    assert oai.name() == "kure_v1"
    assert llama.name() == "kure_v1"
    assert resilient.name() == "kure_v1"
