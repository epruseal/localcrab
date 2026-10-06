"""#363: cleanup of a published backup in write_backup_atomic and repair().

Nothing here needs PostgreSQL. The writer tests use tmp_path. The settle tests use a fake
transaction object. The CLI tests stub repair().
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import repair_pgvector_legacy_none_owner as repair  # noqa: E402

SNAP = {"table": "t", "rows": []}
STATE_ERROR = repair.BackupPublicationStateError


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _tmp_name(path: Path) -> str:
    return f"{path.name}.tmp-{os.getpid()}"


def _replace(path: Path, text: str) -> None:
    """Replace the entry with a file of another inode.

    Unlink then recreate can reuse the inode number on ext4, so write the new file first and rename it over."""
    staged = path.with_name(path.name + ".staged")
    staged.write_text(text)
    os.replace(staged, path)


# ---------------------------------------------------------------------------
# platform facts (P1)
# ---------------------------------------------------------------------------


def test_platform_supports_the_dir_fd_calls_the_writer_uses():
    for fn in (os.open, os.stat, os.unlink, os.link):
        assert fn in os.supports_dir_fd
    assert os.link in os.supports_follow_symlinks
    assert os.stat in os.supports_follow_symlinks


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------


class TestWriter:
    def test_normal_success_returns_the_identity_of_the_published_file(self, tmp_path):
        backup = tmp_path / "backup.json"
        ident = repair.write_backup_atomic(str(backup), SNAP)
        stat = os.stat(backup)
        assert ident == (stat.st_dev, stat.st_ino)
        assert (stat.st_mode & 0o777) == 0o600
        assert not (tmp_path / _tmp_name(backup)).exists()

    def test_existing_final_is_preserved_and_no_temp_remains(self, tmp_path):
        backup = tmp_path / "backup.json"
        backup.write_text("older backup")
        with pytest.raises(FileExistsError, match="already exists"):
            repair.write_backup_atomic(str(backup), SNAP)
        assert backup.read_text() == "older backup"
        assert not (tmp_path / _tmp_name(backup)).exists()

    def test_post_link_confirmation_failure_preserves_final_and_temp(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_stat = os.stat

        def stat_fails_for_the_final(path, *args, **kwargs):
            if path == backup.name and kwargs.get("dir_fd") is not None:
                raise OSError("simulated: stat failed after link")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat_fails_for_the_final)
        with pytest.raises(STATE_ERROR) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        assert info.value.publication_state == "unconfirmed"
        assert backup.exists() and (tmp_path / _tmp_name(backup)).exists()
        monkeypatch.undo()
        with pytest.raises(FileExistsError):
            repair.write_backup_atomic(str(backup), SNAP)

    def test_temp_replaced_before_the_confirmed_cleanup_survives(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        temp = tmp_path / _tmp_name(backup)
        real_stat = os.stat
        state = {"n": 0}

        def swap_temp_after_the_confirmation(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == backup.name and kwargs.get("dir_fd") is not None:
                state["n"] += 1
                if state["n"] == 1:  # the confirmation stat of the final
                    _replace(temp, "someone else's file")
            return result

        monkeypatch.setattr(os, "stat", swap_temp_after_the_confirmation)
        with pytest.raises(OSError) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert temp.read_text() == "someone else's file"
        assert not backup.exists(), "the unpublish step must remove the confirmed final"
        assert any("preserved" in n and temp.name in n for n in getattr(info.value, "__notes__", []))

    def test_fchmod_failure_with_a_replaced_temp_preserves_the_replacement(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        temp = tmp_path / _tmp_name(backup)

        def fchmod_fails_after_a_swap(fd, mode):
            _replace(temp, "someone else's file")
            raise PermissionError("simulated: fchmod denied")

        monkeypatch.setattr(os, "fchmod", fchmod_fails_after_a_swap)
        with pytest.raises(PermissionError) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        assert temp.read_text() == "someone else's file"
        assert any(temp.name in n for n in getattr(info.value, "__notes__", []))
        assert not backup.exists()

    def test_first_fstat_failure_leaks_no_fd_and_says_the_temp_is_preserved(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        before = _fd_count()
        real_fstat = os.fstat

        def fstat_fails_once(fd):
            monkeypatch.setattr(os, "fstat", real_fstat)
            raise OSError("simulated: fstat failed")

        monkeypatch.setattr(os, "fstat", fstat_fails_once)
        with pytest.raises(OSError) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert _fd_count() == before
        assert not backup.exists()
        assert (tmp_path / _tmp_name(backup)).exists()
        assert any("preserved" in n for n in getattr(info.value, "__notes__", []))

    def test_file_fd_close_failure_before_the_link_publishes_nothing(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_close = os.close
        seen = {"file_fd": None}
        real_open = os.open

        def remember(path, flags, mode=0o777, **kw):
            fd = real_open(path, flags, mode, **kw)
            if flags & os.O_CREAT:
                seen["file_fd"] = fd
            return fd

        def close_fails_for_the_file_fd(fd):
            real_close(fd)
            if fd == seen["file_fd"]:
                seen["file_fd"] = None
                raise OSError("simulated: close failed")

        monkeypatch.setattr(os, "open", remember)
        monkeypatch.setattr(os, "close", close_fails_for_the_file_fd)
        with pytest.raises(OSError, match="close failed"):
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert not backup.exists()
        assert not (tmp_path / _tmp_name(backup)).exists()

    def test_ancestor_swap_during_the_first_fsync_cannot_redirect_the_cleanup(self, tmp_path, monkeypatch):
        """Swap the parent directory while the data fsync runs. Path-based cleanup would
        delete the unrelated file that now sits at the old path. Relative cleanup through
        the pinned directory must not."""
        home = tmp_path / "work"
        home.mkdir()
        backup = home / "backup.json"
        moved = tmp_path / "work-original"
        real_fsync = os.fsync
        calls = {"n": 0}

        # first call: data fsync with the swap; second call: the directory fsync, which fails
        def combined(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                real_fsync(fd)
                os.rename(home, moved)
                home.mkdir()
                (home / _tmp_name(backup)).write_text("unrelated temp-named file")
                (home / backup.name).write_text("unrelated final-named file")
                return None
            raise OSError("simulated: directory fsync failed")

        monkeypatch.setattr(os, "fsync", combined)
        with pytest.raises(OSError, match="directory fsync failed"):
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert (home / _tmp_name(backup)).read_text() == "unrelated temp-named file"
        assert (home / backup.name).read_text() == "unrelated final-named file"
        assert not (moved / backup.name).exists(), "the unpublish step must remove our final"

    def test_a_failed_temp_cleanup_before_the_link_is_named_in_a_note(self, tmp_path, monkeypatch):
        """The temp file stays when its cleanup fails. The operator must be able to read where it is."""
        backup = tmp_path / "backup.json"
        real_unlink = os.unlink

        def temp_unlink_fails(path, *args, **kwargs):
            if path.startswith(backup.name + ".tmp-"):
                raise OSError("simulated: cannot remove the temp file")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "fchmod", lambda fd, mode: (_ for _ in ()).throw(PermissionError("simulated: fchmod denied")))
        monkeypatch.setattr(os, "unlink", temp_unlink_fails)
        with pytest.raises(PermissionError) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert (tmp_path / _tmp_name(backup)).exists()
        notes = getattr(info.value, "__notes__", [])
        assert any("preserved" in n and "cannot remove the temp file" in n for n in notes), notes

    def test_failed_final_removal_after_confirmation_reports_published(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_unlink = os.unlink
        real_fsync = os.fsync
        calls = {"n": 0}

        def dir_fsync_fails(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_fsync(fd)
            raise OSError("simulated: directory fsync failed")

        def final_unlink_fails(path, *args, **kwargs):
            if path == backup.name:
                raise OSError("simulated: cannot remove the final")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", dir_fsync_fails)
        monkeypatch.setattr(os, "unlink", final_unlink_fails)
        with pytest.raises(STATE_ERROR) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert info.value.publication_state == "published"
        assert backup.exists()

    def test_final_replaced_before_the_unpublish_step_reports_unconfirmed(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_fsync = os.fsync
        calls = {"n": 0}

        def dir_fsync_fails_after_a_swap(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_fsync(fd)
            _replace(backup, "replacement")
            raise OSError("simulated: directory fsync failed")

        monkeypatch.setattr(os, "fsync", dir_fsync_fails_after_a_swap)
        with pytest.raises(STATE_ERROR) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert info.value.publication_state == "unconfirmed"
        assert backup.read_text() == "replacement"

    def test_final_already_absent_at_the_unpublish_step_is_not_published(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_fsync = os.fsync
        calls = {"n": 0}

        def dir_fsync_fails_after_the_final_vanishes(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_fsync(fd)
            os.unlink(backup)
            raise OSError("simulated: directory fsync failed")

        monkeypatch.setattr(os, "fsync", dir_fsync_fails_after_the_final_vanishes)
        with pytest.raises(OSError, match="directory fsync failed") as info:
            repair.write_backup_atomic(str(backup), SNAP)
        assert not isinstance(info.value, STATE_ERROR)
        assert not backup.exists()


# ---------------------------------------------------------------------------
# discard helper
# ---------------------------------------------------------------------------


class TestDiscardPublishedBackup:
    def _publish(self, directory: Path) -> tuple[Path, tuple[int, int]]:
        backup = directory / "backup.json"
        return backup, repair.write_backup_atomic(str(backup), SNAP)

    def test_removes_our_file(self, tmp_path):
        backup, ident = self._publish(tmp_path)
        assert repair._discard_published_backup(str(backup), ident) is True
        assert not backup.exists()

    def test_absent_file_counts_as_removed(self, tmp_path):
        backup, ident = self._publish(tmp_path)
        backup.unlink()
        assert repair._discard_published_backup(str(backup), ident) is True

    def test_replaced_file_is_preserved(self, tmp_path):
        backup, ident = self._publish(tmp_path)
        _replace(backup, "replacement")
        assert repair._discard_published_backup(str(backup), ident) is False
        assert backup.read_text() == "replacement"

    def test_ancestor_swapped_before_the_call_is_preserved(self, tmp_path):
        home = tmp_path / "work"
        home.mkdir()
        backup, ident = self._publish(home)
        os.rename(home, tmp_path / "moved")
        home.mkdir()
        (home / "backup.json").write_text("unrelated file")
        assert repair._discard_published_backup(str(backup), ident) is False
        assert (home / "backup.json").read_text() == "unrelated file"

    def test_ancestor_swap_between_the_identity_check_and_the_unlink_cannot_redirect(self, tmp_path, monkeypatch):
        home = tmp_path / "work"
        home.mkdir()
        backup, ident = self._publish(home)
        moved = tmp_path / "moved"
        real_stat = os.stat
        state = {"done": False}

        def swap_after_the_identity_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == "backup.json" and kwargs.get("dir_fd") is not None and not state["done"]:
                state["done"] = True
                os.rename(home, moved)
                home.mkdir()
                (home / "backup.json").write_text("unrelated file")
            return result

        monkeypatch.setattr(os, "stat", swap_after_the_identity_stat)
        assert repair._discard_published_backup(str(backup), ident) is True
        monkeypatch.undo()
        assert (home / "backup.json").read_text() == "unrelated file"
        assert not (moved / "backup.json").exists()

    def test_directory_close_failure_does_not_change_the_result(self, tmp_path, monkeypatch):
        backup, ident = self._publish(tmp_path)
        real_close = os.close
        real_open = os.open
        seen = {"dir_fd": None}

        def remember(path, flags, mode=0o777, **kw):
            fd = real_open(path, flags, mode, **kw)
            if flags & os.O_DIRECTORY:
                seen["dir_fd"] = fd
            return fd

        def close_fails_for_the_directory(fd):
            real_close(fd)
            if fd == seen["dir_fd"]:
                seen["dir_fd"] = None
                raise OSError("simulated: close failed")

        monkeypatch.setattr(os, "open", remember)
        monkeypatch.setattr(os, "close", close_fails_for_the_directory)
        assert repair._discard_published_backup(str(backup), ident) is True
        monkeypatch.undo()
        assert not backup.exists()


# ---------------------------------------------------------------------------
# settle function
# ---------------------------------------------------------------------------


class _Trans:
    def __init__(self, rollback_error: Exception | None = None):
        self.rollback_error = rollback_error
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1
        if self.rollback_error is not None:
            raise self.rollback_error


def _exc(message: str = "boom", *, invalidated: bool = False) -> OSError:
    exc = OSError(message)
    if invalidated:
        exc.connection_invalidated = True
    return exc


def _published(tmp_path: Path) -> tuple[Path, tuple[int, int]]:
    backup = tmp_path / "backup.json"
    return backup, repair.write_backup_atomic(str(backup), SNAP)


class TestSettleFailedRepair:
    def test_rule_a_keeps_a_state_error_and_adds_the_database_outcome(self, tmp_path):
        original = STATE_ERROR("detail", publication_state="unconfirmed")
        result = repair._settle_failed_repair(original, _Trans(), str(tmp_path / "b"), None, False)
        assert result is original
        assert original.publication_state == "unconfirmed"
        assert original.db_outcome == "rolled back"

    def test_rule_a_with_a_rollback_failure_names_it(self, tmp_path):
        original = STATE_ERROR("detail", publication_state="published")
        trans = _Trans(RuntimeError("rollback broke"))
        result = repair._settle_failed_repair(original, trans, str(tmp_path / "b"), None, True)
        assert result is original and original.publication_state == "published"
        assert "unknown" in original.db_outcome

    @pytest.mark.parametrize("published", [False, True])
    @pytest.mark.parametrize("commit_started", [False, True])
    def test_rule_b_rollback_failure_is_primary(self, tmp_path, published, commit_started):
        backup, ident = _published(tmp_path) if published else (tmp_path / "none.json", None)
        trans = _Trans(RuntimeError("rollback broke"))
        result = repair._settle_failed_repair(_exc("primary"), trans, str(backup), ident, commit_started)
        assert isinstance(result, STATE_ERROR)
        assert result.publication_state == ("published" if published else "not published")
        assert ("unknown" in result.db_outcome) == commit_started
        assert "primary" in str(result)
        if published:
            assert backup.exists()

    def test_rule_b_carries_the_notes_of_the_original_error(self, tmp_path):
        original = _exc("primary")
        original.add_note("temp file x.tmp-1 preserved: reason")
        result = repair._settle_failed_repair(original, _Trans(RuntimeError("r")), str(tmp_path / "b"), None, False)
        assert "x.tmp-1 preserved" in str(result)

    def test_rollback_exception_never_escapes(self, tmp_path):
        trans = _Trans(RuntimeError("rollback broke"))
        result = repair._settle_failed_repair(_exc(), trans, str(tmp_path / "b"), None, False)
        assert isinstance(result, BaseException) and trans.rolled_back == 1

    def test_rule_c_unpublished_failure_is_rolled_back(self, tmp_path):
        original = _exc("primary")
        result = repair._settle_failed_repair(original, _Trans(), str(tmp_path / "b"), None, False)
        assert result is original
        assert original.publication_state == "not published" and original.db_outcome == "rolled back"

    def test_rule_d_connection_loss_preserves_the_backup(self, tmp_path):
        backup, ident = _published(tmp_path)
        original = _exc("lost", invalidated=True)
        result = repair._settle_failed_repair(original, _Trans(), str(backup), ident, True)
        assert result is original and backup.exists()
        assert original.publication_state == "published"
        assert original.db_outcome == "unknown (connection invalidated)"

    def test_rule_e_definite_failure_removes_the_backup(self, tmp_path):
        backup, ident = _published(tmp_path)
        original = _exc("definite")
        result = repair._settle_failed_repair(original, _Trans(), str(backup), ident, True)
        assert result is original and not backup.exists()
        assert original.publication_state == "not published" and original.db_outcome == "rolled back"

    def test_rule_e_already_absent_backup_counts_as_removed(self, tmp_path):
        backup, ident = _published(tmp_path)
        backup.unlink()
        result = repair._settle_failed_repair(_exc("definite"), _Trans(), str(backup), ident, True)
        assert not isinstance(result, STATE_ERROR) and result.publication_state == "not published"

    def test_rule_e_replaced_backup_is_preserved_and_unconfirmed(self, tmp_path):
        backup, ident = _published(tmp_path)
        _replace(backup, "replacement")
        result = repair._settle_failed_repair(_exc("definite"), _Trans(), str(backup), ident, True)
        assert isinstance(result, STATE_ERROR) and result.publication_state == "unconfirmed"
        assert result.db_outcome == "rolled back" and backup.read_text() == "replacement"

    def test_rule_e_failed_removal_reports_published(self, tmp_path, monkeypatch):
        backup, ident = _published(tmp_path)
        real_unlink = os.unlink

        def fail(path, *args, **kwargs):
            if path == backup.name:
                raise OSError("simulated: cannot remove")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fail)
        result = repair._settle_failed_repair(_exc("definite"), _Trans(), str(backup), ident, True)
        monkeypatch.undo()
        assert isinstance(result, STATE_ERROR) and result.publication_state == "published"
        assert backup.exists()


# ---------------------------------------------------------------------------
# error text
# ---------------------------------------------------------------------------


class TestErrorText:
    def test_the_word_uncertain_is_used_only_for_the_unconfirmed_state(self):
        for state in ("published", "not published"):
            text = str(STATE_ERROR("detail", publication_state=state, db_outcome="rolled back"))
            assert "uncertain" not in text.lower()
            assert state in text
        assert "unconfirmed" in str(STATE_ERROR("detail", publication_state="unconfirmed"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_text(monkeypatch, capsys, exc: BaseException) -> tuple[int, str]:
    """Run main() with the database layer stubbed and repair() raising exc."""

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Engine:
        class url:  # noqa: N801
            host = "h"
            port = 5432

        def connect(self):
            return _Conn()

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(repair, "connect", lambda dsn: _Engine())
    monkeypatch.setattr(repair, "target_identity_reason", lambda engine: None)
    monkeypatch.setattr(repair, "detect", lambda engine, table: [])
    monkeypatch.setattr(repair, "audit", lambda engine, table: [])
    monkeypatch.setattr(repair, "repair", boom)
    code = repair.main(["--pg-url", "postgresql://u:p@h/db", "--table", "t", "--apply", "--skip-backup"])
    return code, capsys.readouterr().out


class TestCli:
    def test_state_error_prints_the_state_the_outcome_and_the_retry_rule(self, monkeypatch, capsys):
        exc = STATE_ERROR("detail", publication_state="published", db_outcome="rolled back")
        code, out = _cli_text(monkeypatch, capsys, exc)
        assert code == repair.EXIT_BACKUP
        assert "published" in out and "manual investigation" in out and "not be retried" in out

    def test_unpublished_connection_loss_does_not_claim_a_preserved_backup(self, monkeypatch, capsys):
        exc = _exc("lost", invalidated=True)
        exc.publication_state, exc.db_outcome = "not published", "rolled back"
        code, out = _cli_text(monkeypatch, capsys, exc)
        assert code == repair.EXIT_BACKUP
        assert "preserved" not in out and "not published" in out and "rolled back" in out

    def test_published_connection_loss_says_the_backup_is_preserved_and_the_outcome_unknown(self, monkeypatch, capsys):
        exc = _exc("lost", invalidated=True)
        exc.publication_state, exc.db_outcome = "published", "unknown (connection invalidated)"
        _, out = _cli_text(monkeypatch, capsys, exc)
        assert "preserved" in out and "unknown (connection invalidated)" in out
        assert "repair rolled back" not in out

    def test_definite_failure_with_a_removed_backup_says_rolled_back(self, monkeypatch, capsys):
        exc = _exc("definite")
        exc.publication_state, exc.db_outcome = "not published", "rolled back"
        _, out = _cli_text(monkeypatch, capsys, exc)
        assert "rolled back" in out and "preserved" not in out

    def test_an_oserror_without_state_attributes_makes_no_claim(self, monkeypatch, capsys):
        _, out = _cli_text(monkeypatch, capsys, _exc("plain"))
        assert "publication state unknown" in out and "database outcome unknown" in out
        assert "rolled back" not in out and "preserved" not in out

    def test_notes_of_the_error_are_printed(self, monkeypatch, capsys):
        exc = _exc("plain")
        exc.add_note("temp file x.tmp-1 preserved: reason")
        _, out = _cli_text(monkeypatch, capsys, exc)
        assert "x.tmp-1 preserved" in out

# ---------------------------------------------------------------------------
# correction 1 (F1 to F4)
# ---------------------------------------------------------------------------


class TestCorrectionF1TempStateIsThreeValued:
    def test_a_temp_stat_error_after_the_confirmation_is_named_in_a_note(self, tmp_path, monkeypatch):
        """After the link is confirmed, the directory fsync fails. Reading the temp state then also fails.
        The temp may still exist, so the operator must be told where it is and that its state is unknown."""
        backup = tmp_path / "backup.json"
        temp_name = _tmp_name(backup)
        real_fsync, real_stat = os.fsync, os.stat
        state = {"fsyncs": 0}

        def dir_fsync_fails(fd):
            state["fsyncs"] += 1
            if state["fsyncs"] == 1:
                return real_fsync(fd)
            raise OSError("simulated: directory fsync failed")

        def temp_stat_fails_after_the_failure(path, *args, **kwargs):
            if path == temp_name and kwargs.get("dir_fd") is not None and state["fsyncs"] >= 2:
                raise OSError("simulated: stat failed for the temp file")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", dir_fsync_fails)
        monkeypatch.setattr(os, "stat", temp_stat_fails_after_the_failure)
        with pytest.raises(OSError) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        notes = getattr(info.value, "__notes__", [])
        assert any(temp_name in n and "could not be read" in n for n in notes), notes
        assert not backup.exists()

    def test_an_absent_temp_adds_no_note(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        real_fsync = os.fsync
        state = {"n": 0}

        def dir_fsync_fails(fd):
            state["n"] += 1
            if state["n"] == 1:
                return real_fsync(fd)
            raise OSError("simulated: directory fsync failed")

        monkeypatch.setattr(os, "fsync", dir_fsync_fails)
        with pytest.raises(OSError, match="directory fsync failed") as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert not any("temp file" in n for n in getattr(info.value, "__notes__", []))

    def test_a_present_temp_is_named_when_the_final_cannot_be_removed(self, tmp_path, monkeypatch):
        backup = tmp_path / "backup.json"
        temp_name = _tmp_name(backup)
        real_fsync, real_unlink = os.fsync, os.unlink
        state = {"n": 0}

        def dir_fsync_fails(fd):
            state["n"] += 1
            if state["n"] == 1:
                return real_fsync(fd)
            raise OSError("simulated: directory fsync failed")

        def unlink_fails_for_both(path, *args, **kwargs):
            if path in (backup.name, temp_name):
                raise OSError("simulated: unlink failed")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", dir_fsync_fails)
        monkeypatch.setattr(os, "unlink", unlink_fails_for_both)
        with pytest.raises(STATE_ERROR) as info:
            repair.write_backup_atomic(str(backup), SNAP)
        monkeypatch.undo()
        assert temp_name in str(info.value) and info.value.publication_state == "published"


class TestCorrectionF2UncertainWord:
    @pytest.mark.parametrize("state", ["published", "not published"])
    def test_confirmed_states_never_say_uncertain(self, state):
        assert "uncertain" not in str(STATE_ERROR("d", publication_state=state, db_outcome="rolled back")).lower()

    def test_the_unconfirmed_state_says_uncertain(self):
        assert "unconfirmed (uncertain)" in str(STATE_ERROR("d", publication_state="unconfirmed"))


class TestCorrectionF3FirstCliLine:
    def test_a_state_carrying_error_says_repair_failed_not_backup_write_failed(self, monkeypatch, capsys):
        exc = _exc("commit rejected")
        exc.publication_state, exc.db_outcome = "not published", "rolled back"
        _, out = _cli_text(monkeypatch, capsys, exc)
        assert "! repair failed: commit rejected" in out and "backup write failed" not in out

    def test_an_error_without_state_still_says_backup_write_failed(self, monkeypatch, capsys):
        _, out = _cli_text(monkeypatch, capsys, _exc("disk full"))
        assert "! backup write failed: disk full" in out and "repair failed" not in out


# ---- F4: the places where an exception reaches the caller without the two attributes -----------------


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows, self._scalar = rows or [], scalar

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, *, begin_error=None, exit_error=None, rows=1):
        self.begin_error, self.exit_error, self.rows = begin_error, exit_error, rows
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if self.exit_error is not None:
            raise self.exit_error
        return False

    def begin(self):
        if self.begin_error is not None:
            raise self.begin_error
        conn = self

        class _Trans:
            def commit(self):
                conn.committed = True

            def rollback(self):
                pass

        return _Trans()

    def execute(self, statement):
        sql = str(statement)
        if "count" in sql.lower() and "update" not in sql.lower():
            return _FakeResult(scalar=self.rows)
        return _FakeResult(rows=[
            {"node_id": f"n{i}", "prior_pack_id": "None", "metadata": "{}", "xmin": "1"} for i in range(self.rows)])


class _FakeEngine:
    class url:  # noqa: N801
        host = "h"
        port = 5432

    def __init__(self, conn=None, connect_error=None):
        self.conn, self.connect_error = conn, connect_error

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        return self.conn


def _repair_cli(monkeypatch, capsys, engine, argv_extra):
    monkeypatch.setattr(repair, "connect", lambda dsn: _EngineForMain(engine))
    monkeypatch.setattr(repair, "target_identity_reason", lambda e: None)
    monkeypatch.setattr(repair, "detect", lambda e, t: [])
    monkeypatch.setattr(repair, "audit", lambda e, t: [])
    code = repair.main(["--pg-url", "postgresql://u:p@h/db", "--table", "t", "--apply", *argv_extra])
    return code, capsys.readouterr().out


class _EngineForMain:
    """main() opens one throw-away connection to test reachability, then repair() uses the real stub."""

    class url:  # noqa: N801
        host = "h"
        port = 5432

    def __init__(self, engine):
        self._engine = engine
        self._first = True

    def connect(self):
        if self._first:
            self._first = False

            class _Probe:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _Probe()
        return self._engine.connect()


class TestCorrectionF4AttributesAreNotAlwaysPresent:
    """Three places let an OSError reach the caller without publication_state and db_outcome: the connect call
    and the begin call (before the transaction) and the connection context exit (after the block)."""

    def _assert_unknown_wording(self, out):
        assert "publication state unknown" in out and "database outcome unknown" in out
        assert "rolled back" not in out and "preserved" not in out

    def test_connect_failure(self, monkeypatch, capsys):
        engine = _FakeEngine(connect_error=OSError("simulated: connect failed"))
        code, out = _repair_cli(monkeypatch, capsys, engine, ["--skip-backup"])
        assert code == repair.EXIT_BACKUP
        self._assert_unknown_wording(out)

    def test_begin_failure(self, monkeypatch, capsys):
        engine = _FakeEngine(_FakeConn(begin_error=OSError("simulated: begin failed")))
        code, out = _repair_cli(monkeypatch, capsys, engine, ["--skip-backup"])
        assert code == repair.EXIT_BACKUP
        self._assert_unknown_wording(out)

    def test_context_exit_failure_after_a_normal_commit_with_no_backup(self, monkeypatch, capsys):
        """--skip-backup: the repair committed, no backup exists, and the exit error carries no attributes."""
        conn = _FakeConn(exit_error=OSError("simulated: close failed"))
        code, out = _repair_cli(monkeypatch, capsys, _FakeEngine(conn), ["--skip-backup"])
        assert conn.committed and code == repair.EXIT_BACKUP
        self._assert_unknown_wording(out)

    def test_context_exit_failure_with_a_requested_backup_but_no_target_rows(self, monkeypatch, capsys, tmp_path):
        conn = _FakeConn(exit_error=OSError("simulated: close failed"), rows=0)
        code, out = _repair_cli(monkeypatch, capsys, _FakeEngine(conn), ["--backup-to", str(tmp_path / "b.json")])
        assert conn.committed and code == repair.EXIT_BACKUP
        assert not (tmp_path / "b.json").exists()
        self._assert_unknown_wording(out)

    def test_a_non_oserror_from_these_places_is_not_handled_by_the_oserror_clause(self, monkeypatch, capsys):
        """The CLI clause handles OSError only. Another type propagates, so no wording is promised for it."""
        engine = _FakeEngine(connect_error=RuntimeError("simulated: not an OSError"))
        with pytest.raises(RuntimeError, match="not an OSError"):
            _repair_cli(monkeypatch, capsys, engine, ["--skip-backup"])
