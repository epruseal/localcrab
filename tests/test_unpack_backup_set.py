"""``scripts/unpack_backup_set.py``: 백업 세트에서 DB 다섯 파일만 풀기 (#710).

정지 상태 백업 세트의 MANIFEST는 삭제 저널 같은 하위 경로 파일과 심볼릭 링크 항목도 적는다.
도구는 다섯 DB만 sha256 대조해 복사하고 나머지는 건너뛰어야 한다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencrab.stores.backup import backup_data_dir
from tests.test_merge_sqlite_stores import source_dir  # noqa: F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "unpack_backup_set.py"
_spec = importlib.util.spec_from_file_location("unpack_backup_set", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

NAMES = ("billing.db", "doc_store.db", "graph.db", "opencrab.db", "vectors.db")


def _entry(data: bytes) -> dict:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture
def fake_set(tmp_path):
    """다섯 DB와 하위 경로 파일, 심볼릭 링크 항목이 든 손으로 만든 세트."""
    root = tmp_path / "set"
    (root / "delete-pack-journals").mkdir(parents=True)
    files = {}
    for name in NAMES:
        data = f"content of {name}".encode() * 50
        (root / name).write_bytes(data)
        files[name] = _entry(data)
    journal = b'{"pack": "x"}'
    (root / "delete-pack-journals" / "pending.json").write_bytes(journal)
    files["delete-pack-journals/pending.json"] = _entry(journal)
    files["link-entry"] = {"symlink": True}
    (root / "MANIFEST.json").write_text(json.dumps({"format": 1, "files": files}))
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / "MANIFEST.json").read_text())


def _write_manifest(root: Path, manifest: dict) -> None:
    (root / "MANIFEST.json").write_text(json.dumps(manifest))


def test_unpacks_five_dbs_and_skips_subpaths(fake_set, tmp_path, capsys):
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 0
    assert sorted(p.name for p in dest.iterdir()) == sorted(NAMES)
    for name in NAMES:
        assert (dest / name).read_bytes() == (fake_set / name).read_bytes()
    out = capsys.readouterr().out
    assert "delete-pack-journals/pending.json" in out
    assert "link-entry" in out
    assert "ALL OK" in out


def test_real_backup_set_with_subdirectories(source_dir, tmp_path):  # noqa: F811
    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
    (source_dir / "delete-pack-journals").mkdir()
    (source_dir / "delete-pack-journals" / "pending.json").write_text("{}", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backup_data_dir(source_dir, backups, settings=SimpleNamespace(vector_db_file="vectors.db"))
    assert any("/" in key for key in _manifest(backup.set_dir)["files"]), "전제: 하위 경로 항목이 있다"
    dest = tmp_path / "out"
    assert tool.main([str(backup.set_dir), str(dest)]) == 0
    assert sorted(p.name for p in dest.iterdir()) == sorted(NAMES)


def test_destination_must_not_exist(fake_set, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep").write_text("x")
    assert tool.main([str(fake_set), str(dest)]) == 2
    assert [p.name for p in dest.iterdir()] == ["keep"]


def test_missing_parent_of_destination(fake_set, tmp_path):
    assert tool.main([str(fake_set), str(tmp_path / "no" / "out")]) == 2
    assert not (tmp_path / "no").exists()


@pytest.mark.parametrize("name", NAMES)
def test_missing_db_file_stops_before_creating_destination(fake_set, tmp_path, name):
    (fake_set / name).unlink()
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 2
    assert not dest.exists()


@pytest.mark.parametrize("name", NAMES)
def test_missing_manifest_entry_stops_before_creating_destination(fake_set, tmp_path, name):
    manifest = _manifest(fake_set)
    del manifest["files"][name]
    _write_manifest(fake_set, manifest)
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 2
    assert not dest.exists()


@pytest.mark.parametrize("mode", ["missing", "bad-json", "no-files", "symlink-entry", "bad-hash"])
def test_unusable_manifest_is_rejected(fake_set, tmp_path, mode):
    if mode == "missing":
        (fake_set / "MANIFEST.json").unlink()
    elif mode == "bad-json":
        (fake_set / "MANIFEST.json").write_text("{")
    else:
        manifest = _manifest(fake_set)
        if mode == "no-files":
            del manifest["files"]
        elif mode == "symlink-entry":
            manifest["files"]["graph.db"] = {"symlink": True}
        else:
            manifest["files"]["graph.db"]["sha256"] = "zz"
        _write_manifest(fake_set, manifest)
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 2
    assert not dest.exists()


def test_symlinked_db_in_set_is_rejected(fake_set, tmp_path):
    target = tmp_path / "elsewhere.db"
    target.write_bytes((fake_set / "graph.db").read_bytes())
    (fake_set / "graph.db").unlink()
    (fake_set / "graph.db").symlink_to(target)
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 2
    assert not dest.exists()


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_mismatch_removes_destination_and_exits_1(fake_set, tmp_path, field, capsys):
    manifest = _manifest(fake_set)
    if field == "sha256":
        manifest["files"]["vectors.db"]["sha256"] = "0" * 64
    else:
        manifest["files"]["vectors.db"]["size"] += 1
    _write_manifest(fake_set, manifest)
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 1
    assert not dest.exists()
    assert "ALL OK" not in capsys.readouterr().out


def test_source_set_is_not_modified(fake_set, tmp_path):
    before = {p.name: p.read_bytes() for p in fake_set.rglob("*") if p.is_file()}
    assert tool.main([str(fake_set), str(tmp_path / "out")]) == 0
    after = {p.name: p.read_bytes() for p in fake_set.rglob("*") if p.is_file()}
    assert before == after


def test_empty_db_file_is_copied(fake_set, tmp_path):
    (fake_set / "billing.db").write_bytes(b"")
    manifest = _manifest(fake_set)
    manifest["files"]["billing.db"] = _entry(b"")
    _write_manifest(fake_set, manifest)
    dest = tmp_path / "out"
    assert tool.main([str(fake_set), str(dest)]) == 0
    assert (dest / "billing.db").read_bytes() == b""
