"""B11: 다섯 파일 모드(SINGLE_SQLITE_FILE 없음)의 프로세스는 단일 SQLite root를 거부한다(#671).

단일 root는 ledger.json이나 single.db가 있는 디렉터리다. SINGLE_SQLITE_FILE 없이 그 디렉터리를
LOCAL_DATA_DIR로 받은 코드는 파일을 만들거나 열기 전에 거부해야 한다. 이 파일의 test_t11_ 함수가
변이 표(설계 8절)의 기대 RED를 낸다. 모든 거부 시험은 root의 항목 목록과 모든 파일의 sha256이
거부 전과 같은지 확인한다.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_DIM = 4
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_FALLBACK_MESSAGE = "single SQLite root requires SINGLE_SQLITE_FILE"
_VARIANTS = [("ledger.json",), ("single.db",), ("ledger.json", "single.db")]
_VARIANT_IDS = ["ledger", "single", "both"]
_KINDS = ["graph", "vector", "doc", "sql", "billing"]


def _hash_embed(texts):
    """네트워크와 모델 내려받기를 쓰지 않는 4차원 해시 임베딩."""
    vectors = []
    for text in texts:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        vectors.append([digest[i] / 255.0 for i in range(_DIM)])
    return vectors


def _msg() -> str:
    from opencrab.stores import _single_file

    return getattr(_single_file, "_SINGLE_ROOT_MESSAGE", _FALLBACK_MESSAGE)


@pytest.fixture(autouse=True)
def _fresh_single_registry(monkeypatch):
    """시험마다 factory의 경로별 등록부를 비우고, 끝나면 남은 공유 객체를 닫는다."""
    from opencrab.stores import factory

    registry: dict = {}
    monkeypatch.setattr(factory, "_SINGLE_FILES", registry, raising=False)
    yield registry
    for shared in list(registry.values()):
        for owner in list(shared._users):
            shared.release(owner)
        if not shared._closed:
            shared.close()


def _snapshot(root: Path) -> dict[str, str]:
    """root 아래 모든 항목의 이름과 내용 해시(링크는 대상 문자열)를 모은다."""
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            key = str(path.relative_to(root))
            if path.is_symlink():
                result[key] = "link:" + os.readlink(path)
            elif path.is_dir():
                result[key] = "dir"
            else:
                result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _synthetic_root(base: Path, names=("ledger.json", "single.db"), dirname="root") -> Path:
    """표식 이름만 빈 파일로 둔 디렉터리. 검사는 존재만 본다."""
    root = base / dirname
    root.mkdir()
    for name in names:
        (root / name).write_bytes(b"")
    return root


def _settings(root: Path, **overrides):
    from opencrab.config import Settings

    values = {"STORAGE_MODE": "local", "LOCAL_DATA_DIR": str(root), "EMBEDDING_BACKEND": "openai", "EMBED_DIM": _DIM}
    values.update(overrides)
    return Settings(**values)


def _env(monkeypatch, root: Path) -> None:
    """get_settings()가 SINGLE_SQLITE_FILE 없는 다섯 파일 설정을 읽게 하고 임베딩을 해시로 바꾼다."""
    from opencrab.config import get_settings
    from opencrab.stores import factory

    monkeypatch.setenv("LOCAL_DATA_DIR", str(root))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: _hash_embed)


def _call(kind: str, settings):
    from opencrab.stores import factory

    if kind == "billing":
        return factory.make_billing_sql_store(settings, None)
    return getattr(factory, f"make_{kind}_store")(settings)


def _expect_rejected_and_unchanged(root: Path, action) -> None:
    before = _snapshot(root)
    with pytest.raises(RuntimeError, match=re.escape(_msg())):
        action()
    assert _snapshot(root) == before


def _authorized(tmp_path, monkeypatch):
    from tests.test_factory_single_file import _authorized_root

    return _authorized_root(tmp_path, monkeypatch)


# ----------------------------------------------------------------------------------------------
# 정상
# ----------------------------------------------------------------------------------------------


def test_t11_n1_plain_directory_with_packs_builds_five_factories(tmp_path, monkeypatch):
    """T11-N1: 표식이 없는 평범한 디렉터리(packs/와 임의 파일이 있다)에서 다섯 factory가 만들어진다."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "packs").mkdir()
    (data / "notes.txt").write_text("x")
    _env(monkeypatch, data)
    settings = _settings(data)
    made = []
    try:
        for kind in _KINDS:
            made.append(_call(kind, settings))
    finally:
        for store in reversed(made):
            store.close()
    assert len(made) == len(_KINDS)
    names = set(os.listdir(data))
    assert {"graph.db", "doc_store.db", "vectors.db", "opencrab.db", "billing.db"} <= names, names


