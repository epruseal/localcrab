"""``delete_pack`` 재개 저널 — RED (#327).

`delete_pack` 은 graph/doc/vector 세 스토어를 **따로** 커밋한다(크로스 스토어 단일
트랜잭션이 없다). 중간에 죽으면 부분 삭제가 남는데, 그 상태를 "이전 중단의 잔재" 로
식별해 완주하거나 명시 중단할 계약이 없었다 — 이 파일이 그 계약을 고정한다.

설계 원문은 스크래치(`design-v5.md`, 5라운드 codex 검증 대상)에 있고, 팀리드가 직접
재정한 두 핵심 결정은 다음과 같다(이 파일 전체가 그 결정을 전제한다):

  1. 크래시를 가로지르는 **정확한 누적 삭제 건수 복원은 요구하지 않는다.** 재개
     실행은 이번 실행에서 확인한 건수만 내고, 이전 중단 실행의 건수는 "알 수 없음"
     으로 명시한다.
  2. 축의 재실행 여부는 **`done` 플래그로만** 판정한다. `count` 가 채워져 있어도
     그것을 "이미 실행됨" 의 근거로 쓰지 않는다 — graph 삭제 루프가 노드별 예외를
     삼키고 진행하므로, 크래시 없이 정상 종료해도 일부만 지워진 채 `count` 가
     채워질 수 있기 때문이다(§8-4/§9(b)).
  3. 팩 동일성 확인은 `packs` 레지스트리 행의 **PK 기반 생존**을 안전 증명으로
     쓴다(§3) — `created_at` 같은 초 단위 약한 신호에 기대지 않는다. 저널 생성
     시점에 행이 있었고 재개 시점에도 그대로 있으면 자동 재개, 그렇지 않으면(행이
     없었거나/사라졌거나/식별 불가) 명시 중단한다.

**이 커밋은 RED 전용이다.** 아래가 요구하는 `opencrab.pack.delete_journal` 모듈은
아직 없다 — 그래서 이 파일은 수집(collection) 단계에서부터 실패한다. 이것이 이번
커밋이 고정하려는 RED 상태다. GREEN 커밋이 그 모듈과 `delete_pack` 의 확장을 더해야
이 파일이 수집되고, 그 다음 아래 개별 단언이 하나씩 통과해야 한다.

## GREEN 이 만족해야 할 계약 (이 파일이 강제)

- ``opencrab.pack.delete_journal`` 모듈 신설. 최소 표면:
  - ``JOURNAL_SCHEMA``(정수), 예외 ``DeletePackJournalUnverifiable``,
    ``DeletePackJournalConflict``, ``DeletePackJournalCorrupt``.
  - ``journal_path(data_dir, pack_name) -> Path`` — 파일명은 ``pack_name`` 을
    안전하게(해시로) 인코딩한다(팩 이름에 유니코드/구분자가 와도 안전해야 한다).
  - ``lock_filename(pack_name) -> str`` — ``delete_pack`` 자신이 실행 전체에 거는
    ``opencrab.locking.file_lock`` 의 파일명과 **동일한 이름**을 산출한다(테스트가
    같은 락을 바깥에서 잡아 경합을 관측할 수 있어야 한다).
  - ``load_journal(data_dir, pack_name) -> dict | None`` — 없으면 `None`, 읽을 수
    없으면(파싱 실패/스키마 불일치) ``DeletePackJournalCorrupt``.
  - ``save_journal(data_dir, pack_name, payload) -> None`` — 임시파일 + fsync +
    ``os.replace`` 원자적 교체(선례: opencrab-dump #9 rename 저널과 동형).
  - ``clear_journal(data_dir, pack_name) -> None``.
- ``delete_pack(pack_name, graph, docs, vec, *, sql=None)`` — 새 키워드 인자
  ``sql``(``load_chunks`` 의 기존 ``sql=`` 관례와 동형). 함수 전체를
  ``file_lock(delete_journal.lock_filename(pack_name))`` 로 감싼다.
- 각 축(`doc.node_twin_loop`, `doc.doc_node_extra`/`doc_sources`, `graph.graph_nodes`,
  `vectors`)은 단일 원자적 쓰기로 ``{done, count/clean}`` 을 저널에 남기고, `done`
  하나로만 재실행 여부를 정한다.
- `sql` 이 주어지고 저널이 새로 만들어질 때 ``get_pack(sql, pack_name)`` 으로 팩
  동일성 스냅샷을 남기고, 기존 저널을 만나 재개할 때 다시 조회해 그 스냅샷과 대조한다
  (§3 4가지 경로 — 아래 ``TestPackIdentityResumeSafety`` 가 전부 고정한다).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from opencrab.pack import load as pack_load
from opencrab.pack import delete_journal          # noqa: F401 -- 아직 없다. RED.
from opencrab.pack.ownership import create_pack, delete_pack_row, get_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

from tests._pack_fixtures import ensure_test_user
from tests.test_pack_load import (  # noqa: F401 -- 기존 픽스처·더블 재사용
    _FakeChromaCollection,
    _FakeChromaVec,
    _NoVec,
    _node,
    _write_jsonl,
)

_OWNER = "delete-journal-test-owner"


@pytest.fixture
def pack_sql(tmp_path):
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    ensure_test_user(sql, _OWNER)
    return sql


@pytest.fixture
def live(tmp_path, monkeypatch, pack_sql):
    """진짜 SQLite 3스토어. `pack_sql` 자동 등록은 없다 — 각 테스트가
    `create_pack`/`sql=None` 여부를 직접 고른다(팩 동일성 확인이 이 파일의 핵심이라,
    "등록된 팩" 과 "등록 안 된 팩" 을 테스트마다 구분해서 만들어야 한다)."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    yield graph, docs
    graph.close()
    docs.close()


