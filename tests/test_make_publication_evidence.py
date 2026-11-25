"""``scripts/make_publication_evidence.py``: 병합 입력 세트의 게시 증거 파일 생성 (#710).

실제 백업 세트로 만든 증거가 병합 도구의 게시 모드를 통과해야 하고, 규칙을 어긴 입력은
출력 파일을 남기지 않고 종료 코드 2로 끝나야 한다.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencrab.stores.backup import backup_data_dir
from tests.test_merge_sqlite_stores import merge_mod, source_dir  # noqa: F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_publication_evidence.py"
_spec = importlib.util.spec_from_file_location("make_publication_evidence", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

KEYS = {"version", "snapshot_id", "source_manifest_sha256", "preparer_id", "frozen_from",
        "frozen_until", "writer_stopped", "billing_same_snapshot", "packs_observation",
        "backup_at", "packs_checked_at", "capture_from", "capture_until"}


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def backup_set(source_dir, tmp_path):  # noqa: F811
    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backup_data_dir(source_dir, backups, settings=SimpleNamespace(vector_db_file="vectors.db"))
    return backup.set_dir


def _args(backup_set: Path, out: Path, **override) -> list[str]:
    lock = json.loads((backup_set / "MANIFEST.json").read_text())["lock"]
    start, end = _stamp(lock["acquired_at"]), _stamp(lock["released_at"])
    values = {"preparer-id": "tester", "frozen-from": (start - timedelta(seconds=2)).isoformat(),
              "packs-checked-at": (start - timedelta(seconds=1)).isoformat(),
              "frozen-until": (end + timedelta(seconds=1)).isoformat()}
    values.update({key.replace("_", "-"): value for key, value in override.items()})
    args = [str(backup_set), "--out", str(out)]
    for key, value in values.items():
        args += [f"--{key}", value]
    return args


def test_evidence_from_real_backup_passes_publication_merge(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    assert tool.main(_args(backup_set, out)) == 0
    evidence = json.loads(out.read_text())
    assert set(evidence) == KEYS
    manifest = json.loads((backup_set / "MANIFEST.json").read_text())
    assert evidence["snapshot_id"] == manifest["set_id"]
    assert evidence["capture_until"] == manifest["lock"]["released_at"]
    (backup_set.parent / (backup_set.name + ".source.lifetime.lock")).touch()
    root = tmp_path / "published"
    rc = merge_mod.main(["--source-dir", str(backup_set), "--publication-root", str(root),
                         "--evidence-file", str(out), "--apply", "--verify", "--immutable-source",
                         "--min-free-gib", "0"])
    assert rc == 0
    assert (root / "single.db").is_file()


def test_refuses_overwrite_and_keeps_existing_file(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    out.write_text("keep")
    assert tool.main(_args(backup_set, out)) == 2
    assert out.read_text() == "keep"


def test_manifest_without_lock_is_rejected(backup_set, tmp_path):
    manifest = json.loads((backup_set / "MANIFEST.json").read_text())
    del manifest["lock"]
    (backup_set / "MANIFEST.json").write_text(json.dumps(manifest))
    out = tmp_path / "evidence.json"
    assert tool.main(_args_without_lock(backup_set, out)) == 2
    assert not out.exists()


def _args_without_lock(backup_set: Path, out: Path) -> list[str]:
    return [str(backup_set), "--out", str(out), "--preparer-id", "tester",
            "--frozen-from", "2026-01-01T00:00:00+00:00",
            "--packs-checked-at", "2026-01-01T00:00:01+00:00",
            "--frozen-until", "2026-01-01T00:00:05+00:00"]


@pytest.mark.parametrize("manifest_bytes", [None, b"not json", b"[]"])
def test_unreadable_manifest_is_rejected(tmp_path, manifest_bytes):
    set_dir = tmp_path / "set"
    set_dir.mkdir()
    if manifest_bytes is not None:
        (set_dir / "MANIFEST.json").write_bytes(manifest_bytes)
    out = tmp_path / "evidence.json"
    assert tool.main(_args_without_lock(set_dir, out)) == 2
    assert not out.exists()


@pytest.mark.parametrize("field", ["frozen_from", "packs_checked_at", "frozen_until"])
def test_window_that_misses_capture_is_rejected(backup_set, tmp_path, field):
    lock = json.loads((backup_set / "MANIFEST.json").read_text())["lock"]
    start, end = _stamp(lock["acquired_at"]), _stamp(lock["released_at"])
    bad = {"frozen_from": (start + timedelta(seconds=1)).isoformat(),
           "packs_checked_at": (end + timedelta(minutes=5)).isoformat(),
           "frozen_until": (start - timedelta(seconds=1)).isoformat()}[field]
    out = tmp_path / "evidence.json"
    assert tool.main(_args(backup_set, out, **{field: bad})) == 2
    assert not out.exists()


def test_non_utc_offset_is_rejected(backup_set, tmp_path):
    lock = json.loads((backup_set / "MANIFEST.json").read_text())["lock"]
    start = _stamp(lock["acquired_at"])
    kst = (start - timedelta(seconds=2)).astimezone(UTC).replace(tzinfo=None).isoformat() + "+09:00"
    out = tmp_path / "evidence.json"
    assert tool.main(_args(backup_set, out, frozen_from=kst)) == 2
    assert not out.exists()


def test_empty_preparer_is_rejected(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    assert tool.main(_args(backup_set, out, preparer_id="")) == 2
    assert not out.exists()


def test_zulu_suffix_times_are_accepted(backup_set, tmp_path):
    lock = json.loads((backup_set / "MANIFEST.json").read_text())["lock"]
    start, end = _stamp(lock["acquired_at"]), _stamp(lock["released_at"])

    def zulu(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    out = tmp_path / "evidence.json"
    rc = tool.main(_args(backup_set, out, frozen_from=zulu(start - timedelta(seconds=2)),
                         packs_checked_at=zulu(start - timedelta(seconds=1)),
                         frozen_until=zulu(end + timedelta(seconds=1))))
    assert rc == 0


@pytest.mark.parametrize("where", ["set", "nested"])
def test_out_inside_the_set_is_rejected_and_set_stays_clean(backup_set, where):
    before = sorted(p.name for p in backup_set.iterdir())
    out = backup_set / "evidence.json" if where == "set" else backup_set / "docs" / "evidence.json"
    assert tool.main(_args(backup_set, out)) == 2
    assert sorted(p.name for p in backup_set.iterdir()) == before


def test_partial_file_is_not_left_after_failure(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    out.write_text("keep")
    assert tool.main(_args(backup_set, out)) == 2
    assert not (tmp_path / "evidence.json.partial").exists()


@pytest.mark.parametrize("released_offset,rc", [(0.999, 0), (1.0, 2)])
def test_capture_end_must_stay_inside_backup_second(backup_set, tmp_path, released_offset, rc):
    manifest = json.loads((backup_set / "MANIFEST.json").read_text())
    created = _stamp(manifest["created_at"])
    manifest["lock"]["acquired_at"] = (created - timedelta(seconds=1)).isoformat()
    manifest["lock"]["released_at"] = (created + timedelta(seconds=released_offset)).isoformat()
    (backup_set / "MANIFEST.json").write_text(json.dumps(manifest))
    out = tmp_path / "evidence.json"
    args = [str(backup_set), "--out", str(out), "--preparer-id", "tester",
            "--frozen-from", (created - timedelta(seconds=3)).isoformat(),
            "--packs-checked-at", (created - timedelta(seconds=2)).isoformat(),
            "--frozen-until", (created + timedelta(seconds=5)).isoformat()]
    assert tool.main(args) == rc
    assert out.exists() == (rc == 0)


def test_write_failure_removes_own_partial_and_exits_2(backup_set, tmp_path, monkeypatch):
    out = tmp_path / "evidence.json"

    def boom(_fd):
        raise OSError(27, "File too large")

    monkeypatch.setattr(tool.os, "fsync", boom)
    assert tool.main(_args(backup_set, out)) == 2
    monkeypatch.undo()
    assert not (tmp_path / "evidence.json.partial").exists()
    assert not out.exists()
    # 같은 경로로 다시 실행하면 합격한다.
    assert tool.main(_args(backup_set, out)) == 0


def test_foreign_partial_file_is_left_alone(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    foreign = tmp_path / "evidence.json.partial"
    foreign.write_text("someone else")
    assert tool.main(_args(backup_set, out)) == 2
    assert foreign.read_text() == "someone else"


def test_out_symlink_file_in_set_pointing_outside_is_rejected(backup_set, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (backup_set / "evidence.json").symlink_to(outside / "evidence.json")
    before = sorted(p.name for p in backup_set.rglob("*"))
    assert tool.main(_args(backup_set, backup_set / "evidence.json")) == 2
    assert sorted(p.name for p in backup_set.rglob("*")) == before
    assert list(outside.iterdir()) == []


def test_frozen_from_inside_lock_interval_is_rejected(backup_set, tmp_path):
    lock = json.loads((backup_set / "MANIFEST.json").read_text())["lock"]
    start, end = _stamp(lock["acquired_at"]), _stamp(lock["released_at"])
    assert end > start
    inside = (start + (end - start) / 2).isoformat()
    out = tmp_path / "evidence.json"
    assert tool.main(_args(backup_set, out, frozen_from=inside, packs_checked_at=inside)) == 2
    assert not out.exists()


def test_lock_without_released_at_is_rejected(backup_set, tmp_path):
    out = tmp_path / "evidence.json"
    args = _args(backup_set, out)
    manifest = json.loads((backup_set / "MANIFEST.json").read_text())
    del manifest["lock"]["released_at"]
    (backup_set / "MANIFEST.json").write_text(json.dumps(manifest))
    assert tool.main(args) == 2
    assert not out.exists()


def test_out_through_alias_of_set_is_rejected(backup_set, tmp_path):
    alias = tmp_path / "set-alias"
    alias.symlink_to(backup_set, target_is_directory=True)
    before = sorted(p.name for p in backup_set.iterdir())
    assert tool.main(_args(backup_set, alias / "evidence.json")) == 2
    assert sorted(p.name for p in backup_set.iterdir()) == before


def test_out_in_set_when_set_is_given_through_alias(backup_set, tmp_path):
    alias = tmp_path / "set-alias"
    alias.symlink_to(backup_set, target_is_directory=True)
    before = sorted(p.name for p in backup_set.iterdir())
    args = _args(backup_set, backup_set / "evidence.json")
    args[0] = str(alias)
    assert tool.main(args) == 2
    assert sorted(p.name for p in backup_set.iterdir()) == before