def test_t11_n2_authorized_root_single_mode_builds_all_five(tmp_path, monkeypatch):
    """T11-N2: 게시한 authorized root와 SINGLE_SQLITE_FILE로 다섯 factory가 만들어지고 billing.db가 root에 있다."""
    from opencrab.stores import factory
    from tests.test_factory_single_file import _close_all, _make_four, _point_at, _single_settings

    built = _authorized(tmp_path, monkeypatch)
    _point_at(monkeypatch, built.root)
    settings = _single_settings(built.root)
    graph, docs, vec, sql = _make_four(settings)
    billing = factory.make_billing_sql_store(settings, sql)
    try:
        assert billing.shared_file is None
        assert all(store.shared_file is sql.shared_file for store in (graph, docs, vec))
    finally:
        billing.close()
        _close_all((graph, docs, vec, sql))
    assert (built.root / "billing.db").is_file()


def test_t11_n3_allow_single_root_opens_marked_directory_and_default_rejects(tmp_path):
    """T11-N3: allow_single_root=True는 표식 디렉터리에서 열리고 기본값은 거부한다. 같은 디렉터리 두 호출이다."""
    from opencrab.stores.sql_store import SQLStore

    root = _synthetic_root(tmp_path)
    url = f"sqlite:///{root / 'billing.db'}"
    store = SQLStore(url, create_tables=False, allow_single_root=True)
    store.close()
    before = _snapshot(root)
    with pytest.raises(RuntimeError, match=re.escape(_msg())):
        SQLStore(url, create_tables=False)
    assert _snapshot(root) == before


def test_t11_n4_memory_database_passes_the_guard(tmp_path, monkeypatch):
    """T11-N4: ':memory:'와 DB 경로가 없는 sqlite URL은 root 검사에 걸리지 않는다."""
    from opencrab.stores._single_file import reject_unmanaged_single_file
    from opencrab.stores.sql_store import SQLStore

    assert reject_unmanaged_single_file(":memory:") is None
    store = SQLStore("sqlite://", create_tables=False)
    store.close()
    # 작업 디렉터리가 단일 root여도 ':memory:'는 파일이 아니므로 첫 줄에서 반환해야 한다.
    root = _synthetic_root(tmp_path)
    monkeypatch.chdir(root)
    before = _snapshot(root)
    assert reject_unmanaged_single_file(":memory:") is None
    assert _snapshot(root) == before


def test_t11_n5_marker_table_direct_open_keeps_shared_message(tmp_path):
    """T11-N5: 표식 표가 있는 단일 파일을 직접 열면 기존 메시지로 거부된다. 입력 이름은 root 표식 이름이 아니다."""
    from opencrab.stores._single_file import reject_unmanaged_single_file

    path = tmp_path / "unified.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.close()
    with pytest.raises(RuntimeError, match="requires shared connection"):
        reject_unmanaged_single_file(str(path))


# ----------------------------------------------------------------------------------------------
# 오류: factory 다섯 분기
# ----------------------------------------------------------------------------------------------


def _factory_rejects(tmp_path, monkeypatch, variant, kind):
    root = _synthetic_root(tmp_path, variant)
    _env(monkeypatch, root)
    settings = _settings(root)
    _expect_rejected_and_unchanged(root, lambda: _call(kind, settings))


@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
def test_t11_e1_graph_factory_rejects_marked_directory(tmp_path, monkeypatch, variant):
    """T11-E1: 그래프 factory가 거부하고 root가 변하지 않는다."""
    _factory_rejects(tmp_path, monkeypatch, variant, "graph")


@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
def test_t11_e2_vector_factory_rejects_marked_directory(tmp_path, monkeypatch, variant):
    """T11-E2: sqlite-vec 벡터 factory가 거부하고 root가 변하지 않는다."""
    _factory_rejects(tmp_path, monkeypatch, variant, "vector")


@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
def test_t11_e3_doc_factory_rejects_marked_directory(tmp_path, monkeypatch, variant):
    """T11-E3: 문서 factory가 거부하고 root가 변하지 않는다."""
    _factory_rejects(tmp_path, monkeypatch, variant, "doc")


@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
def test_t11_e4_sql_factory_rejects_marked_directory(tmp_path, monkeypatch, variant):
    """T11-E4: 관계형 factory가 거부하고 root가 변하지 않는다."""
    _factory_rejects(tmp_path, monkeypatch, variant, "sql")


@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
def test_t11_e5_billing_factory_rejects_marked_directory(tmp_path, monkeypatch, variant):
    """T11-E5: billing factory(다섯 파일 분기)가 거부하고 root가 변하지 않는다."""
    _factory_rejects(tmp_path, monkeypatch, variant, "billing")