def _seed_pack(graph, docs, tmp_path, pack_name: str, node_ids: list[str]):
    """`pack_name` 소유 노드 `node_ids` 를 그래프에 심는다(`OntologyBuilder` 없이
    `pack_load.load_nodes` 를 그대로 쓴다 — `test_pack_load.py` 와 동일 경로)."""
    from opencrab.auth import Principal, principal_scope
    from opencrab.ontology.builder import OntologyBuilder

    rows = [_node(id=nid, pack_id=pack_name) for nid in node_ids]
    f = _write_jsonl(tmp_path / f"{pack_name}-nodes.jsonl", rows)
    principal = Principal(user_id=_OWNER, is_local=True, disabled=False)
    with principal_scope(principal):
        builder = OntologyBuilder(graph, docs, None)
        pack_load.load_nodes(pack_name, f, builder, {})


def _live_node_ids(graph, node_ids: list[str]) -> set[str]:
    return {nid for nid in node_ids if graph.get_node("Document", nid) is not None}


# ---------------------------------------------------------------------------
# 1. 저널 원자성 — 쓰기 도중 죽어도 부분 기록을 읽고 잘못 판단하지 않는다.
# ---------------------------------------------------------------------------

class TestJournalAtomicity:
    def test_replace_failing_midwrite_leaves_the_previous_journal_intact(
        self, tmp_path, monkeypatch
    ):
        """`os.replace` 직전에 죽는다(v3/v4 §8-3 carry-forward, monkeypatch 방식 —
        여기는 실 SIGKILL 이 아니라 삽입 예외로 충분하다: 원자성은 프로세스 생사가
        아니라 "무엇을 먼저 쓰고 무엇을 나중에 바꿔치기하는가" 의 문제이기 때문이다).

        역변이: `save_journal` 이 임시파일 없이 최종 경로에 직접 쓰면, 이 테스트가
        만드는 죽은 쓰기가 최종 파일을 반쯤 쓴 채로 남기고, 아래 재조회가 그 반쪽을
        읽어 `DeletePackJournalCorrupt` 대신 조용히 틀린 값을 얻는다(테스트가 그
        차이를 못 잡으면 이 역변이가 생존한다 — 그래서 "읽을 수 있고 이전 값과
        같다" 를 직접 확인한다).
        """
        delete_journal.save_journal(tmp_path, "pack-a", {"schema": delete_journal.JOURNAL_SCHEMA,
                                                          "axes": {"graph_nodes": {"done": True, "count": 3}}})
        before = delete_journal.load_journal(tmp_path, "pack-a")

        import os
        real_replace = os.replace

        def _boom(*a, **kw):
            raise OSError("simulated crash mid os.replace")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            delete_journal.save_journal(
                tmp_path, "pack-a",
                {"schema": delete_journal.JOURNAL_SCHEMA,
                 "axes": {"graph_nodes": {"done": True, "count": 999}}},
            )
        monkeypatch.setattr(os, "replace", real_replace)

        after = delete_journal.load_journal(tmp_path, "pack-a")
        assert after == before, (
            "저널 쓰기가 os.replace 직전에 죽었는데 이전 값이 그대로 안 남았다 — "
            f"before={before!r} after={after!r}"
        )

    def test_a_torn_journal_file_aborts_rather_than_being_silently_treated_as_absent(
        self, tmp_path
    ):
        """파싱 실패를 "저널 없음" 으로 접으면 소유 증거가 사라진 채 재개가 열린다
        (opencrab-dump #9 rename 저널의 `load_journal` 계약과 동형).

        역변이: `load_journal` 이 `json.loads` 실패를 `except Exception: return None`
        으로 삼키면 이 테스트가 실패한다(찢어진 파일을 "새 실행" 으로 오인해 예외
        없이 `None` 을 반환하므로).
        """
        path = delete_journal.journal_path(tmp_path, "pack-torn")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": 1, "axes": {"graph_no', encoding="utf-8")  # 절단됨

        with pytest.raises(delete_journal.DeletePackJournalCorrupt):
            delete_journal.load_journal(tmp_path, "pack-torn")


