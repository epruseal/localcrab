"""#671 B4: 단일 root 안 항목 경로 시험.

설계 8판 8절의 시험 목록이 정본이다. 함수 접두사는 test_t13_다. 합성 root는
tests/test_factory_single_file.py의 보조 함수를 재사용하고, packs/는 시험이 authorized root에
직접 만든다(운영의 항목 이동을 모사한다. authorize 뒤라 이동 순서 규칙을 어기지 않는다).
제품 코드는 바꾸지 않는다. 이 시험은 코드가 항목 경로를 root 하나에서 만든다는 성질을 고정한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_factory_single_file import (  # noqa: F401  (_isolate_single_registry는 autouse 고정물이다)
    _authorized_and_pointed,
    _close_all,
    _make_four,
    _single_env,
    _single_settings,
)
from tests.test_factory_single_file import (
    _fresh_single_registry as _isolate_single_registry,  # noqa: F401
)
from tests.test_single_mode_auth_lock_order import (
    _assert_new_names,
    _listing,
)
from tests.test_single_mode_auth_lock_order import (
    fresh_tool_context as _tool_context,  # noqa: F401  (MCP 도구 컨텍스트를 비우는 고정물이다)
)

# manifest에만 있는 고유 토큰. SQL packs 표와 제목에는 없다. auto_pack 문턱(점수 10)을 넘기려고
# 키워드 둘(5점씩)과 태그 하나(4점)를 둔다.
_TOKEN = "t13zorbquux"
_TOKEN2 = "t13quuxzorb"
_TAG = "t13tag"
_QUESTION = f"{_TOKEN} {_TOKEN2} {_TAG}"
_OTHER_ID = "t13-outside-scope"
_NO_PACK_DELETE = "single SQLite deletion is not yet supported"

# 읽기 경로가 root에 만들면 안 되는 이름과 만들 수 있는 새 이름.
# 설계 8판의 후보에는 잠금 파일 둘이 없었다. 구현 때 실측으로 packs list(CLI 저장소 열기)가 write.lock을,
# content_pack_list(MCP 컨텍스트의 billing 저장소)가 billing.db.lock을 만든다는 것이 확인되어
# 허용 집합에 넣고 금지 집합에서 write.lock을 뺐다(구현 보고에 기록했다). 항목 경로 단언(packs/,
# delete-pack-*, 옛 다섯 파일, .env)은 그대로다.
FORBIDDEN_READ = frozenset(
    {"opencrab.db", "graph.db", "doc_store.db", "vectors.db", "bootstrap.lock",
     "delete-pack-journals", ".env"}
)
ALLOWED_READ_NEW = frozenset(
    {"single.db-wal", "single.db-shm", "billing.db-wal", "billing.db-shm", "write.lock", "billing.db.lock"}
)


# ---------------------------------------------------------------------------
# 보조 함수
# ---------------------------------------------------------------------------


def _put_manifests(root: Path, entries) -> None:
    """entries의 각 (pack_id, keywords, tags)로 root/packs/<pack_id>/stage/manifest.json을 쓴다."""
    for pack_id, keywords, tags in entries:
        stage = root / "packs" / pack_id / "stage"
        stage.mkdir(parents=True)
        manifest = {"pack_id": pack_id, "title": f"T13 title {pack_id}", "version": "1",
                    "keywords": list(keywords), "tags": list(tags)}
        (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _prepare(tmp_path, monkeypatch, *, packs: bool = True):
    """authorized 단일 root를 만들고 환경을 단일 모드로 맞춘다. packs가 참이면 manifest 둘을 둔다."""
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(tmp_path / "no-such.env"))
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    if packs:
        _put_manifests(built.root, [(built.pack_id, [_TOKEN, _TOKEN2], [_TAG]), (_OTHER_ID, ["t13other"], [])])
    return built


def _owner(built):
    from opencrab.auth import Principal

    return Principal(user_id=built.owner, is_local=True, disabled=False)


def _wide_console(monkeypatch) -> None:
    """CLI 표가 줄을 접지 않게 한다. 출력 문자열 단언이 폭에 기대지 않게 하는 고정물이다."""
    from rich.console import Console

    from opencrab import cli

    monkeypatch.setattr(cli, "console", Console(width=1000))


def _invoke(*args):
    from click.testing import CliRunner

    from opencrab.cli import main

    return CliRunner().invoke(main, list(args))


def _content_pack_list(built, **arguments):
    from opencrab.auth import principal_scope
    from opencrab.mcp.tools import dispatch_tool

    with principal_scope(_owner(built)):
        return dispatch_tool("content_pack_list", arguments)


def _resolve(built, question: str):
    from opencrab.auth import principal_scope
    from opencrab.config import get_settings
    from opencrab.pack.read_scope import read_scope
    from opencrab.services.pack_selection import resolve_packs

    stores = _make_four(_single_settings(built.root))
    try:
        sql = stores[3]
        principal = _owner(built)
        with principal_scope(principal):
            scope = read_scope(sql, principal)
            return resolve_packs(
                question, None, True, False, get_settings().local_data_dir,
                scope=scope, raise_on_error=True, sql=sql, principal=principal,
            )
    finally:
        _close_all(stores)


def _squash(text: str) -> str:
    """공백을 모두 지운 문자열. 출력이 줄을 접어도 경로 단언이 흔들리지 않는다."""
    return "".join(text.split())


# ---------------------------------------------------------------------------
# 정상
# ---------------------------------------------------------------------------


def test_t13_n1_registry_reads_root_packs(tmp_path, monkeypatch):
    """T13-N1: 단일 모드에서 settings.local_data_dir가 root이고 레지스트리가 root/packs를 읽는다."""
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import get_pack, load_pack_registry

    built = _prepare(tmp_path, monkeypatch)
    local_data_dir = get_settings().local_data_dir
    assert os.path.realpath(local_data_dir) == os.path.realpath(built.root)
    registry = load_pack_registry(local_data_dir)
    assert sorted(info.pack_id for info in registry) == sorted([built.pack_id, _OTHER_ID])
    packs_dir = os.path.realpath(built.root / "packs")
    for info in registry:
        assert os.path.realpath(info.path).startswith(packs_dir + os.sep), info.path
    again = get_pack(local_data_dir, built.pack_id)
    assert again is not None
    assert again.pack_id == built.pack_id
    assert again.keywords == [_TOKEN, _TOKEN2]


def test_t13_n2a_packs_list_reads_root_packs(tmp_path, monkeypatch):
    """T13-N2a: packs list가 root/packs를 읽고 read_scope 밖 manifest는 거르며 종료 코드 0이다."""
    built = _prepare(tmp_path, monkeypatch)
    _wide_console(monkeypatch)
    result = _invoke("packs", "list")
    assert result.exit_code == 0, result.output
    assert built.pack_id in result.output
    assert f"T13 title {built.pack_id}" in result.output
    assert _OTHER_ID not in result.output


def test_t13_n2b_packs_show_reads_root_packs(tmp_path, monkeypatch):
    """T13-N2b: packs show가 manifest를 읽고 scope 밖 pack_id는 not found 종료 코드 1이다."""
    built = _prepare(tmp_path, monkeypatch)
    _wide_console(monkeypatch)
    shown = _invoke("packs", "show", built.pack_id)
    assert shown.exit_code == 0, shown.output
    info = json.loads(shown.output)
    assert info["pack_id"] == built.pack_id
    assert info["title"] == f"T13 title {built.pack_id}"
    missing = _invoke("packs", "show", _OTHER_ID)
    assert missing.exit_code == 1, missing.output
    assert "not found" in missing.output


@pytest.mark.usefixtures("_tool_context")
def test_t13_n3_content_pack_list_enrichment(tmp_path, monkeypatch):
    """T13-N3: manifest keywords에만 있는 토큰으로 content_pack_list가 그 팩을 돌려준다."""
    built = _prepare(tmp_path, monkeypatch)
    result = _content_pack_list(built, query=_TOKEN)
    assert [pack["pack_id"] for pack in result["packs"]] == [built.pack_id], result


def test_t13_n4_resolve_packs_enrichment(tmp_path, monkeypatch):
    """T13-N4: resolve_packs auto_pack이 manifest keywords의 토큰으로 그 팩을 고른다."""
    built = _prepare(tmp_path, monkeypatch)
    selection = _resolve(built, _QUESTION)
    assert [item["pack_id"] for item in selection.selected_packs] == [built.pack_id], selection
    assert selection.effective_pack_ids == [built.pack_id]


def test_t13_n5_journal_and_lock_paths(tmp_path, monkeypatch):
    """T13-N5: 삭제 저널과 이름 있는 잠금의 경로가 root 하나에서 만들어진다(파일을 만들지 않는 순수 단언).

    기대값은 제품 도우미(_slug, journal_path, lock_filename)를 쓰지 않고 리터럴과 sha256으로 직접 만든다.
    기대값을 도우미로 만들면 접두사 변이가 실제 값과 기대값을 함께 바꿔 살아남는다.
    """
    from opencrab import locking
    from opencrab.pack import delete_journal

    built = _prepare(tmp_path, monkeypatch)
    name = "t13-journal-pack"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    data_dir = locking.lock_data_dir()
    real_root = os.path.realpath(built.root)
    assert os.path.realpath(data_dir) == real_root
    journal = delete_journal.journal_path(data_dir, name)
    assert os.path.realpath(journal) == os.path.join(real_root, "delete-pack-journals", f"{digest}.json")
    lock = locking._lock_path(delete_journal.lock_filename(name), data_dir)
    assert lock == os.path.join(real_root, f"delete-pack-{digest}.lock")
    assert not os.path.lexists(journal)
    assert not os.path.lexists(lock)


# ---------------------------------------------------------------------------
# 오류와 경계
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_tool_context")
def test_t13_e1_packs_absent_is_enrichment_loss_only(tmp_path, monkeypatch):
    """T13-E1: packs/가 없으면 오류도 거부도 아니고 manifest 보강만 빠진다. 읽기가 packs/를 만들지 않는다."""
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import load_pack_registry

    built = _prepare(tmp_path, monkeypatch, packs=False)
    before = _listing(built.root)
    assert not (built.root / "packs").exists()
    assert load_pack_registry(get_settings().local_data_dir) == []
    assert not (built.root / "packs").exists()
    _wide_console(monkeypatch)
    listed = _invoke("packs", "list")
    assert listed.exit_code == 0, listed.output
    squashed = _squash(listed.output)
    assert "Nopacksunder" in squashed
    assert _squash(f"{built.root}/packs/") in squashed
    plain = _content_pack_list(built)
    assert [pack["pack_id"] for pack in plain["packs"]] == [built.pack_id], plain
    enriched = _content_pack_list(built, query=_TOKEN)
    assert enriched["packs"] == [], enriched
    _assert_new_names(before, _listing(built.root), ALLOWED_READ_NEW)
    assert not (built.root / "packs").exists()


def test_t13_e2_single_mode_delete_stays_refused(tmp_path, monkeypatch):
    """T13-E2: 단일 모드 delete_pack은 첫 쓰기 앞에서 거부되고 root에 저널과 삭제 잠금이 생기지 않는다."""
    from opencrab.auth import principal_scope
    from opencrab.pack.load import delete_pack

    built = _prepare(tmp_path, monkeypatch)
    graph, docs, vec, sql = _make_four(_single_settings(built.root))
    try:
        before = _listing(built.root)
        with principal_scope(_owner(built)):
            with pytest.raises(RuntimeError, match=_NO_PACK_DELETE):
                delete_pack(built.pack_id, graph, docs, vec, sql=sql)
        after = _listing(built.root)
    finally:
        _close_all((graph, docs, vec, sql))
    assert "delete-pack-journals" not in after
    assert not [name for name in after if name.startswith("delete-pack-")], sorted(after)
    _assert_new_names(before, after, ALLOWED_READ_NEW)


# ---------------------------------------------------------------------------
# 불변
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_tool_context")
@pytest.mark.parametrize("variant", ["packs_present", "packs_absent"])
def test_t13_g1_read_paths_leave_root_entries_alone(tmp_path, monkeypatch, variant):
    """T13-G1: 읽기 경로 다섯을 한 root에서 모두 부른 뒤 root 항목 집합이 허용 새 이름 밖에서 전과 같다."""
    from opencrab import locking
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import get_pack, load_pack_registry
    from opencrab.pack import delete_journal

    built = _prepare(tmp_path, monkeypatch, packs=(variant == "packs_present"))
    before = _listing(built.root)
    local_data_dir = get_settings().local_data_dir
    load_pack_registry(local_data_dir)
    get_pack(local_data_dir, built.pack_id)
    _wide_console(monkeypatch)
    assert _invoke("packs", "list").exit_code == 0
    _invoke("packs", "show", built.pack_id)
    _content_pack_list(built, query=_TOKEN)
    _resolve(built, _QUESTION)
    delete_journal.journal_path(locking.lock_data_dir(), "t13-g1")
    after = _listing(built.root)
    _assert_new_names(before, after, ALLOWED_READ_NEW)
    assert not (after & FORBIDDEN_READ), sorted(after & FORBIDDEN_READ)
    if variant == "packs_absent":
        assert "packs" not in after