def test_t11_e6_chroma_branch_rejects_before_creating_anything(tmp_path, monkeypatch):
    """T11-E6: chroma 분기가 거부한다. chroma 디렉터리와 chroma.lock이 생기지 않는다."""
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    settings = _settings(root, EMBEDDING_BACKEND="local", VECTOR_BACKEND="chroma")
    assert settings.vector_backend_resolved == "chroma"
    _expect_rejected_and_unchanged(root, lambda: _call("vector", settings))
    assert not (root / "chroma").exists()
    assert not (root / "chroma.lock").exists()


@pytest.mark.parametrize("kind", ["graph", "doc", "vector", "sql"])
def test_t11_e7_direct_constructors_reject_any_file_name_in_root(tmp_path, kind):
    """T11-E7: 직접 생성자 넷이 root 안 임의 파일 이름에서 거부한다."""
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    root = _synthetic_root(tmp_path)
    path = str(root / "anything.db")
    builders = {
        "graph": lambda: LocalGraphStore(path),
        "doc": lambda: LocalSQLDocStore(path),
        "vector": lambda: SqliteVecStore(path, _hash_embed, _DIM),
        "sql": lambda: SQLStore(f"sqlite:///{path}"),
    }
    _expect_rejected_and_unchanged(root, builders[kind])


def test_t11_e8_bootstrap_on_empty_rejects_before_lock_and_database(tmp_path, monkeypatch):
    """T11-E8: bootstrap-on-empty가 bootstrap.lock과 opencrab.db를 만들기 전에 거부한다."""
    from opencrab.auth import maybe_bootstrap_on_empty

    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
    monkeypatch.chdir(tmp_path)
    _expect_rejected_and_unchanged(root, maybe_bootstrap_on_empty)
    names = set(os.listdir(root))
    assert "bootstrap.lock" not in names and "opencrab.db" not in names


@pytest.mark.parametrize("state", ["authorized", "ready"])
def test_t11_e9_published_root_rejects_all_five_factories(tmp_path, monkeypatch, state):
    """T11-E9: 실제 게시한 root(ready와 authorized)를 SINGLE_SQLITE_FILE 없이 받으면 다섯 factory가 거부한다."""
    from tests.test_factory_single_file import _write_ledger_state

    built = _authorized(tmp_path, monkeypatch)
    if state == "ready":
        _write_ledger_state(built.root, "ready")
    _env(monkeypatch, built.root)
    settings = _settings(built.root)
    ledger_before = (built.root / "ledger.json").read_bytes()
    for kind in _KINDS:
        _expect_rejected_and_unchanged(built.root, lambda kind=kind: _call(kind, settings))
    assert (built.root / "ledger.json").read_bytes() == ledger_before


def _wal_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def test_t11_e10_mixed_directory_with_wal_graph_db_gets_no_sidecars(tmp_path):
    """T11-E10: 표식 이름과 WAL graph.db가 섞인 root. 거부 뒤에도 -wal과 -shm이 0이고 graph.db가 같다.

    대조 단계: root 밖 사본에 _single_marker를 mode=ro로 부르면 곁파일이 실제로 생긴다. 안 생기면 이
    시험은 표식 검사가 root 검사보다 앞서는 변이를 잡지 못하므로 실패한다.
    """
    from opencrab.stores import _single_file
    from opencrab.stores.local_graph_store import LocalGraphStore

    root = _synthetic_root(tmp_path)
    _wal_database(root / "graph.db")
    assert sorted(os.listdir(root)) == ["graph.db", "ledger.json", "single.db"]

    control = tmp_path / "control"
    control.mkdir()
    (control / "graph.db").write_bytes((root / "graph.db").read_bytes())
    uri = (control / "graph.db").resolve().as_uri() + "?mode=ro"
    _single_file._single_marker(uri, 1.0)
    assert any(name.endswith(("-wal", "-shm")) for name in os.listdir(control)), os.listdir(control)

    before = _snapshot(root)
    with pytest.raises(RuntimeError, match=re.escape(_msg())):
        LocalGraphStore(str(root / "graph.db"))
    after = _snapshot(root)
    assert not [name for name in after if name.endswith(("-wal", "-shm"))], sorted(after)
    assert after == before