# ---------------------------------------------------------------------------
# 2. 명시 중단 경로 = dry-run(무쓰기) — 이 부류에서 가장 위험한 회귀.
# ---------------------------------------------------------------------------

class TestExplicitAbortWritesNothing:
    def test_unregistered_pack_journal_on_resume_always_aborts_and_touches_no_row(
        self, live, tmp_path
    ):
        """§3/§8-9(d): 저널 생성 시 레지스트리 행이 없었다(`registered=False`) —
        증명 수단 자체가 없으므로, 재개 시 행이 그 뒤 생겼든 안 생겼든 항상 명시
        중단이고, 이번 호출은 스토어를 **전혀** 건드리지 않는다.

        역변이: 재개 경로가 저장소 축 실행 루프를 먼저 돌고 나서야 동일성을 검사하면,
        중단 예외는 그대로 던져지지만 이미 일부 축이 지워진 뒤라 이 테스트의 "행
        불변" 단언이 잡는다.
        """
        graph, docs = live
        node_ids = ["u1", "u2"]
        _seed_pack(graph, docs, tmp_path, "unreg-pack", node_ids)

        # 1회차: sql 없이 호출 — pack_identity.registered=False 로 저널만 남기고
        # doc 축에서 예외로 죽었다고 가정(실제 크래시 대신 몽키패치로 doc 축 실패
        # 유도 — 이 클래스의 관심사는 "재개 시 중단" 이지 doc 실패 자체가 아니다).
        import opencrab.stores.local_sql_doc_store as doc_mod
        real_delete = doc_mod.LocalSQLDocStore.delete_node_doc

        def _fail_once(self, space, node_id):
            raise RuntimeError("시뮬레이션된 doc 축 실패")

        import unittest.mock as mock
        with mock.patch.object(doc_mod.LocalSQLDocStore, "delete_node_doc", _fail_once):
            with pytest.raises(Exception):  # noqa: PT011 -- doc 축 예외가 밖으로 새는지는 별개 테스트의 관심사
                pack_load.delete_pack("unreg-pack", graph, docs, _NoVec(), sql=None)

        before = _live_node_ids(graph, node_ids)

        # 이제 팩을 등록한다(운영자가 뒤늦게 sql 을 넘겨 재개를 시도하는 상황).
        pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
        ensure_test_user(pack_sql, _OWNER)
        create_pack(pack_sql, _OWNER, "unreg-pack")

        with pytest.raises(delete_journal.DeletePackJournalUnverifiable):
            pack_load.delete_pack("unreg-pack", graph, docs, _NoVec(), sql=pack_sql)

        after = _live_node_ids(graph, node_ids)
        assert after == before, (
            f"증명 수단 없는 저널의 재개 중단인데 행이 바뀌었다: before={before} after={after}"
        )

    def test_registry_row_vanished_between_journal_creation_and_resume_aborts_and_touches_no_row(
        self, live, tmp_path
    ):
        """§3/§8-9(b): 저널 생성 시 행이 있었는데 재개 시 사라졌다 — PK 슬롯 이상
        신호다. `DeletePackJournalConflict`, 스토어 무변형.

        역변이: 재개가 "행이 사라졌으면 그냥 등록 안 된 것과 같다" 고 `registered=
        False` 취급해 조용히 진행하면, 사라짐이라는 더 강한 이상 신호를 삼킨다 —
        이 테스트가 그 구분을 강제한다(예외 타입까지 구분해서 확인).
        """
        graph, docs = live
        node_ids = ["v1", "v2"]
        _seed_pack(graph, docs, tmp_path, "vanish-pack", node_ids)

        pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
        ensure_test_user(pack_sql, _OWNER)
        create_pack(pack_sql, _OWNER, "vanish-pack")
        assert get_pack(pack_sql, "vanish-pack") is not None

        import opencrab.stores.local_sql_doc_store as doc_mod
        import unittest.mock as mock

        def _fail_once(self, space, node_id):
            raise RuntimeError("시뮬레이션된 doc 축 실패")

        with mock.patch.object(doc_mod.LocalSQLDocStore, "delete_node_doc", _fail_once):
            with pytest.raises(Exception):  # noqa: PT011
                pack_load.delete_pack("vanish-pack", graph, docs, _NoVec(), sql=pack_sql)

        # 슬롯 소멸을 흉내낸다 — 오늘 코드에서 유일한 삭제 경로(only_status 없이 강제)
        delete_pack_row(pack_sql, "vanish-pack", _OWNER)
        assert get_pack(pack_sql, "vanish-pack") is None

        before = _live_node_ids(graph, node_ids)
        with pytest.raises(delete_journal.DeletePackJournalConflict):
            pack_load.delete_pack("vanish-pack", graph, docs, _NoVec(), sql=pack_sql)
        after = _live_node_ids(graph, node_ids)
        assert after == before, (
            f"레지스트리 행 소멸 중단인데 행이 바뀌었다: before={before} after={after}"
        )


