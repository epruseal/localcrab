"""Read-only verification helpers used by the issue #80 runner and tests."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_MODULES = {
    "tests/test_issue80_sql_graph.py",
    "tests/test_pack_provenance.py",
}
BACKENDS = {"sqlite", "postgresql", "kuzu", "neo4j", "unit", "static"}
PHASES = {"startup", "graphtx", "runtime", "builder", "mapping", "swap", "guards"}
NEGATIVE_REF = "kuzu-capability-negative-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(value)
    return value


def validate_manifest(value: dict[str, Any]) -> None:
    """Validate the exact-set and Kùzu metadata invariants."""
    if not isinstance(value, dict) or value.get("schema") != "issue80-manifest-v1":
        raise ValueError("invalid issue80 manifest schema")
    if value.get("repository_root") != ".":
        raise ValueError("issue80 manifest repository root must be '.'")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("issue80 manifest cases are empty")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("malformed issue80 manifest case")
        allowed = {"nodeid", "backend", "phase", "selection", "negative_probe_ref"}
        if set(case) - allowed:
            raise ValueError("unknown issue80 manifest case key")
        nodeid = case.get("nodeid")
        if not isinstance(nodeid, str) or nodeid in seen:
            raise ValueError("duplicate or malformed issue80 nodeid")
        seen.add(nodeid)
        if "::" not in nodeid:
            raise ValueError("issue80 nodeid is not concrete")
        module = nodeid.split("::", 1)[0]
        if module not in ALLOWED_MODULES:
            raise ValueError(f"issue80 nodeid outside allowlist: {nodeid}")
        if case.get("backend") not in BACKENDS or case.get("phase") not in PHASES:
            raise ValueError("invalid issue80 case metadata")
        if case.get("backend") == "kuzu":
            if case.get("selection") != "capability-negative" or case.get("negative_probe_ref") != NEGATIVE_REF:
                raise ValueError("Kùzu case is not capability-negative")
        else:
            if case.get("selection") != "ordinary" or "negative_probe_ref" in case:
                raise ValueError("non-Kùzu case has negative metadata")
    qualification = value.get("kuzu_qualification")
    if not isinstance(qualification, dict):
        raise ValueError("Kùzu qualification metadata is missing")
    if qualification.get("bundle_id") != "kuzu-qualification-v1" or qualification.get("status") != "capability-negative":
        raise ValueError("Kùzu qualification verdict is not capability-negative")
    if qualification.get("package") != "ladybug" or qualification.get("required_version") != "0.19.1":
        raise ValueError("Kùzu qualification package metadata is incomplete")
    bundle = ROOT / "tests" / "fixtures" / "issue80" / "qualification"
    if not all((bundle / name).is_file() for name in (
        "manifest.json", "kuzu_capability.json", "kuzu-qualification-v1.json",
    )):
        raise ValueError("Kùzu qualification bundle is missing")
    if value.get("regression_command") != "python -m pytest -q":
        raise ValueError("issue80 regression command drift")


def manifest_nodeids(value: dict[str, Any]) -> tuple[str, ...]:
    validate_manifest(value)
    return tuple(case["nodeid"] for case in value["cases"])


def verify_kuzu_bundle() -> dict[str, Any]:
    """Validate the portable capability-negative evidence without importing Ladybug."""
    bundle = ROOT / "tests" / "fixtures" / "issue80" / "qualification"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    capability = json.loads((bundle / "kuzu_capability.json").read_text(encoding="utf-8"))
    canonical = json.loads((bundle / "kuzu-qualification-v1.json").read_text(encoding="utf-8"))
    canonical_keys = {
        "schema", "bundle_id", "status", "observed_platform", "package",
        "raw_pypi_metadata", "files", "observations",
    }
    if set(canonical) != canonical_keys:
        raise ValueError("canonical Kùzu qualification key set drift")
    if canonical.get("schema") != "issue80-kuzu-qualification-v1" or canonical.get("bundle_id") != "kuzu-qualification-v1":
        raise ValueError("invalid canonical Kùzu qualification schema")
    if canonical.get("status") != "capability-negative" or canonical.get("observed_platform") != "macos-arm64-cp314":
        raise ValueError("canonical Kùzu qualification verdict drift")
    package = canonical.get("package")
    if not isinstance(package, dict) or package.get("name") != "ladybug" or package.get("version") != "0.19.1":
        raise ValueError("canonical Kùzu package metadata drift")
    wheel_name = "ladybug-0.19.1-cp314-cp314-macosx_15_0_arm64.whl"
    if package.get("wheel_filename") != wheel_name or package.get("platform_tag") != "macosx_15_0_arm64":
        raise ValueError("canonical Kùzu wheel metadata drift")
    raw_name = "raw_pypi_metadata-ladybug-0.19.1.json"
    raw_path = bundle / raw_name
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if set(raw) != {"info", "last_serial", "ownership", "urls", "vulnerabilities"}:
        raise ValueError("PyPI observation is not the complete response shape")
    if not isinstance(raw.get("vulnerabilities"), list):
        raise ValueError("PyPI observation vulnerabilities field is missing")
    raw_meta = canonical.get("raw_pypi_metadata")
    expected_raw_hash = _sha256(raw_path)
    if not isinstance(raw_meta, dict) or raw_meta.get("raw_pypi_metadata_path") != f"tests/fixtures/issue80/qualification/{raw_name}" or raw_meta.get("raw_pypi_metadata_sha256") != expected_raw_hash:
        raise ValueError("canonical PyPI observation hash drift")
    info = raw.get("info")
    if not isinstance(info, dict) or info.get("name") != "ladybug" or info.get("version") != "0.19.1":
        raise ValueError("PyPI observation version drift")
    if info.get("release_url") != raw_meta.get("release_url"):
        raise ValueError("PyPI observation release URL drift")
    wheels = [item for item in raw.get("urls", []) if item.get("filename") == wheel_name]
    if len(wheels) != 1 or wheels[0].get("digests", {}).get("sha256") != package.get("wheel_sha256"):
        raise ValueError("PyPI wheel observation drift")
    if raw_meta.get("core_metadata_sha256") != wheels[0].get("core-metadata", {}).get("sha256"):
        raise ValueError("PyPI core metadata observation drift")
    files = canonical.get("files")
    if not isinstance(files, list) or len(files) != 4:
        raise ValueError("canonical qualification companion list drift")
    expected_paths = {
        "tests/fixtures/issue80/qualification/qualification_probe.py",
        "tests/fixtures/issue80/qualification/qualification_probe.raw.json",
        "tests/fixtures/issue80/qualification/resolution-reinstall.raw.txt",
        f"tests/fixtures/issue80/qualification/{raw_name}",
    }
    observed_paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("malformed qualification companion entry")
        relative = entry["path"]
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("qualification companion path is not repository-relative")
        path = (ROOT / relative).resolve()
        if path != ROOT and ROOT not in path.parents:
            raise ValueError("qualification companion path escaped repository")
        observed_paths.add(relative)
        if not path.is_file() or _sha256(path) != entry["sha256"]:
            raise ValueError(f"qualification companion hash drift: {relative}")
    if observed_paths != expected_paths:
        raise ValueError("canonical qualification companion path set drift")
    if manifest.get("runtime_version") != "ladybug==0.19.1":
        raise ValueError("qualification runtime version drift")
    if capability.get("atomic_write_capability") != "unavailable":
        raise ValueError("Kùzu capability is not negative")
    source = (ROOT / "opencrab" / "stores" / "kuzu_graph_store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name.split(".")[0] in {"ladybug", "kuzu"} for alias in node.names):
                raise ValueError("Kùzu optional package imported by active source")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"execute", "executemany", "commit", "rollback"}:
                raise ValueError("Kùzu source contains a direct mutation sink")
    return {"status": "capability-negative", "runtime_version": manifest["runtime_version"]}


def graph_source_residuals() -> dict[str, list[str]]:
    """Return source residuals that would bypass the Kùzu or fixture boundary."""
    kuzu = (ROOT / "opencrab" / "stores" / "kuzu_graph_store.py").read_text(encoding="utf-8")
    positives = [line for line in kuzu.splitlines() if "import ladybug" in line or "_conn.execute" in line]
    test_positive = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "importorskip(\"ladybug\")" in text or "KuzuGraphStore(db_path" in text:
            test_positive.append(str(path.relative_to(ROOT)))
    return {"kuzu_production": positives, "kuzu_test_positive": test_positive}


def create_evidence_child(base_dir: str | os.PathLike[str]) -> Path:
    """Create a private, wrapper-owned evidence child below an explicit base."""
    base = Path(base_dir)
    if not base.is_absolute() or not base.is_dir() or base.is_symlink():
        raise ValueError("evidence base must be an existing absolute directory")
    resolved = base.resolve()
    if resolved in {Path("/"), Path("/tmp"), ROOT} or ROOT in resolved.parents:
        raise ValueError("evidence base is too broad")
    child = Path(tempfile.mkdtemp(prefix="localcrab-issue80.", dir=str(resolved)))
    child_stat = child.stat()
    if child.resolve().parent != resolved or stat.S_IMODE(child_stat.st_mode) != 0o700:
        raise RuntimeError("evidence child has unsafe identity")
    marker = child / ".issue80-run"
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{child.resolve()}\n")
    except BaseException:
        try:
            marker.unlink()
        finally:
            child.rmdir()
        raise
    return child


def cleanup_evidence_child(base_dir: str | os.PathLike[str], child: str | os.PathLike[str]) -> None:
    """Remove only a verified wrapper child; reject siblings and symlinks."""
    base = Path(base_dir).resolve()
    target = Path(child)
    if not target.is_absolute() or target.is_symlink() or not target.is_dir():
        raise ValueError("evidence child is not a real directory")
    resolved = target.resolve()
    if resolved.parent != base or not resolved.name.startswith("localcrab-issue80."):
        raise ValueError("evidence child escaped its approved base")
    marker = resolved / ".issue80-run"
    if marker.is_symlink() or not marker.is_file() or stat.S_IMODE(marker.stat().st_mode) != 0o600:
        raise ValueError("evidence marker is unsafe")
    if marker.read_text(encoding="utf-8") != f"{resolved}\n":
        raise ValueError("evidence marker identity mismatch")
    for path in resolved.rglob("*"):
        if path.is_symlink() or not path.is_file():
            raise ValueError("evidence child contains an unsafe entry")
    shutil.rmtree(resolved)