@pytest.mark.parametrize("variant", ["dir_link", "db_link"])
def test_t11_e11_symlinked_paths_are_rejected(tmp_path, monkeypatch, variant):
    """T11-E11: LOCAL_DATA_DIR가 root로 가는 링크이거나 DB 파일이 root 안으로 가는 링크이면 거부한다."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    root = _synthetic_root(tmp_path)
    if variant == "dir_link":
        link = tmp_path / "link"
        link.symlink_to(root)
        _env(monkeypatch, link)
        settings = _settings(link)
        _expect_rejected_and_unchanged(root, lambda: _call("graph", settings))
    else:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "graph.db").symlink_to(root / "graph.db")
        _expect_rejected_and_unchanged(root, lambda: LocalGraphStore(str(plain / "graph.db")))


def test_t11_e12_message_appears_once_and_names_the_directory(tmp_path):
    """T11-E12: 거부 메시지는 상수와 한 번 일치하고 디렉터리 경로를 담는다. 상수는 설계가 정한 문장이다."""
    from opencrab.stores import _single_file
    from opencrab.stores.local_graph_store import LocalGraphStore

    assert getattr(_single_file, "_SINGLE_ROOT_MESSAGE", None) == _FALLBACK_MESSAGE
    root = _synthetic_root(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        LocalGraphStore(str(root / "graph.db"))
    text = str(excinfo.value)
    assert text.count(_msg()) == 1, text
    assert str(root) in text, text


# ----------------------------------------------------------------------------------------------
# 오류: 백업, init, 스크립트
# ----------------------------------------------------------------------------------------------


class _BackupSettings:
    """백업 목록이 읽는 필드만 가진 설정 대역."""

    def __init__(self, local_data_dir: str) -> None:
        self.vector_db_file = "vectors.db"
        self.local_data_dir = local_data_dir
        self.single_sqlite_file = ""


def _backup_case(tmp_path, monkeypatch, variant):
    """(data_dir, dest 또는 None, 지켜야 할 root)를 돌려준다."""
    if variant == "a_default_dest":
        root = _synthetic_root(tmp_path)
        return root, None, root
    if variant == "b_to_elsewhere":
        root = _synthetic_root(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        return root, out, root
    plain = tmp_path / "plain"
    plain.mkdir()
    if variant == "c_dest_synthetic":
        root = _synthetic_root(tmp_path)
    else:
        root = _authorized(tmp_path, monkeypatch).root
    return plain, root, root


_BACKUP_VARIANTS = ["a_default_dest", "b_to_elsewhere", "c_dest_synthetic", "c_dest_published"]


@pytest.mark.parametrize("variant", _BACKUP_VARIANTS)
def test_t11_e13_backup_data_dir_rejects_single_root(tmp_path, monkeypatch, variant):
    """T11-E13: 단일 root가 data_dir이거나 목적지이면 BackupError다. write.lock, staging, 백업 세트가 생기지 않는다."""
    from opencrab.stores import backup as bk

    data_dir, dest, root = _backup_case(tmp_path, monkeypatch, variant)
    before = _snapshot(root)
    with pytest.raises(bk.BackupError, match=re.escape(_msg())):
        bk.backup_data_dir(data_dir, dest, settings=_BackupSettings(str(data_dir)))
    assert _snapshot(root) == before
    assert not (root / "write.lock").exists()
    assert not (data_dir / "write.lock").exists()
    if data_dir != root:
        assert os.listdir(data_dir) == [], os.listdir(data_dir)


@pytest.mark.parametrize("variant", _BACKUP_VARIANTS)
def test_t11_e13_backup_cli_exits_nonzero_for_single_root(tmp_path, monkeypatch, capsys, variant):
    """T11-E13(CLI): 백업 CLI가 0이 아닌 종료 코드와 메시지를 내고 root가 변하지 않는다."""
    from opencrab.config import get_settings
    from opencrab.stores import backup as bk

    data_dir, dest, root = _backup_case(tmp_path, monkeypatch, variant)
    monkeypatch.setenv("LOCAL_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    get_settings.cache_clear()
    argv = ["--data-dir", str(data_dir)] + (["--to", str(dest)] if dest is not None else [])
    before = _snapshot(root)
    code = bk._main(argv)
    err = capsys.readouterr().err
    assert code != 0, err
    assert _msg() in err, err
    assert _snapshot(root) == before


def test_t11_e14_init_rejects_single_root_without_touching_it(tmp_path, monkeypatch):
    """T11-E14: SINGLE_SQLITE_FILE 없이 단일 root에서 opencrab init을 하면 거부한다. 작업 디렉터리는 root 밖이다."""
    from click.testing import CliRunner

    from opencrab.cli import main

    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    before = _snapshot(root)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError), repr(result.exception)
    assert _msg() in str(result.exception)
    assert _snapshot(root) == before


def _script(name: str):
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    return importlib.import_module(name)


@pytest.mark.parametrize("argv", [[], ["--dry-run"]], ids=["apply", "dry_run"])
def test_t11_e15_migrate_chroma_script_rejects_before_chroma_paths(tmp_path, monkeypatch, argv):
    """T11-E15: migrate_chroma_to_sqlite_vec.main()이 chroma 디렉터리와 chroma.lock을 만들기 전에 거부한다."""
    pytest.importorskip("sqlite_vec")
    module = _script("migrate_chroma_to_sqlite_vec")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    monkeypatch.setattr(sys, "argv", ["migrate_chroma_to_sqlite_vec.py", *argv])
    _expect_rejected_and_unchanged(root, module.main)
    assert not (root / "chroma").exists()


@pytest.mark.parametrize("variant", ["skip_graph", "dry_run"])
def test_t11_e16_migrate_to_local_rejects_before_preflight(tmp_path, monkeypatch, variant):
    """T11-E16: migrate_to_local._run()이 preflight와 백업과 makedirs 앞에서 거부한다."""
    module = _script("migrate_to_local")
    root = _synthetic_root(tmp_path)

    def _preflight_must_not_run(args):
        raise AssertionError("preflight ran before the single root guard")

    monkeypatch.setattr(module, "preflight", _preflight_must_not_run)
    args = SimpleNamespace(
        local_data_dir=str(root), skip_graph=(variant == "skip_graph"), dry_run=(variant == "dry_run")
    )
    _expect_rejected_and_unchanged(root, lambda: module._run(args))


def test_t11_e17_import_obsidian_vault_rejects_before_lock_and_docs(tmp_path, monkeypatch):
    """T11-E17: import_vault()가 write.lock과 docs 디렉터리를 만들기 전에 거부한다."""
    import contextlib

    import opencrab.auth as auth

    module = _script("import_obsidian_vault")
    root = _synthetic_root(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(auth, "require_local_principal", lambda: object())
    monkeypatch.setattr(auth, "principal_scope", lambda principal: contextlib.nullcontext())

    def run():
        module.import_vault(vault, "bolt://127.0.0.1:1", "u", "p", "db", root)

    _expect_rejected_and_unchanged(root, run)
    assert not (root / "write.lock").exists() and not (root / "docs").exists()


def test_t11_n6_under_root_check_walks_every_ancestor_of_the_target(tmp_path):
    """T11-N6: reject_single_root_under는 대상 경로와 그 모든 상위 디렉터리를 abspath와 realpath로 본다."""
    from opencrab.stores._single_file import reject_single_root_under

    root = _synthetic_root(tmp_path)
    (root / "inner").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "out").symlink_to(root / "inner", target_is_directory=True)
    other = tmp_path / "other" / "deep"
    other.mkdir(parents=True)
    before = _snapshot(root)
    assert reject_single_root_under(other / "file.jsonl") is None
    assert reject_single_root_under(plain / "file.jsonl") is None
    for target in (root, root / "a" / "b" / "file.jsonl", plain / "out" / "file.jsonl"):
        with pytest.raises(RuntimeError, match=re.escape(_msg())):
            reject_single_root_under(target)
    assert _snapshot(root) == before


# ----------------------------------------------------------------------------------------------
# 오류: 설계 4판이 유보했던 스크립트 넷(R-B11-12)
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--apply", "--check-guard"], ids=["apply", "check_guard"])
def test_t11_e18_repair_store_mismatch_rejects_before_write_lock(tmp_path, monkeypatch, flag):
    """T11-E18: repair_store_mismatch_499의 apply와 --check-guard가 write.lock을 만들기 전에 거부한다."""
    module = _script("repair_store_mismatch_499")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    argv = [
        "--local-data-dir", str(root),
        "--run-dir", str(tmp_path / "run"),
        "--pack-input-root", str(tmp_path / "inputs"),
        flag,
    ]  # fmt: skip
    _expect_rejected_and_unchanged(root, lambda: module.main(argv))
    assert not (root / "write.lock").exists()


@pytest.mark.parametrize("entry", ["apply", "prepare_rollback", "rollback"])
def test_t11_e19_cleanup_heartbeat_rejects_before_load_and_write_lock(tmp_path, monkeypatch, entry):
    """T11-E19: cleanup_openclaw_heartbeat_493의 apply, prepare_rollback, rollback이 write.lock 앞에서 거부한다."""
    module = _script("cleanup_openclaw_heartbeat_493")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    run_dir, backup_dir = tmp_path / "run", tmp_path / "backup"
    calls = {
        "apply": lambda: module.apply(root, run_dir, "vec", None, backup_dir),
        "prepare_rollback": lambda: module.prepare_rollback(root, run_dir, backup_dir),
        "rollback": lambda: module.rollback(root, run_dir, backup_dir),
    }
    _expect_rejected_and_unchanged(root, calls[entry])
    assert not (root / "write.lock").exists()


class _ReachedNeo4jError(Exception):
    """거부 지점을 지나 Neo4j 연결 단계에 닿았다는 표지."""


def _reach_neo4j(*args, **kwargs):
    raise _ReachedNeo4jError


@pytest.mark.parametrize("variant", ["a_default_output", "b_explicit_under_root"])
def test_t11_e20_export_pack_graph_rejects_before_output_mkdir(tmp_path, monkeypatch, variant):
    """T11-E20: export_pack_graph_from_neo4j가 출력 디렉터리를 만들기 전에 거부한다. 기본 출력과 root 아래 명시 출력이다."""
    pytest.importorskip("neo4j")
    module = _script("export_pack_graph_from_neo4j")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    argv = ["export_pack_graph_from_neo4j.py"]
    if variant == "b_explicit_under_root":
        argv += ["--output", str(root / "deep" / "stage" / "out.jsonl")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "make_driver", _reach_neo4j)
    _expect_rejected_and_unchanged(root, module.main)
    assert not (root / "packs").exists() and not (root / "deep").exists()


@pytest.mark.parametrize("variant", ["a_default_stage", "b_explicit_stage_under_root"])
def test_t11_e22_import_pack_graph_rejects_before_connecting_or_writing_status(tmp_path, monkeypatch, variant):
    """T11-E22: import_pack_graph_to_neo4j가 Neo4j에 닿기 전에 거부한다. 상태 파일은 stage의 부모에 쓰인다."""
    module = _script("import_pack_graph_to_neo4j")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    argv = ["import_pack_graph_to_neo4j.py"]
    if variant == "b_explicit_stage_under_root":
        argv += ["--stage", str(root / "deep" / "stage")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "Neo4jStore", _reach_neo4j)
    _expect_rejected_and_unchanged(root, module.main)
    assert not (root / "packs").exists() and not (root / "deep").exists()


@pytest.mark.parametrize("command", ["backfill-pack-id", "source-fts-orphans"])
def test_t11_e23_pack_admin_cli_rejects_before_write_lock(tmp_path, monkeypatch, command):
    """T11-E23: packs backfill-pack-id와 source-fts-orphans의 --apply가 write.lock을 잡기 전에 거부한다.

    두 명령은 graph.db나 doc_store.db가 있어야 잠금에 닿는다. 표식이 있는 디렉터리에 그 파일을 함께 둔 혼합
    디렉터리로 시험한다.
    """
    from click.testing import CliRunner

    from opencrab.cli import main

    root = _synthetic_root(tmp_path, ("ledger.json", "graph.db", "doc_store.db"))
    _env(monkeypatch, root)
    before = _snapshot(root)
    result = CliRunner().invoke(main, ["packs", command, "--apply"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError), repr(result.exception)
    assert _msg() in str(result.exception)
    assert not (root / "write.lock").exists()
    assert _snapshot(root) == before


def test_t11_e24_backfill_space_rejects_before_missing_check_and_lock(tmp_path, monkeypatch, capsys):
    """T11-E24: backfill_space.main이 단일 root에서 write.lock과 저장소 파일 열기 앞에 종료 코드 2로 거부한다."""
    module = _script("backfill_space")
    root = _synthetic_root(tmp_path, ("ledger.json", "graph.db", "doc_store.db", "vectors.db", "opencrab.db"))
    _env(monkeypatch, root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = _snapshot(root)
    code = module.main(["--dry-run-scratch", str(scratch)])
    err = capsys.readouterr().err
    assert code == 2, err
    assert _msg() in err, err
    assert _snapshot(root) == before
    assert not (root / "write.lock").exists()
    assert list(scratch.iterdir()) == []


@pytest.fixture
def _real_crabharness(monkeypatch):
    """이 체크아웃의 crabharness 패키지를 import 경로 맨 앞에 두고, 끝나면 모듈 등록부를 되돌린다."""
    repo_root = Path(__file__).resolve().parent.parent
    removed = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "crabharness" or name.startswith("crabharness.")
    }
    monkeypatch.syspath_prepend(str(repo_root / "crabharness"))
    yield
    for name in list(sys.modules):
        if name == "crabharness" or name.startswith("crabharness."):
            del sys.modules[name]
    sys.modules.update(removed)


def _write_crabharness_package(tmp_path: Path) -> str:
    import json

    package = {
        "package_id": "pkg-1",
        "mission_id": "mis-1",
        "run_id": "run-1",
        "nodes": [{"space": "resource", "node_type": "Dataset", "node_id": "ds1", "properties": {}}],
        "edges": [],
    }
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    return str(path)


def test_t11_e25_crabharness_apply_rejects_before_write_lock(tmp_path, monkeypatch, _real_crabharness):
    """T11-E25: crabharness의 apply_promotion_package가 write.lock을 잡기 전에 단일 root를 거부한다."""
    from crabharness.apply import apply_promotion_package

    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    package = _write_crabharness_package(tmp_path)
    _expect_rejected_and_unchanged(root, lambda: apply_promotion_package(package))
    assert not (root / "write.lock").exists()


@pytest.mark.parametrize("entry", ["crabharness_apply", "source_fts_orphans_apply"])
def test_t11_g10_single_mode_still_reaches_write_lock(tmp_path, monkeypatch, _real_crabharness, entry):
    """T11-G10: 단일 모드에서는 root의 write.lock이 정상 구성원이다.

    crabharness apply는 SINGLE_SQLITE_FILE이 있을 때 B11 거부 지점을 건너뛰고(hold_single_file 뒤) 잠금에
    닿는다. write_lock을 가로채 닿았는지 본다. packs source-fts-orphans --apply는 B1~B3 이후 단일 모드에서
    명시 문구로 write.lock 앞에서 거부한다(#671 D10). 이 경우 잠금에 닿지 않는다.
    """
    import shutil

    from opencrab import locking
    from tests.test_factory_single_file import _single_env

    built = _authorized(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    reached = []

    class _ReachedLockError(Exception):
        pass

    real_write_lock = locking.write_lock

    def spy(*args, **kwargs):
        reached.append(1)
        if entry == "crabharness_apply":
            raise _ReachedLockError
        return real_write_lock(*args, **kwargs)

    monkeypatch.setattr(locking, "write_lock", spy)
    if entry == "crabharness_apply":
        from crabharness.apply import apply_promotion_package

        with pytest.raises(_ReachedLockError):
            apply_promotion_package(_write_crabharness_package(tmp_path))
    else:
        from click.testing import CliRunner

        from opencrab.cli import main

        shutil.copy2(tmp_path / "live" / "doc_store.db", built.root / "doc_store.db")
        result = CliRunner().invoke(main, ["packs", "source-fts-orphans", "--apply"])
        assert result.exit_code == 1, (result.exit_code, result.output, result.exception)
        assert "SINGLE_SQLITE_FILE is set" in " ".join(result.output.split())
        assert not reached
        return
    assert reached


@pytest.mark.parametrize("variant", ["a_defaults_via_main", "b_output_root_under_root", "c_local_data_dir_is_root"])
def test_t11_e21_build_nemotron_rejects_before_stage_mkdir(tmp_path, monkeypatch, variant):
    """T11-E21: build_nemotron_personas_korea_pack이 stage 디렉터리를 만들기 전에 거부한다."""
    pytest.importorskip("pyarrow")
    module = _script("build_nemotron_personas_korea_pack")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if variant == "a_defaults_via_main":
        monkeypatch.setattr(sys, "argv", ["build_nemotron_personas_korea_pack.py"])
        action = module.main
    else:
        out_root, local_dir = (root / "packs" / "x", elsewhere) if variant.startswith("b_") else (elsewhere / "p", root)
        args = SimpleNamespace(
            dataset_root=str(tmp_path / "dataset"), output_root=str(out_root), local_data_dir=str(local_dir),
            reset=False, reset_ingest=False, no_zip=True, progress_every=1, sqlite_batch_nodes=1,
        )  # fmt: skip
        action = lambda: module.build(args)  # noqa: E731
    _expect_rejected_and_unchanged(root, action)
    assert not (root / "packs").exists()
    assert not (elsewhere / "p").exists()


def test_t11_g9_script_targets_outside_the_root_pass_the_guard(tmp_path, monkeypatch):
    """T11-G9: LOCAL_DATA_DIR이 단일 root여도 root 밖으로 쓰는 스크립트 출력은 거부되지 않는다(거짓 거부 0).

    export는 root 밖 명시 출력에서 Neo4j 연결 단계에, build는 root 밖 출력과 root 밖 local-data-dir에서
    parquet 검사 단계에 닿는다. 닿았는지는 각 단계의 고유한 예외로 가린다.
    """
    pytest.importorskip("neo4j")
    pytest.importorskip("pyarrow")
    export = _script("export_pack_graph_from_neo4j")
    build = _script("build_nemotron_personas_korea_pack")
    root = _synthetic_root(tmp_path)
    _env(monkeypatch, root)
    before = _snapshot(root)
    monkeypatch.setattr(sys, "argv", ["export_pack_graph_from_neo4j.py", "--output", str(tmp_path / "o" / "x.jsonl")])
    monkeypatch.setattr(export, "make_driver", _reach_neo4j)
    with pytest.raises(_ReachedNeo4jError):
        export.main()
    args = SimpleNamespace(
        dataset_root=str(tmp_path / "dataset"), output_root=str(tmp_path / "p"), local_data_dir=str(tmp_path / "ld"),
        reset=False, reset_ingest=False, no_zip=True, progress_every=1, sqlite_batch_nodes=1,
    )  # fmt: skip
    with pytest.raises(RuntimeError, match="expected 9 parquet shards"):
        build.build(args)
    imp = _script("import_pack_graph_to_neo4j")
    monkeypatch.setattr(sys, "argv", ["import_pack_graph_to_neo4j.py", "--stage", str(tmp_path / "s" / "stage")])
    monkeypatch.setattr(imp, "Neo4jStore", _reach_neo4j)
    with pytest.raises(_ReachedNeo4jError):
        imp.main()
    assert _snapshot(root) == before


# ----------------------------------------------------------------------------------------------
# 엣지
# ----------------------------------------------------------------------------------------------


def test_t11_g1_unreadable_directory_fails_closed_and_missing_passes(tmp_path, monkeypatch):
    """T11-G1: lstat이 PermissionError를 내면 거부한다. FileNotFoundError와 NotADirectoryError는 없음으로 본다."""
    from opencrab.stores._single_file import reject_single_root

    plain = tmp_path / "plain"
    plain.mkdir()
    (tmp_path / "afile").write_text("x")
    assert reject_single_root(str(plain)) is None
    assert reject_single_root(str(tmp_path / "missing")) is None
    assert reject_single_root(str(tmp_path / "afile" / "sub")) is None
    real = os.lstat

    def denied(path, *args, **kwargs):
        if str(path).endswith("ledger.json"):
            raise PermissionError(13, "denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    with pytest.raises(RuntimeError, match=re.escape(_msg())):
        reject_single_root(str(plain))


@pytest.mark.parametrize("kind", ["directory", "broken_link"])
def test_t11_g2_marker_as_directory_or_broken_link_still_rejects(tmp_path, kind):
    """T11-G2: ledger.json이 디렉터리이거나 깨진 링크여도 거부한다."""
    from opencrab.stores._single_file import reject_single_root

    root = tmp_path / "root"
    root.mkdir()
    if kind == "directory":
        (root / "ledger.json").mkdir()
    else:
        (root / "ledger.json").symlink_to(tmp_path / "nowhere")
    with pytest.raises(RuntimeError, match=re.escape(_msg())):
        reject_single_root(str(root))


def test_t11_g3_relative_database_path_in_root_is_rejected(tmp_path, monkeypatch):
    """T11-G3: cwd가 root일 때 상대 경로 DB도 거부한다."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    root = _synthetic_root(tmp_path)
    monkeypatch.chdir(root)
    _expect_rejected_and_unchanged(root, lambda: LocalGraphStore("graph.db"))


def test_t11_g5_rolled_back_directory_without_markers_passes(tmp_path, monkeypatch):
    """T11-G5: 롤백 모양. 단일 root를 옮기고 표식 없는 다섯 파일 디렉터리를 그 자리에 두면 다섯 factory가 통과한다."""
    live = _synthetic_root(tmp_path, dirname="live")
    live.rename(tmp_path / "moved")
    live.mkdir()
    _env(monkeypatch, live)
    settings = _settings(live)
    made = []
    try:
        for kind in _KINDS:
            made.append(_call(kind, settings))
    finally:
        for store in reversed(made):
            store.close()
    assert len(made) == len(_KINDS)


def test_t11_g6_absolute_vector_db_file_into_root_is_rejected(tmp_path, monkeypatch):
    """T11-G6: LOCAL_DATA_DIR는 평범한 디렉터리이고 VECTOR_DB_FILE이 root 안 절대 경로이면 벡터 생성이 거부된다."""
    root = _synthetic_root(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    _env(monkeypatch, plain)
    settings = _settings(plain, VECTOR_DB_FILE=str(root / "vectors.db"))
    _expect_rejected_and_unchanged(root, lambda: _call("vector", settings))


def test_t11_g7_default_call_shape_rejects_marked_directory(tmp_path):
    """T11-G7: allow_single_root를 모르는 기존 호출 형태가 표식 디렉터리에서 거부된다.

    SQLStore(url)와 검사 함수 reject_unmanaged_single_file(path)(그래프, 문서, 벡터 생성자와 스크립트의
    호출 형태) 둘이다. 기본값이 False로 고정되어 있어야 두 형태가 모두 거부한다.
    """
    from opencrab.stores._single_file import reject_unmanaged_single_file
    from opencrab.stores.sql_store import SQLStore

    root = _synthetic_root(tmp_path)
    _expect_rejected_and_unchanged(root, lambda: SQLStore(f"sqlite:///{root / 'x.db'}"))
    _expect_rejected_and_unchanged(root, lambda: reject_unmanaged_single_file(str(root / "x.db")))


@pytest.mark.parametrize("variant", ["a_root_holds_link_out", "b_outside_holds_link_in"])
def test_t11_g8_symlinked_database_is_checked_through_both_parents(tmp_path, variant):
    """T11-G8: abspath 부모와 realpath 부모를 각각 가르는 입력이다.

    a: root/graph.db가 root 밖 파일의 링크다. abspath 부모(root)만 root를 본다.
    b: 평범한 디렉터리의 graph.db가 root 안 파일의 링크다. realpath 부모(root)만 root를 본다.
    """
    from opencrab.stores.local_graph_store import LocalGraphStore

    root = _synthetic_root(tmp_path)
    if variant == "a_root_holds_link_out":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "graph.db").write_bytes(b"")
        (root / "graph.db").symlink_to(outside / "graph.db")
        path = root / "graph.db"
    else:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "graph.db").symlink_to(root / "graph.db")
        path = plain / "graph.db"
    _expect_rejected_and_unchanged(root, lambda: LocalGraphStore(str(path)))