# ---------------------------------------------------------------------------
# 3. `done` 플래그 단독 판정 — count 가 채워져도 done 이 아니면 재실행된다.
# ---------------------------------------------------------------------------

class TestDoneFlagAloneGatesReexecution:
    def test_partial_node_deletion_without_any_crash_leaves_graph_axis_not_done(
        self, live, tmp_path
    ):
        """그래프 삭제 루프는 노드별 예외를 삼키고 진행한다(load.py, `except
        Exception as exc: deleted = False`) — **크래시 없이 정상 종료해도** 일부만
        지워질 수 있다. `count>0` 이 채워져도 `done` 은 `False` 로 남아야 한다.

        역변이: 저널이 "count 가 기록되면 done=True" 로 판정하면, 이 테스트의 1회차
        직후 단언(`done is False`)이 잡는다 — 그 뒤 영구 정지(재실행 안 됨)까지
        확인할 필요도 없이 여기서 이미 걸린다.
        """
        graph, docs = live
        node_ids = ["p1", "p2", "p3"]
        _seed_pack(graph, docs, tmp_path, "partial-pack", node_ids)

        real_delete_node = graph.delete_node

        def _fail_for_p2(node_type, node_id):
            if node_id == "p2":
                raise RuntimeError("시뮬레이션된 노드 삭제 실패")
            return real_delete_node(node_type, node_id)

        import unittest.mock as mock
        with mock.patch.object(graph, "delete_node", side_effect=_fail_for_p2):
            pack_load.delete_pack("partial-pack", graph, docs, _NoVec())  # 예외 없이 정상 반환(기존 관용 계약)

        journal = delete_journal.load_journal(tmp_path, "partial-pack")
        axis = journal["axes"]["graph_nodes"]
        assert axis["count"] > 0, "p1/p3는 지워졌으니 count는 0보다 커야 한다"
        assert axis["done"] is False, (
            f"일부만 지워졌는데(count={axis['count']}) done=True로 잘못 기록됐다 — "
            "count 존재를 done의 근거로 쓴 것이다(영구 정지 위험)"
        )
        assert graph.get_node("Document", "p2") is not None, "p2는 여전히 남아 있어야 한다"

        # 재실행 — 이번엔 실패 없이(정상 delete_node) 남은 p2까지 마저 지운다.
        pack_load.delete_pack("partial-pack", graph, docs, _NoVec())
        journal2 = delete_journal.load_journal(tmp_path, "partial-pack")
        assert journal2["axes"]["graph_nodes"]["done"] is True
        assert _live_node_ids(graph, node_ids) == set(), "재실행 뒤에도 p2가 남아 있다"


# ---------------------------------------------------------------------------
# 4. 벡터 미확인 상태 — 조회 실패는 done으로 승격되지 않는다.
# ---------------------------------------------------------------------------

class TestVectorUnconfirmedNeverBecomesDone:
    def test_pre_delete_query_unreadable_leaves_vector_axis_not_done(self, live, tmp_path):
        """G22(`test_pack_load.py`)와 같은 판독 불가 응답을 삭제 **전** 조회에
        주입한다. 기존 계약(0건 삭제·delete 미호출)은 그대로 성립해야 하고,
        **추가로** 저널의 vectors 축이 `done=False` 로 남아야 한다 — "카운트
        구조적 미지원" 이 아니라 "이번 조회 실패" 이기 때문이다.

        역변이: `chroma_unreadable` 신호를 무시하고 `count==0` 이면 곧바로
        `done=True` 를 찍으면, 이 테스트의 `done is False` 단언이 잡는다(그 뒤
        실제로 존재하는 벡터가 영원히 재시도 안 되는 것을 이 테스트가 막는다).
        """
        graph, docs = live
        vec = _FakeChromaVec({"a1": "vecpack", "a2": "vecpack"})
        vec._collection.malformed_get_wheres = {1: {"no_ids_key": []}}

        _n, _c, chunk_vec_del = pack_load.delete_pack("vecpack", graph, docs, vec)
        assert chunk_vec_del == 0
        assert not vec._collection.delete_calls

        journal = delete_journal.load_journal(tmp_path, "vecpack")
        vaxis = journal["axes"]["vectors"]
        assert vaxis["clean"] is False
        assert vaxis["done"] is False, (
            f"조회 실패(count=0)가 done=True로 잘못 승격됐다: {vaxis!r}"
        )

        # 복구된 뒤(malformed 없이) 재실행하면 실제로 지워지고 done=True로 수렴한다.
        vec2 = _FakeChromaVec({"a1": "vecpack", "a2": "vecpack"})
        pack_load.delete_pack("vecpack", graph, docs, vec2)
        journal2 = delete_journal.load_journal(tmp_path, "vecpack")
        assert journal2["axes"]["vectors"]["done"] is True
        assert vec2._collection.delete_calls, "복구 후 재실행에서 실제 삭제가 안 일어났다"

    def test_structurally_unsupported_backend_is_done_immediately(self, live, tmp_path):
        """대조군 — `vec.available=False` 는 "카운트 미지원" 이지 "조회 실패" 가
        아니다. 대기할 것이 없으므로 즉시 done=True(#165 기존 계약과 동형).

        역변이: 미지원 백엔드도 vectors 축을 영원히 pending 으로 남기면(위 테스트가
        요구한 "미확인은 done 아님" 을 과잉 적용하면) 이 테스트가 잡는다 — 재개할
        것이 없는 축까지 재실행 대상으로 취급하는 것도 결함이다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "novec-pack", ["z1"])
        pack_load.delete_pack("novec-pack", graph, docs, _NoVec())
        journal = delete_journal.load_journal(tmp_path, "novec-pack")
        assert journal["axes"]["vectors"] == {"done": True, "clean": True, "count": 0}


# ---------------------------------------------------------------------------
# 5. 여러 중단 지점 — 첫 축(doc) 직후, 두 번째 축(graph) 직후는 실 SIGKILL.
# ---------------------------------------------------------------------------

def _run_kill_script(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    repo_root = str(Path(pack_load.__file__).resolve().parents[2])
    script = tmp_path / "kill_mid_delete.py"
    script.write_text(
        textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {repo_root!r})
            {textwrap.indent(textwrap.dedent(body), "            ")}
        """),
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )


class TestMultipleCrashPoints:
    def test_sigkill_right_after_doc_axis_commits_then_resume_finishes_graph_and_vectors(
        self, tmp_path, monkeypatch
    ):
        """1번째 훅 위치: doc 축의 마지막 커밋 직후·단일 원자적 쓰기 이전에 실
        SIGKILL. 재개가 doc을 재확인(멱등, 이미 지워진 그대로)하고 graph·vectors를
        마저 끝낸다.

        역변이: 크래시 훅을 없애 doc 축 커밋과 저널 갱신을 분리하지 않으면(둘을
        하나의 "죽지 않는" 단계로 합치면) 이 크래시 지점 자체가 재현 불가능해져
        테스트가 무의미하게 항상 통과한다 — 그래서 killed(`returncode < 0`)를
        먼저 확인한다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        proc = _run_kill_script(tmp_path, """
            from opencrab.auth import Principal, principal_scope
            from opencrab.ontology.builder import OntologyBuilder
            from opencrab.pack import load as pack_load
            from opencrab.stores.local_graph_store import LocalGraphStore
            from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

            graph = LocalGraphStore(os.path.join({str(tmp_path)!r}, "graph.db"))
            docs = LocalSQLDocStore(os.path.join({str(tmp_path)!r}, "doc.db"))
            principal = Principal(user_id="crash-user", is_local=True, disabled=False)
            with principal_scope(principal):
                builder = OntologyBuilder(graph, docs, None)
                import json
                p = os.path.join({str(tmp_path)!r}, "nodes.jsonl")
                with open(p, "w", encoding="utf-8") as fh:
                    for nid in ("k1", "k2"):
                        fh.write(json.dumps({{"id": nid, "label": "n", "node_type": "Document",
                                              "space": "resource", "pack_id": "crash-pack"}}) + "\\n")
                pack_load.load_nodes("crash-pack", p, builder, {{}})

            from opencrab.pack import delete_journal
            real_save = delete_journal.save_journal
            calls = []

            def killer(data_dir, pack_name, payload):
                calls.append(payload)
                out = real_save(data_dir, pack_name, payload)
                axes = payload.get("axes", {{}})
                if axes.get("doc_node_extra_and_sources", {{}}).get("done") is True and len(calls) >= 1:
                    os.kill(os.getpid(), 9)
                return out

            delete_journal.save_journal = killer

            class _NoVec:
                available = False
                def delete(self, ids): pass

            pack_load.delete_pack("crash-pack", graph, docs, _NoVec())
        """)
        assert proc.returncode < 0, (
            f"doc 축 커밋 직후 죽지 않았다: rc={proc.returncode} stderr={proc.stderr[-2000:]}"
        )

        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        graph = LocalGraphStore(str(tmp_path / "graph.db"))
        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
        try:
            journal_before = delete_journal.load_journal(tmp_path, "crash-pack")
            assert journal_before is not None, "죽은 실행이 저널 자체를 안 남겼다"
            assert journal_before["axes"]["doc_node_extra_and_sources"]["done"] is True
            assert journal_before["axes"]["graph_nodes"].get("done") is not True

            _n, _c, _v = pack_load.delete_pack("crash-pack", graph, docs, _NoVec())
            journal_after = delete_journal.load_journal(tmp_path, "crash-pack")
            assert journal_after["axes"]["graph_nodes"]["done"] is True
            assert journal_after["axes"]["vectors"]["done"] is True
            assert graph.get_node("Document", "k1") is None
            assert graph.get_node("Document", "k2") is None
        finally:
            graph.close()
            docs.close()

    def test_sigkill_right_after_graph_axis_commits_then_resume_finishes_vectors_only(
        self, tmp_path, monkeypatch
    ):
        """2번째 훅 위치: graph 축 완료 직후. 재개가 doc·graph를 신뢰하고(둘 다
        이미 done=True) vectors만 실행한다 — doc/graph를 다시 실행하지 않는다는
        점에서 위 테스트와 검증 축이 다르다(이 테스트는 "이미 done인 축은 건드리지
        않는다"를 고정한다).

        역변이: 재개가 이미 done인 축까지 무조건 다시 도는(멱등성에 기대 "그냥 다
        재실행"으로 단순화한) 구현이면, 이 테스트 자체는 최종 상태로는 못 잡을 수
        있다(멱등이라 결과가 같다) — 그래서 doc 축 실행 카운터를 몽키패치로 세어
        "재개 시 doc 축 함수가 다시 불리지 않았다"를 직접 확인한다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        proc = _run_kill_script(tmp_path, """
            from opencrab.auth import Principal, principal_scope
            from opencrab.ontology.builder import OntologyBuilder
            from opencrab.pack import load as pack_load
            from opencrab.stores.local_graph_store import LocalGraphStore
            from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

            graph = LocalGraphStore(os.path.join({str(tmp_path)!r}, "graph.db"))
            docs = LocalSQLDocStore(os.path.join({str(tmp_path)!r}, "doc.db"))
            principal = Principal(user_id="crash-user", is_local=True, disabled=False)
            with principal_scope(principal):
                builder = OntologyBuilder(graph, docs, None)
                import json
                p = os.path.join({str(tmp_path)!r}, "nodes.jsonl")
                with open(p, "w", encoding="utf-8") as fh:
                    for nid in ("g1", "g2"):
                        fh.write(json.dumps({{"id": nid, "label": "n", "node_type": "Document",
                                              "space": "resource", "pack_id": "crash-pack-2"}}) + "\\n")
                pack_load.load_nodes("crash-pack-2", p, builder, {{}})

            from opencrab.pack import delete_journal
            real_save = delete_journal.save_journal

            def killer(data_dir, pack_name, payload):
                out = real_save(data_dir, pack_name, payload)
                if payload.get("axes", {{}}).get("graph_nodes", {{}}).get("done") is True:
                    os.kill(os.getpid(), 9)
                return out

            delete_journal.save_journal = killer

            class _NoVec:
                available = False
                def delete(self, ids): pass

            pack_load.delete_pack("crash-pack-2", graph, docs, _NoVec())
        """)
        assert proc.returncode < 0, (
            f"graph 축 커밋 직후 죽지 않았다: rc={proc.returncode} stderr={proc.stderr[-2000:]}"
        )

        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        graph = LocalGraphStore(str(tmp_path / "graph.db"))
        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
        try:
            import unittest.mock as mock
            doc_calls = []
            real_delete_node_doc = docs.delete_node_doc

            def _counted(space, node_id):
                doc_calls.append(node_id)
                return real_delete_node_doc(space, node_id)

            with mock.patch.object(docs, "delete_node_doc", side_effect=_counted):
                pack_load.delete_pack("crash-pack-2", graph, docs, _NoVec())

            assert doc_calls == [], (
                f"doc 축이 이미 done인데도 재개가 다시 실행했다: {doc_calls}"
            )
            journal = delete_journal.load_journal(tmp_path, "crash-pack-2")
            assert journal["axes"]["vectors"]["done"] is True
        finally:
            graph.close()
            docs.close()


# ---------------------------------------------------------------------------
# 6. 요약 표시 — 재개 실행의 확인 건수와 "이전 부분 실행 불명" 마킹.
# ---------------------------------------------------------------------------

class TestResumeSummaryText:
    def test_resumed_run_reports_only_this_run_count_and_marks_unknown_prior(
        self, live, tmp_path, capsys
    ):
        """§6: 재개 실행의 출력 요약은 (a) 이번 실행에서 확인한 건수만 내고
        (b) "이전 부분 실행이 있었고 그 건수는 알 수 없다"를 명시하며 (c) 전체
        요약에 "(재개)"를 붙인다.

        역변이: 재개 판정만 하고 요약 문자열을 기존 그대로 두면(내부적으로는
        올바르게 재개해도 운영자에게 보이는 신호가 없으면), 이 테스트가 stdout에서
        그 문구를 못 찾아 잡는다.
        """
        graph, docs = live
        node_ids = ["s1", "s2", "s3"]
        _seed_pack(graph, docs, tmp_path, "summary-pack", node_ids)

        real_delete_node = graph.delete_node

        def _fail_for_s2(node_type, node_id):
            if node_id == "s2":
                raise RuntimeError("시뮬레이션된 노드 삭제 실패")
            return real_delete_node(node_type, node_id)

        import unittest.mock as mock
        with mock.patch.object(graph, "delete_node", side_effect=_fail_for_s2):
            pack_load.delete_pack("summary-pack", graph, docs, _NoVec())
        capsys.readouterr()  # 1회차 출력은 버린다 — 이 테스트는 재개 호출의 출력만 본다

        pack_load.delete_pack("summary-pack", graph, docs, _NoVec())
        out = capsys.readouterr().out

        assert "재개" in out, f"재개 표시가 요약에 없다: {out!r}"
        assert "알 수 없" in out, f"이전 중단분 불명 표시가 요약에 없다: {out!r}"


# ---------------------------------------------------------------------------
# 7. 락 경합 — holder가 쥔 채 contender의 타임아웃/비차단 실패를 직접 관측.
#    (locking.py:465 process-internal RLock, locking.py:489 OS-level flock)
# ---------------------------------------------------------------------------

class TestLockContentionIsReallyObserved:
    def test_inprocess_rlock_contention_blocks_a_different_thread(self, tmp_path, monkeypatch):
        """§8-7(a)/(b): 같은 프로세스의 다른 스레드가 holder가 쥔 동안 짧은
        timeout으로 획득 실패를 직접 관측하고, holder 해제 뒤 즉시 성공한다.

        역변이: `delete_pack`가 `delete_journal.lock_filename(pack_name)` 과
        다른(또는 팩마다 안 갈리는) 락 이름을 쓰면, 이 테스트의 holder가 잡은 락과
        delete_pack이 실제로 기다리는 락이 어긋나 contention이 전혀 관측되지 않는다
        (타임아웃 없이 즉시 통과 — 이 테스트가 그 어긋남을 잡는다).
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        from opencrab import locking

        filename = delete_journal.lock_filename("lockpack")
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def _hold():
            with locking.file_lock(filename, str(tmp_path)):
                holder_ready.set()
                release_holder.wait(timeout=10)

        t = threading.Thread(target=_hold)
        t.start()
        assert holder_ready.wait(timeout=5), "holder가 락을 못 잡았다"

        with pytest.raises(TimeoutError):
            with locking.file_lock(filename, str(tmp_path), timeout=0.2):
                pass  # holder가 쥔 동안이라 여기 못 들어와야 한다

        release_holder.set()
        t.join(timeout=10)

        # holder 해제 뒤에는 즉시 성공한다.
        with locking.file_lock(filename, str(tmp_path), timeout=1.0):
            pass

    def test_delete_pack_itself_blocks_on_the_same_named_lock(self, live, tmp_path, monkeypatch):
        """`delete_pack` 이 실행 전체를 `delete_journal.lock_filename(pack_name)`
        으로 감싼다는 것 자체를 확인한다(위 테스트는 락 메커니즘만, 이 테스트는
        `delete_pack`이 그 메커니즘을 실제로 쓰는지).

        역변이: `delete_pack`이 아예 락을 안 잡으면, holder가 쥔 동안 호출한
        `delete_pack`이 타임아웃 없이 그냥 통과해 버린다 — 이 테스트가 그것을 잡는다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        graph, docs = live
        from opencrab import locking

        filename = delete_journal.lock_filename("busy-pack")
        with locking.file_lock(filename, str(tmp_path)):
            with pytest.raises(TimeoutError):
                pack_load.delete_pack("busy-pack", graph, docs, _NoVec(), lock_timeout=0.2)

    def test_os_level_file_lock_contention_across_processes(self, tmp_path, monkeypatch):
        """§8-7(c): 별도 프로세스가 `locking.py:489` 의 `_acquire()` 수준에서 OS
        파일 락을 쥐고 있으면, 자식 프로세스가 짧은 timeout으로 획득 실패를
        관측하고 holder 종료(락 해제) 뒤 성공한다 — 스레드 내부 RLock이 아니라
        진짜 프로세스 간 flock 경합이다(선례: `test_backup_consistency.py`의
        `test_backup_times_out_when_another_process_holds_write_lock`과 동형).

        역변이: 프로세스 내부 RLock만 잡고 OS flock을 건드리지 않는 구현이면 이
        테스트가 실패한다(자식 프로세스는 별도 프로세스라 RLock을 아예 공유하지
        않으므로, OS flock이 실제로 걸려야만 대기가 생긴다).
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        repo_root = str(Path(pack_load.__file__).resolve().parents[2])
        filename = delete_journal.lock_filename("oslock-pack")
        holder_script = tmp_path / "hold_delete_lock.py"
        holder_script.write_text(textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {repo_root!r})
            from opencrab.locking import acquire_file_lock
            fh = acquire_file_lock({filename!r}, {str(tmp_path)!r})
            print("held", flush=True)
            time.sleep(30)
        """), encoding="utf-8")

        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(holder_script)], stdout=subprocess.PIPE, text=True
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "held"

            from opencrab import locking
            with pytest.raises(TimeoutError):
                with locking.file_lock(filename, str(tmp_path), timeout=1.0):
                    pass
        finally:
            proc.kill()
            proc.wait(timeout=10)

        # holder가 죽어 OS flock이 풀린 뒤에는 성공한다.
        from opencrab import locking
        with locking.file_lock(filename, str(tmp_path), timeout=2.0):
            pass


# ---------------------------------------------------------------------------
# 회귀 대조 — 하위호환(sql 없이 호출)은 이 파일 신설 이후에도 그대로 성립해야 한다.
# ---------------------------------------------------------------------------

class TestBackwardCompatibilityWithoutSql:
    def test_sql_omitted_still_deletes_and_returns_the_same_tuple_shape(self, live, tmp_path):
        """§3 "sql이 안 주어진 호출: 이 절 전체 스킵" — 기존 3만여 호출부
        (`tests/test_pack_load.py` 등)가 `sql=` 을 안 준다. 이 파일이 `delete_pack`
        의 시그니처를 넓혀도 그 호출부 전량이 그대로 통과해야 한다.

        역변이: `sql` 을 선택적이 아니라 필수로 만들면(키워드 인자에 기본값을
        안 주면), 이 테스트를 포함해 기존 호출부 전량이 `TypeError` 로 깨진다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "compat-pack", ["c1"])
        result = pack_load.delete_pack("compat-pack", graph, docs, _NoVec())
        assert isinstance(result, tuple) and len(result) == 3
        node_del, chunk_sql_del, chunk_vec_del = result
        assert node_del == 1
        assert chunk_vec_del == 0
