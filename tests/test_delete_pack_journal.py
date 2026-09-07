"""``delete_pack`` 재개 저널 — RED (#327).

`delete_pack` 은 graph/doc/vector 세 스토어를 **따로** 커밋한다(크로스 스토어 단일
트랜잭션이 없다). 중간에 죽으면 부분 삭제가 남는데, 그 상태를 "이전 중단의 잔재" 로
식별해 완주하거나 명시 중단할 계약이 없었다 — 이 파일이 그 계약을 고정한다.

설계 원문은 스크래치(`design-v5.md`, 5라운드 codex 검증 대상)에 있다. 5라운드 검증이
"팩 동일성의 PK 기반 자동 재개 증명"(v5 §3)을 반례로 깨자, 팀리드가 재정으로 그
자동 재개 자체를 이번 범위에서 들어냈다 — **모든 재개는 운영자의 명시 확인을
요구한다.** 이 파일은 그 재정을 그대로 고정한다:

  1. 크래시를 가로지르는 **정확한 누적 삭제 건수 복원은 요구하지 않는다.** 재개
     실행은 이번 실행에서 확인한 건수만 내고, 이전 중단 실행의 건수는 "알 수 없음"
     으로 명시한다.
  2. 축의 재실행 여부는 **`done` 플래그로만** 판정한다. `count` 가 채워져 있어도
     그것을 "이미 실행됨" 의 근거로 쓰지 않는다 — graph 삭제 루프가 노드별 예외를
     삼키고 진행하므로, 크래시 없이 정상 종료해도 일부만 지워진 채 `count` 가
     채워질 수 있기 때문이다(§8-4/§9(b)).
  3. **기본 동작은 탐지와 보고다.** 저널이 있으면 재실행은 부분 완료 상태를
     읽어서 보고하고 멈춘다 — 스토어를 전혀 건드리지 않는다(`DeletePackJournalPending`).
     보고에는 축별 done/pending 상태, 지금 살아 있는 라이브 카운트
     (`pack_live_counts` 재사용), 그리고 "저널 생성 이후 새 콘텐츠가 들어왔을 수
     있고 재개는 그것도 지운다" 는 창(window) 경고가 담긴다.
  4. **명시 플래그(`resume=True`)가 있을 때만 완주한다.** 운영자가 위 보고를 보고
     결정한다.
  5. **명시 플래그가 있어도 거부하는 경우를 둔다.** `sql` 이 주어졌고 저널 생성
     시점에 레지스트리 행의 `created_at` 스냅샷을 남겼다면, 재개 시점에 그 행이
     사라졌거나 `created_at` 이 다르면 `DeletePackJournalConflict` 로 거부한다 —
     일치는 "같은 팩임의 증명" 이 못 되므로(SQLite `datetime('now')` 초 단위 충돌,
     5라운드 실측) 통과 증거로 쓰지 않고, 불일치만 확실한 부정 신호로 차단에 쓴다.
     저널 생성 시점에 비교 근거가 없었으면(`sql` 미제공 등) 이 검사는 건너뛴다.

**PR #360 리뷰 후속(지적 1/2) — done 축도 무조건 스킵하지 않는다.** 위 4번의
"명시 플래그가 있을 때만 완주한다"는 재정은 그대로다. 다만 `done=True` 저널을
영구 신뢰하면 완료 이후 새로 들어온 콘텐츠나 연결이 복구된 벡터스토어를 영원히
건너뛴다는 결함이 리뷰에서 나왔다. `TestDoneJournalDriftRecheck` 가 그 수정을
고정한다: `doc_node_extra_and_sources` 축은 매 호출 라이브 카운트(고아 `doc_nodes`
포함)를 다시 재고, `done=True` 라도 남은 게 있으면 재시도하고, `vectors` 축은 내용을
재조회하지 않고 모양(`_vec_shape`)과 `available` 속성만으로 "확인 불가"(연결 실패
등)를 재시도 대상으로 남긴다. `available=True` 인 채로 완료된 vectors 축의 사후
드리프트는 범위 밖이다 — 이미 완료·available 한 벡터스토어는 매 재개마다
재조회하지 않는다는 기존 계약(`TestResumeSkipDoesNotReuseCountInReturnValue`)을
우선했다.

**이 커밋은 RED 전용이다.** 아래가 요구하는 `opencrab.pack.delete_journal` 모듈은
아직 없다 — 그래서 이 파일은 수집(collection) 단계에서부터 실패한다. 이것이 이번
커밋이 고정하려는 RED 상태다. GREEN 커밋이 그 모듈과 `delete_pack` 의 확장을 더해야
이 파일이 수집되고, 그 다음 아래 개별 단언이 하나씩 통과해야 한다.

## GREEN 이 만족해야 할 계약 (이 파일이 강제)

- ``opencrab.pack.delete_journal`` 모듈 신설. 최소 표면:
  - ``JOURNAL_SCHEMA``(정수), 예외 ``DeletePackJournalPending``,
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
- ``delete_pack(pack_name, graph, docs, vec, *, sql=None, resume=False,
  lock_timeout=None)`` — 새 키워드 인자. ``sql`` 은 ``load_chunks`` 의 기존
  ``sql=`` 관례와 동형. 함수 전체를
  ``file_lock(delete_journal.lock_filename(pack_name), timeout=lock_timeout)`` 로
  감싼다.
- 축(`node_twin_loop`, `doc_node_extra_and_sources`, `graph_nodes`, `vectors`)은
  단일 원자적 쓰기로 ``{done, count/clean}`` 을 저널에 남기고, `done` 하나로만
  재실행 여부를 정한다. `node_twin_loop` 개별 노드의 `docs.delete_node_doc` 실패는
  기존처럼 삼키되(관용 계약 불변), 하나라도 삼켰으면 sticky 플래그로
  `done=False` 를 고정한다(로컬 지적 7) — `node_twin_loop.done` 이 아니면
  `graph_nodes` 축에 진입하지 않는다(doc→graph 게이팅, v3/v4 carry-forward).
- 벡터 축은 세 갈래다(로컬 지적 4): 애초에 벡터 스토어가 백엔드 모양조차 없으면
  (`_NoVec` 류) 구조적 미지원으로 즉시 `done=True`; 백엔드 모양은 있는데
  `available=False`(연결·초기화 실패)면 재시도로 나을 수 있으므로 `done=False`;
  조회/삭제를 실제로 시도했는데 판독 불가면 "이번 조회 실패"로 `done=False`.
- 기본 호출(저널이 있고 `resume` 미지정)은 `DeletePackJournalPending` 을 던지고
  스토어를 전혀 건드리지 않는다. 메시지에는 축별 상태, `pack_live_counts` 라이브
  카운트, 창 경고, `resume=True` 안내가 담긴다.
- `resume=True` 이고 `sql` 이 주어졌으며 저널에 팩 동일성 스냅샷(`created_at`)이
  있으면, 현재 `get_pack(sql, pack_name)` 과 대조해 행 소멸이나 `created_at` 불일치
  시 `DeletePackJournalConflict` 를 던지고 무쓰기다. 스냅샷이 없으면(비교 근거 없음)
  검사를 건너뛰고 완주한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from opencrab.pack import delete_journal
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import create_pack, delete_pack_row, get_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from tests._pack_fixtures import ensure_test_user
from tests.test_pack_load import (  # noqa: F401 -- 기존 픽스처·더블 재사용
    _FakeChromaCollection,
    _FakeChromaVec,
    _node,
    _NoVec,
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
    `pack_load.load_nodes` 를 그대로 쓴다 — `test_pack_load.py` 와 동일 경로).

    `OntologyBuilder.add_node` 는 `write_gate.authorize` 를 무조건 거친다 — 레지스트리가
    닿지 않거나(`sql=None`) 이 pack_id 가 아직 등록 안 됐으면 fail-closed 로 거부한다
    (`opencrab/pack/write_gate.py::authorize`). 그래서 여기서 직접 등록한다. `pack_sql`
    픽스처를 받지 않는 이유: 대다수 테스트가 이미 `_seed_pack` 뒤에 자기 몫의
    `SQLStore(f"sqlite:///{{tmp_path / 'opencrab.db'}}")` 를 열어 동일성 스냅샷용
    `create_pack`/`get_pack` 을 부른다 — 여기서 만드는 등록은 같은 파일의 같은 행을
    가리키므로 `get_pack` 이 없을 때만 만들어 그 뒤의 `create_pack` 재시도가
    "이미 있음" 으로 걸려 랜덤 접미 슬러그를 만들지 않게 한다."""
    from opencrab.auth import Principal, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack as _create_pack
    from opencrab.pack.ownership import get_pack as _get_pack
    from opencrab.stores.sql_store import SQLStore

    pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    ensure_test_user(pack_sql, _OWNER)
    if _get_pack(pack_sql, pack_name) is None:
        _create_pack(pack_sql, _OWNER, pack_name)

    rows = [_node(id=nid, pack_id=pack_name) for nid in node_ids]
    f = _write_jsonl(tmp_path / f"{pack_name}-nodes.jsonl", rows)
    principal = Principal(user_id=_OWNER, is_local=True, disabled=False)
    with principal_scope(principal):
        builder = OntologyBuilder(graph, docs, pack_sql)
        pack_load.load_nodes(pack_name, f, builder, {})


def _live_node_ids(graph, node_ids: list[str]) -> set[str]:
    return {nid for nid in node_ids if graph.get_node("Document", nid) is not None}


class _ChromaShapedButUnavailable:
    """`_collection` 을 가진(chroma 모양) 벡터 스토어지만 `available=False` —
    로컬 지적 4가 겨냥한 "연결·초기화 실패" 를 흉내낸다. `_NoVec`(그런 속성이
    애초에 없다, 구조적 미지원)과는 다른 부류다 — 이쪽은 재시도하면 나을 수
    있는 상태라 즉시 done으로 확정하면 안 된다."""

    available = False

    def __init__(self):
        self._collection = _FakeChromaCollection({})

    def delete(self, ids):  # pragma: no cover -- available=False라 호출 안 됨
        pass


class _AvailableRaisesOnSecondRead:
    """`available` 이 두 번째 접근에서 예외를 던지는 shaped(chroma 모양)
    벡터스토어 더블 — [리뷰 P2] `delete_pack` 이 드리프트 탐침에서 한 번,
    `_delete_pack_vectors` 내부에서 다시 한 번 `available` 을 읽던 이중 읽기를
    고정한다. `_collection` 속성을 둬 `_vec_shape()` 가 `"chroma"` 를 반환하게
    한다 — 이것이 없으면 드리프트 탐침의 `and` 단락평가로 첫 읽기 자체가
    스킵돼 구 코드에서도 헬퍼 내부가 첫 접근이 되어 이 RED가 성립하지 않는다
    (설계검증 1라운드 DISAGREE 로 확인된 함정 — 반드시 shaped 로 잡혀야 한다).
    수정된 코드에서는 두 번째 읽기 자체가 사라져 실제 chroma 삭제 경로까지
    진행하므로, `_collection` 은 진짜 조회·삭제를 흉내내는 `_FakeChromaCollection`
    이어야 한다(빈 `object()` 로는 `col.get`/`col.delete` 가 `AttributeError` 로
    막혀 결과를 오염시킨다)."""

    def __init__(self):
        self._reads = 0
        self._collection = _FakeChromaCollection({})

    @property
    def available(self):
        self._reads += 1
        if self._reads >= 2:
            raise RuntimeError("second read of available exploded")
        return True

    def delete(self, ids):  # pragma: no cover -- vec 자신이 아니라 `_collection` 이 지운다
        pass


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

    def test_schema_mismatch_is_corrupt_not_a_parse_success(self, tmp_path):
        """파일이 유효한 JSON 이라도 스키마가 없거나 다르면(#327 이전 형식 잔존,
        또는 미래의 새 스키마 저널을 구 버전이 잘못 읽는 경우) `DeletePackJournalCorrupt`
        다 — 위 테스트는 파싱 자체가 실패하는 경로만 잡고, 이 경로는 파싱은
        성공했는데 내용이 저널이 아닌 경우를 잡는다(같은 함수의 다른 raise 지점).

        역변이(mutation testing 실증, #327): `not isinstance(payload, dict) or
        payload.get("schema") != JOURNAL_SCHEMA` 검사 자체를 지우거나 `or` 를
        `and` 로 바꿔도 이 테스트 전에는 아무 테스트도 안 죽었다 — 스키마 검사가
        전혀 실행되지 않아도 스위트가 통과했다는 뜻이다.
        """
        path = delete_journal.journal_path(tmp_path, "pack-badschema")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 999, "axes": {}}), encoding="utf-8")
        with pytest.raises(delete_journal.DeletePackJournalCorrupt):
            delete_journal.load_journal(tmp_path, "pack-badschema")

        path2 = delete_journal.journal_path(tmp_path, "pack-notadict")
        path2.write_text(json.dumps(["schema", 1]), encoding="utf-8")
        with pytest.raises(delete_journal.DeletePackJournalCorrupt):
            delete_journal.load_journal(tmp_path, "pack-notadict")

    def test_clear_journal_actually_removes_the_file_and_is_idempotent(self, tmp_path):
        """`clear_journal` 은 "호출자가 부른다"(자기 docstring)는 유지보수 동작이다
        — 아무 테스트도 이 함수를 직접 부르지 않았다(mutation testing 실증, #327):
        본문을 통째로 지워도, `unlink(missing_ok=True)` 를 `missing_ok=False` 로
        바꿔도 스위트가 그대로 통과했다.

        두 가지를 확인한다: (1) 저널이 있으면 지운 뒤 `load_journal` 이 `None` 을
        돌려준다, (2) 이미 없는 상태에서 다시 불러도 예외 없이 조용히 넘어간다
        (`missing_ok=True` 계약 — 유지보수 스크립트가 두 번 불러도 안전해야 한다).
        """
        delete_journal.save_journal(
            tmp_path, "pack-clear",
            {"schema": delete_journal.JOURNAL_SCHEMA, "axes": {"vectors": {"done": True}}},
        )
        assert delete_journal.load_journal(tmp_path, "pack-clear") is not None

        delete_journal.clear_journal(tmp_path, "pack-clear")
        assert delete_journal.load_journal(tmp_path, "pack-clear") is None

        delete_journal.clear_journal(tmp_path, "pack-clear")  # 이미 없음 — 조용히 통과

    def test_slug_derived_paths_differ_by_pack_name_and_have_a_stable_format(self, tmp_path):
        """`_slug` 가 팩 이름마다 다른 파일을 내야 서로 다른 팩의 저널·락이 한
        파일로 뭉개지지 않는다(mutation testing 실증, #327): `_slug` 의 `return`
        문을 통째로 지워(`None` 반환) 모든 팩 이름이 같은 파일 `"None.json"` 으로
        수렴해도, 어느 테스트도 서로 다른 두 팩 이름으로 경로를 비교하지 않아
        살아남았다.

        길이·문자 집합(32자리 16진수)도 여기서 고정한다 — `[:32]` 슬라이스 경계가
        딴 값으로 바뀌어도 잡을 테스트가 이거 하나뿐이었다.
        """
        import re

        path_a = delete_journal.journal_path(tmp_path, "pack-alpha")
        path_b = delete_journal.journal_path(tmp_path, "pack-beta")
        assert path_a != path_b, "서로 다른 팩 이름이 같은 저널 경로로 뭉개졌다"

        lock_a = delete_journal.lock_filename("pack-alpha")
        lock_b = delete_journal.lock_filename("pack-beta")
        assert lock_a != lock_b, "서로 다른 팩 이름이 같은 락 파일명으로 뭉개졌다"

        assert re.fullmatch(r"delete-pack-[0-9a-f]{32}\.lock", lock_a), lock_a
        assert re.fullmatch(r"[0-9a-f]{32}\.json", path_a.name), path_a.name

    def test_journal_path_lives_under_its_own_subdirectory(self, tmp_path):
        """저널이 `data_dir` 바로 밑이 아니라 `delete-pack-journals/` 서브디렉터리에
        있어야 다른 용도로 쓰는 `data_dir` 최상위 파일들과 안 섞인다
        (mutation testing 실증, #327): 그 서브디렉터리 이름이 빈 문자열로 바뀌어도
        (`Path.__truediv__` 가 빈 문자열 세그먼트를 조용히 무시해 `data_dir` 바로
        밑으로 떨어진다) 어느 테스트도 실제 파일 위치를 확인하지 않아 살아남았다.
        """
        path = delete_journal.journal_path(tmp_path, "pack-loc")
        assert path.parent.name == "delete-pack-journals", path
        assert path.parent.parent == tmp_path, path

    def test_save_journal_creates_missing_parent_directories(self, tmp_path):
        """`data_dir` 자신도 아직 없는 다단계 경로에서 첫 저널을 쓸 수 있어야 한다
        (mutation testing 실증, #327): `path.parent.mkdir(parents=True, ...)` 의
        `parents=True` 가 `False` 로 바뀌어도, 이 스위트의 다른 모든 테스트는
        `tmp_path` 가 이미 있는 픽스처를 쓰므로(한 단계만 만들면 돼 `parents` 값이
        상관없다) 아무도 못 잡았다. 두 단계 이상 없는 경로로만 이 차이가 드러난다.
        """
        deep_dir = tmp_path / "not-yet-created" / "nested"
        assert not deep_dir.exists()
        delete_journal.save_journal(
            deep_dir, "pack-deep",
            {"schema": delete_journal.JOURNAL_SCHEMA, "axes": {"vectors": {"done": True}}},
        )
        assert delete_journal.load_journal(deep_dir, "pack-deep") is not None

    def test_save_journal_closes_the_directory_fsync_file_descriptor(self, tmp_path, monkeypatch):
        """디렉터리 fsync 뒤 그 fd 를 닫아야 한다 — 안 닫으면 `save_journal` 을 반복
        호출하는(모든 `delete_pack` 실행마다) 장수 프로세스가 fd 를 조금씩 새 문다
        (mutation testing 실증, #327): 이 fd 를 여는/닫는 `try/finally` 블록 전체를
        지워도 — fsync 만 사라지는 게 아니라 `os.close(dir_fd)` 호출 자체가 같이
        사라진다 — 정상 경로 테스트는 전부 그대로 통과했다(내용 왕복만 보고 fd 정리는
        아무도 안 봤다).
        """
        import os

        real_close = os.close
        closed: list[int] = []

        def _spy_close(fd):
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "close", _spy_close)
        delete_journal.save_journal(
            tmp_path, "pack-fdspy",
            {"schema": delete_journal.JOURNAL_SCHEMA, "axes": {"vectors": {"done": True}}},
        )
        assert closed, "save_journal 이 디렉터리 fsync 용 fd 를 열고 안 닫았다"

    def test_journal_schema_version_is_1_and_pinned(self, tmp_path):
        """저널 스키마 버전은 1로 고정된다 — 디스크에 남은 이전 실행의 저널과 호환이
        끊기면 안 되므로 이 상수는 실수로 바뀌면 안 된다(mutation testing 실증,
        #327): `JOURNAL_SCHEMA` 상수 자체를 다른 값으로 바꿔도, 그 상수를 참조해
        쓰고 같은 상수를 참조해 읽는 왕복 테스트는 자체 일관이라 못 잡는다.
        여기서는 상수를 거치지 않고 리터럴 `1` 을 파일에 직접 써서 확인한다.
        """
        path = delete_journal.journal_path(tmp_path, "pack-schemapin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "axes": {}}), encoding="utf-8")
        assert delete_journal.load_journal(tmp_path, "pack-schemapin") == {
            "schema": 1, "axes": {},
        }

    def test_save_journal_writes_the_tmp_file_with_0o666_permission_bits(self, tmp_path):
        """임시 파일 생성 모드가 0o666(움계수만 걸린다)이어야 다른 소유자 프로세스도
        정리·재읽기를 할 수 있다(mutation testing 실증, #327): 438(0o666) 이
        439(0o667, 실행 비트 하나 추가) 로 바뀌어도 왕복 테스트는 여전히 통과해
        아무도 못 잡았다. 움계수를 0 으로 고정해 마스킹 없이 실제 생성 모드를 본다.
        """
        import stat

        old_umask = os.umask(0)
        try:
            delete_journal.save_journal(
                tmp_path, "pack-perm",
                {"schema": delete_journal.JOURNAL_SCHEMA, "axes": {"vectors": {"done": True}}},
            )
        finally:
            os.umask(old_umask)
        path = delete_journal.journal_path(tmp_path, "pack-perm")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o666, oct(mode)

    def test_save_journal_preserves_non_ascii_content_unescaped(self, tmp_path):
        """`ensure_ascii=False` 라 한글 등 비-ASCII 값이 `\\uXXXX` 이스케이프 없이
        원문 그대로 저장돼야 사람이 저널 파일을 직접 열어 읽을 수 있다(mutation
        testing 실증, #327): `ensure_ascii=False` 가 `True` 로 바뀌거나, `ensure_ascii`
        와 `indent` 키워드 이름이 서로 바뀌어(그 결과 `ensure_ascii` 자리에 정수 `2`
        가 들어가 참으로 평가된다) 실질적으로 이스케이프가 켜져도, 내용만 왕복
        확인하는(파싱된 값만 비교하는) 테스트는 이스케이프 여부를 안 봐서 못 잡았다.
        """
        delete_journal.save_journal(
            tmp_path, "pack-비ascii",
            {"schema": delete_journal.JOURNAL_SCHEMA, "axes": {"note": "한글 팩 이름"}},
        )
        path = delete_journal.journal_path(tmp_path, "pack-비ascii")
        raw = path.read_text(encoding="utf-8")
        assert "한글 팩 이름" in raw, raw
        assert "\\u" not in raw, raw


# ---------------------------------------------------------------------------
# 2. 기본 재실행 = 탐지와 보고(무쓰기). 완주는 resume=True 가 있어야만.
# ---------------------------------------------------------------------------

class TestDefaultResumeRequiresExplicitConfirmation:
    def test_existing_journal_without_resume_flag_reports_status_live_counts_and_window_warning_then_writes_nothing(
        self, live, tmp_path
    ):
        """[리드 재정 1/2/4] 저널이 있으면 재실행 기본값은 **탐지와 보고**뿐이다.
        축별 done/pending 상태, 지금 살아 있는 행 수(`pack_live_counts` 재사용),
        그리고 "재개는 저널 생성 이후 새 콘텐츠도 지운다" 는 창 경고까지 메시지에
        담겨야 하고, 이번 호출은 스토어를 전혀 건드리지 않는다.

        역변이: 기본 호출이 저장소 축 실행 루프를 먼저 돌고 나서야 저널 존재를
        검사하면, 예외 자체는 그대로 던져지지만 이미 일부 축이 다시 지워진 뒤라
        아래 "무쓰기" 단언들이 잡는다. 메시지 내용 단언들은 보고 내용이 개별로
        빠지는 회귀(축 상태 누락, 라이브 카운트 누락, 창 경고 누락)를 각각 잡는다.
        """
        graph, docs = live
        node_ids = ["e1", "e2", "e3"]
        _seed_pack(graph, docs, tmp_path, "explicit-pack", node_ids)

        real_delete_node = graph.delete_node

        def _fail_for_e2(node_type, node_id):
            if node_id == "e2":
                raise RuntimeError("시뮬레이션된 노드 삭제 실패")
            return real_delete_node(node_type, node_id)

        import unittest.mock as mock
        with mock.patch.object(graph, "delete_node", side_effect=_fail_for_e2):
            pack_load.delete_pack("explicit-pack", graph, docs, _NoVec())  # 1회차: graph 축 부분 실패

        before = _live_node_ids(graph, node_ids)
        assert before == {"e2"}
        live_counts = pack_load.pack_live_counts("explicit-pack", graph, docs, _NoVec())

        doc_calls: list[str] = []
        real_delete_node_doc = docs.delete_node_doc

        def _counted_doc(space, node_id):
            doc_calls.append(node_id)
            return real_delete_node_doc(space, node_id)

        graph_calls: list[str] = []

        def _counted_graph(node_type, node_id):
            graph_calls.append(node_id)
            return real_delete_node(node_type, node_id)

        with mock.patch.object(docs, "delete_node_doc", side_effect=_counted_doc), \
             mock.patch.object(graph, "delete_node", side_effect=_counted_graph):
            with pytest.raises(delete_journal.DeletePackJournalPending) as exc_info:
                pack_load.delete_pack("explicit-pack", graph, docs, _NoVec())

        assert doc_calls == [] and graph_calls == [], (
            "resume 플래그 없이 호출했는데 스토어 축이 실행됐다 — 기본은 무쓰기 보고여야 한다"
        )
        after = _live_node_ids(graph, node_ids)
        assert after == before, (
            f"기본(무플래그) 재실행인데 저장소 상태가 바뀌었다: before={before} after={after}"
        )

        msg = str(exc_info.value)
        assert "graph_nodes: 대기" in msg, f"미완료 축 표시가 메시지에 없다: {msg!r}"
        assert "doc_node_extra_and_sources: 완료" in msg, f"완료 축 표시가 메시지에 없다: {msg!r}"
        for key, val in live_counts.items():
            shown = val if val is not None else "미확인"
            assert f"{key}={shown}" in msg, f"라이브 카운트 {key}={shown} 가 메시지에 없다: {msg!r}"
        assert "새 콘텐츠가 추가" in msg, f"창 경고가 메시지에 없다: {msg!r}"
        assert "resume=True" in msg, f"완주 방법(resume=True) 안내가 메시지에 없다: {msg!r}"

    def test_resume_flag_with_vanished_registry_row_still_refuses_and_writes_nothing(
        self, live, tmp_path
    ):
        """[리드 재정 5] 저널 생성 시점엔 레지스트리 행이 있었는데 재개 시점에
        사라졌다 — `resume=True` 를 줘도 확실한 부정 신호이므로 거부한다.

        역변이: `resume=True` 를 "행 존재 검사를 생략해도 된다" 로 구현하면, 이
        테스트의 `DeletePackJournalConflict` 단언이 잡는다(스토어 무변형까지
        같이 확인한다).
        """
        graph, docs = live
        node_ids = ["v1", "v2"]
        _seed_pack(graph, docs, tmp_path, "vanish-pack", node_ids)

        pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
        ensure_test_user(pack_sql, _OWNER)
        create_pack(pack_sql, _OWNER, "vanish-pack")
        assert get_pack(pack_sql, "vanish-pack") is not None

        import unittest.mock as mock
        real_delete_node = graph.delete_node

        def _fail_for_v2(node_type, node_id):
            if node_id == "v2":
                raise RuntimeError("시뮬레이션된 노드 삭제 실패")
            return real_delete_node(node_type, node_id)

        with mock.patch.object(graph, "delete_node", side_effect=_fail_for_v2):
            pack_load.delete_pack("vanish-pack", graph, docs, _NoVec(), sql=pack_sql)  # 저널에 동일성 스냅샷 기록

        # 슬롯 소멸을 흉내낸다 — 오늘 코드에서 유일한 삭제 경로(only_status 없이 강제).
        delete_pack_row(pack_sql, "vanish-pack", _OWNER)
        assert get_pack(pack_sql, "vanish-pack") is None

        before = _live_node_ids(graph, node_ids)
        with pytest.raises(delete_journal.DeletePackJournalConflict):
            pack_load.delete_pack("vanish-pack", graph, docs, _NoVec(), sql=pack_sql, resume=True)
        after = _live_node_ids(graph, node_ids)
        assert after == before, (
            f"레지스트리 행 소멸인데 resume=True 에도 행이 바뀌었다: before={before} after={after}"
        )

    def test_resume_flag_with_differing_created_at_still_refuses_and_writes_nothing(
        self, live, tmp_path
    ):
        """[리드 재정 5] `created_at` 이 다르면 확실히 다른 팩이다 — 5라운드
        codex 가 실측한 SQLite `datetime('now')` 초 단위 충돌(같은 초에 옛 행을
        지우고 새 행을 심어도 `created_at` 이 같아질 수 있음)의 반대쪽, "달라진"
        경우를 직접 저널에 주입해 결정적으로 고정한다.

        역변이: 재개가 `created_at` 대조를 건너뛰고 행 존재 여부만 보면, 행은
        여전히 존재하므로(값만 다르다) 이 테스트가 잡는다.
        """
        graph, docs = live
        node_ids = ["m1"]
        _seed_pack(graph, docs, tmp_path, "mismatch-pack", node_ids)

        pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
        ensure_test_user(pack_sql, _OWNER)
        create_pack(pack_sql, _OWNER, "mismatch-pack")
        assert get_pack(pack_sql, "mismatch-pack") is not None

        delete_journal.save_journal(tmp_path, "mismatch-pack", {
            "schema": delete_journal.JOURNAL_SCHEMA,
            "pack_identity": {"created_at": "1999-01-01 00:00:00"},  # 지금 행과 절대 안 같다
            "axes": {"graph_nodes": {"done": False, "count": 0}},
        })

        before = _live_node_ids(graph, node_ids)
        with pytest.raises(delete_journal.DeletePackJournalConflict):
            pack_load.delete_pack("mismatch-pack", graph, docs, _NoVec(), sql=pack_sql, resume=True)
        after = _live_node_ids(graph, node_ids)
        assert after == before, (
            f"created_at 불일치인데 resume=True 에도 행이 바뀌었다: before={before} after={after}"
        )

    def test_resume_flag_with_matching_identity_completes(self, live, tmp_path):
        """일치는 증명은 아니지만(SQLite 초 단위 충돌 가능) 통과 조건으로는 쓴다
        (리드 재정 5 — "불일치만 확실한 부정 신호"). 일치하면 `resume=True` 가
        완주한다.

        역변이: 일치하는데도 항상 거부하면(과잉 방어), 이 테스트가 실패한다 —
        운영자가 명시 확인했는데도 완주가 불가능해지는 회귀다.
        """
        graph, docs = live
        node_ids = ["k1"]
        _seed_pack(graph, docs, tmp_path, "match-pack", node_ids)

        pack_sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
        ensure_test_user(pack_sql, _OWNER)
        create_pack(pack_sql, _OWNER, "match-pack")
        real_row = get_pack(pack_sql, "match-pack")
        assert real_row is not None

        delete_journal.save_journal(tmp_path, "match-pack", {
            "schema": delete_journal.JOURNAL_SCHEMA,
            "pack_identity": {"created_at": real_row["created_at"]},
            "axes": {
                "node_twin_loop": {"done": True, "clean": True},
                "doc_node_extra_and_sources": {"done": True, "count": 0},
                "graph_nodes": {"done": False, "count": 0},
                "vectors": {"done": True, "clean": True, "count": 0},
            },
        })

        pack_load.delete_pack("match-pack", graph, docs, _NoVec(), sql=pack_sql, resume=True)
        assert _live_node_ids(graph, node_ids) == set()

    def test_resume_flag_without_sql_skips_identity_check_and_completes(self, live, tmp_path):
        """저널 생성 시점에 `sql` 이 안 주어졌으면 비교 근거 자체가 없다 — 없는
        근거로 거부하지 않는다. `resume=True` 만으로 `done=False` 축을 완주한다.

        역변이: `sql` 부재를 "동일성 불확실 = 항상 거부" 로 구현하면, `sql` 을
        아예 안 쓰는 기존 다수 호출부(§ 하위호환 클래스 참고)의 재개 경로가
        전부 막힌다 — 이 테스트가 그 과잉 차단을 잡는다.
        """
        graph, docs = live
        node_ids = ["n1", "n2"]
        _seed_pack(graph, docs, tmp_path, "nosql-pack", node_ids)

        import unittest.mock as mock
        real_delete_node = graph.delete_node

        def _fail_for_n2(node_type, node_id):
            if node_id == "n2":
                raise RuntimeError("시뮬레이션된 노드 삭제 실패")
            return real_delete_node(node_type, node_id)

        with mock.patch.object(graph, "delete_node", side_effect=_fail_for_n2):
            pack_load.delete_pack("nosql-pack", graph, docs, _NoVec())  # sql 없이 1회차

        pack_load.delete_pack("nosql-pack", graph, docs, _NoVec(), resume=True)
        assert _live_node_ids(graph, node_ids) == set()


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

        # 재실행 — 명시 플래그로 남은 p2까지 마저 지운다(기본 호출은 이제 무쓰기 보고다).
        pack_load.delete_pack("partial-pack", graph, docs, _NoVec(), resume=True)
        journal2 = delete_journal.load_journal(tmp_path, "partial-pack")
        assert journal2["axes"]["graph_nodes"]["done"] is True
        assert _live_node_ids(graph, node_ids) == set(), "재실행 뒤에도 p2가 남아 있다"


# ---------------------------------------------------------------------------
# 4. 벡터 축 세 갈래 — 구조적 미지원 vs 연결·초기화 실패 vs 이번 조회 실패.
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

        # 복구된 뒤(malformed 없이) resume=True로 재실행하면 실제로 지워지고 done=True로 수렴한다.
        vec2 = _FakeChromaVec({"a1": "vecpack", "a2": "vecpack"})
        pack_load.delete_pack("vecpack", graph, docs, vec2, resume=True)
        journal2 = delete_journal.load_journal(tmp_path, "vecpack")
        assert journal2["axes"]["vectors"]["done"] is True
        assert vec2._collection.delete_calls, "복구 후 재실행에서 실제 삭제가 안 일어났다"

    def test_structurally_unsupported_backend_is_done_immediately(self, live, tmp_path):
        """대조군 — `_NoVec` 은 백엔드 모양 자체가 없다(구조적 미지원). 대기할
        것이 없으므로 즉시 done=True(#165 기존 계약과 동형).

        역변이: 미지원 백엔드도 vectors 축을 영원히 pending 으로 남기면(아래
        테스트가 요구한 "미확인은 done 아님" 을 과잉 적용하면) 이 테스트가 잡는다
        — 재개할 것이 없는 축까지 재실행 대상으로 취급하는 것도 결함이다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "novec-pack", ["z1"])
        pack_load.delete_pack("novec-pack", graph, docs, _NoVec())
        journal = delete_journal.load_journal(tmp_path, "novec-pack")
        assert journal["axes"]["vectors"] == {"done": True, "clean": True, "count": 0}

    def test_connection_failure_shaped_backend_stays_not_done(self, live, tmp_path):
        """[로컬 지적 4] `_collection` 을 가진 chroma 모양 객체지만
        `available=False` 인 경우는 "구조적 미지원" 이 아니라 "연결·초기화 실패"
        다 — 재시도하면 나을 수 있으므로 즉시 done 을 확정하면 안 된다. `_NoVec`
        (백엔드 모양 자체가 없음)과 이 경우를 코드가 구분해야 한다.

        역변이: `available` 하나만 보고 축을 즉시 done=True 로 확정하면(백엔드
        모양을 보지 않으면), 이 테스트가 잡는다 — chroma 모양이 있는데도
        미지원과 똑같이 취급해 재시도 기회를 영구히 없앤 것이다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecfail-pack", ["w1"])
        vec = _ChromaShapedButUnavailable()
        pack_load.delete_pack("vecfail-pack", graph, docs, vec)
        journal = delete_journal.load_journal(tmp_path, "vecfail-pack")
        assert journal["axes"]["vectors"]["done"] is False, (
            f"연결 실패(모양은 chroma)인데 done=True로 잘못 확정됐다: {journal['axes']['vectors']!r}"
        )

        # 연결이 복구된 뒤 resume=True로 재실행하면 실제로 확인되고 done=True로 수렴한다.
        vec2 = _FakeChromaVec({"w1": "vecfail-pack"})
        pack_load.delete_pack("vecfail-pack", graph, docs, vec2, resume=True)
        journal2 = delete_journal.load_journal(tmp_path, "vecfail-pack")
        assert journal2["axes"]["vectors"]["done"] is True


# ---------------------------------------------------------------------------
# 4-2. PR #360 리뷰 지적 1+2 — done 저널의 사후 드리프트를 무조건 신뢰하지 않는다.
# ---------------------------------------------------------------------------

class _SqlalchemyShapedButEngineNone:
    """`_engine` 속성은 있으나 값이 `None`(연결 실패)인 sqlalchemy 모양 벡터
    스토어 — PR #360 리뷰 지적 2의 정확한 재현(`PgVectorStore(_engine=None)`)이다.
    `_vec_shape` 가 값 기준(`getattr(...) is not None`)으로 모양을 판정하면 이
    객체는 "모양 없음"(구조적 미지원, `_NoVec` 과 동류)으로 오분류돼 vectors
    축이 즉시 done=True 로 확정된다 — hasattr 기준으로 바뀌어야 "모양은 있는데
    지금 비어 있다"(연결 실패)로 옳게 분류된다."""

    available = False
    _engine = None

    def delete(self, ids):  # pragma: no cover -- available=False라 호출 안 됨
        pass


class _SqlShapedButConnNone:
    """`_conn` 속성은 있으나 값이 `None`(연결 실패)인 sql 모양 벡터스토어 —
    `_vec_shape`가 sqlalchemy(`_engine`) 분기와 별개로 판정하는 sql(`_conn`/
    `conn`) 분기의 역변이 검출력을 고정한다. `_SqlalchemyShapedButEngineNone`
    은 `_engine` 분기만 지키므로 `_conn`/`conn` hasattr 재작성이 홀로 원복돼도
    잡히지 않는 잔여 사각을 닫는다."""

    available = False
    _conn = None

    def delete(self, ids):  # pragma: no cover -- available=False라 호출 안 됨
        pass


class TestDoneJournalDriftRecheck:
    def test_shaped_but_engine_none_backend_stays_not_done(self, live, tmp_path):
        """[리뷰 지적 2, `_vec_shape` 값 기준 → hasattr 기준 재작성] `_engine`
        속성이 존재하지만 값이 `None`(연결 실패)인 sqlalchemy 모양 벡터스토어는
        "구조적 미지원"이 아니라 "연결 실패"로 분류돼 vectors 축이 즉시
        done=True 로 확정되면 안 된다.

        역변이: `_vec_shape` 를 값 기준(`getattr(vec, "_engine", None) is not
        None`)으로 되돌리면 이 테스트가 잡는다 — `_engine=None` 이 구조적
        미지원으로 오분류돼 vectors 축이 즉시 done=True 로 확정된다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecenginenone-pack", ["h1"])
        vec = _SqlalchemyShapedButEngineNone()
        pack_load.delete_pack("vecenginenone-pack", graph, docs, vec)
        journal = delete_journal.load_journal(tmp_path, "vecenginenone-pack")
        assert journal["axes"]["vectors"]["done"] is False, (
            "_engine=None(연결 실패, sqlalchemy 모양)인데 done=True로 잘못 "
            f"확정됐다: {journal['axes']['vectors']!r}"
        )

    def test_shaped_but_conn_none_backend_stays_not_done(self, live, tmp_path):
        """[리뷰 지적 2 잔여 사각 보강] `_conn` 속성이 존재하지만 값이 `None`
        (연결 실패)인 sql 모양 벡터스토어도, sqlalchemy 모양과 마찬가지로
        "구조적 미지원"이 아니라 "연결 실패"로 분류돼 vectors 축이 즉시
        done=True 로 확정되면 안 된다.

        역변이: `_vec_shape` 의 `_conn`/`conn` hasattr 분기만 값 기준
        (`getattr(vec, "_conn", None) or getattr(vec, "conn", None)` 을 진위
        판정에 씀)으로 되돌리면 이 테스트가 잡는다 — `_engine=None` 케이스를
        지키는 `test_shaped_but_engine_none_backend_stays_not_done` 은 sql
        분기가 홀로 원복돼도 잡지 못한다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecconnnone-pack", ["i1"])
        vec = _SqlShapedButConnNone()
        pack_load.delete_pack("vecconnnone-pack", graph, docs, vec)
        journal = delete_journal.load_journal(tmp_path, "vecconnnone-pack")
        assert journal["axes"]["vectors"]["done"] is False, (
            "_conn=None(연결 실패, sql 모양)인데 done=True로 잘못 "
            f"확정됐다: {journal['axes']['vectors']!r}"
        )

    def test_orphan_doc_nodes_added_after_completion_are_swept_on_resume(
        self, live, tmp_path
    ):
        """[리뷰 지적 1] `doc_node_extra_and_sources` 축이 이미 done=True 로
        완료된 뒤, 같은 팩 이름으로 태그된 고아 doc_nodes 행이 **새로** 생기면
        (완료 이후 유입 — 백필, 재수집 등) `resume=True` 재실행이 그 행을 실제로
        지워야 한다. `done` 플래그를 영구 신뢰하면 이 새 행은 영원히 스킵된다.

        역변이: `live_docs`/`doc_nodes_live` 드리프트 신호를 게이팅에서 빼면(즉
        `if not _axis_done("doc_node_extra_and_sources"):` 로만 판정하면) 이
        테스트가 잡는다 — done=True 저널을 그대로 신뢰해 새 고아 행을 지우지 않는다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "docdrift-pack", ["e1"])
        n1, *_ = pack_load.delete_pack("docdrift-pack", graph, docs, _NoVec())
        assert n1 == 1
        journal = delete_journal.load_journal(tmp_path, "docdrift-pack")
        assert journal["axes"]["doc_node_extra_and_sources"]["done"] is True

        # 완료 이후 유입을 흉내낸다 — 같은 pack_id 로 태그된 고아 doc_nodes 행을
        # 그래프 트윈 없이 직접 심는다(`test_pack_load.py` 의 "보강 경로" 검사와
        # 동일한 방식 — 스키마를 읽어 NOT NULL 컬럼을 채운다).
        cols = {r[1]: r for r in docs._conn.execute("PRAGMA table_info(doc_nodes)")}
        vals = {"space": "resource", "node_id": "orphan-drift-1",
                "properties": json.dumps({"pack_id": "docdrift-pack"})}
        for name, info in cols.items():
            if name in vals or info[4] is not None:      # 이미 채웠거나 기본값 있음
                continue
            if info[3]:                                   # NOT NULL
                vals[name] = "1970-01-01T00:00:00Z" if "at" in name else ""
        docs._conn.execute(
            f"INSERT INTO doc_nodes ({','.join(vals)}) "
            f"VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
        docs._conn.commit()

        n2, *_ = pack_load.delete_pack(
            "docdrift-pack", graph, docs, _NoVec(), resume=True)
        assert n2 == 1, (
            f"완료 이후 유입된 고아 doc_nodes 행이 재개에서 안 지워졌다 (실제 {n2})"
        )
        journal2 = delete_journal.load_journal(tmp_path, "docdrift-pack")
        assert journal2["axes"]["doc_node_extra_and_sources"]["done"] is True

    def test_doc_sources_only_drift_without_doc_nodes_is_swept_on_resume(
        self, live, tmp_path
    ):
        """[리뷰 지적 1 잔여 사각 보강] `live_docs`(doc_sources 드리프트)만
        단독으로도 축을 재시도시켜야 한다 — 위 고아 doc_nodes 테스트는
        `doc_nodes_live` 만 채우므로, 게이트의 `live_docs` 항이 단독으로는
        아무것도 못 잡아도 그 테스트는 여전히 통과한다(두 신호가 OR 로 묶여
        있어 하나만 있어도 게이트가 참이 되기 때문). 이 테스트는 doc_nodes
        드리프트 없이 doc_sources(청크) 드리프트만 만들어 `live_docs` 항
        자신의 검출력을 고정한다.

        역변이: 게이트에서 `live_docs` 항만 빼면(`doc_nodes_live` 는 남기고)
        이 테스트가 잡는다 — 새로 유입된 doc_sources 행이 재개에서 안 지워진다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "docsrcdrift-pack", ["j1"])
        n1, *_ = pack_load.delete_pack("docsrcdrift-pack", graph, docs, _NoVec())
        assert n1 == 1
        journal = delete_journal.load_journal(tmp_path, "docsrcdrift-pack")
        assert journal["axes"]["doc_node_extra_and_sources"]["done"] is True

        # 완료 이후 유입을 흉내낸다 — doc_nodes 는 건드리지 않고 doc_sources
        # (청크) 행만 같은 pack_id 로 직접 upsert 한다.
        docs.upsert_source(
            "orphan-chunk-1", "post-completion chunk",
            {"pack_id": "docsrcdrift-pack"},
        )

        n2, *_ = pack_load.delete_pack(
            "docsrcdrift-pack", graph, docs, _NoVec(), resume=True)
        assert docs.get_source("orphan-chunk-1") is None, (
            "완료 이후 유입된 doc_sources 행이 재개에서 안 지워졌다"
        )
        journal2 = delete_journal.load_journal(tmp_path, "docsrcdrift-pack")
        assert journal2["axes"]["doc_node_extra_and_sources"]["done"] is True

    def test_done_vectors_axis_rechecked_when_backend_becomes_unavailable(
        self, live, tmp_path
    ):
        """[리뷰 지적 1+2] vectors 축이 `available=True` 백엔드로 이미
        done=True 완료된 뒤, 같은 팩을 (모양은 chroma 지만) `available=False`
        백엔드로 재개하면 done=True 저널을 그대로 스킵하지 않고 축을 다시
        시도해 `done=False` 로 되돌려야 한다 — "확인 불가" 상태를 "확인 완료"
        로 영원히 보고하면 안 된다.

        역변이: `vectors_drift` 게이팅을 되돌리면(`if not _axis_done("vectors"):`
        로만 판정하면) 이 테스트가 잡는다 — done=True 저널을 그대로 스킵한다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecdrift-pack", ["f1"])
        vec1 = _FakeChromaVec({"f1": "vecdrift-pack"})
        pack_load.delete_pack("vecdrift-pack", graph, docs, vec1)
        journal = delete_journal.load_journal(tmp_path, "vecdrift-pack")
        assert journal["axes"]["vectors"]["done"] is True

        vec2 = _ChromaShapedButUnavailable()
        pack_load.delete_pack("vecdrift-pack", graph, docs, vec2, resume=True)
        journal2 = delete_journal.load_journal(tmp_path, "vecdrift-pack")
        assert journal2["axes"]["vectors"]["done"] is False, (
            "백엔드가 unavailable 로 바뀌었는데 done=True 저널을 그대로 스킵했다: "
            f"{journal2['axes']['vectors']!r}"
        )

    def test_available_vectors_axis_skip_still_does_not_requery_backend(
        self, live, tmp_path
    ):
        """[v3 설계 트레이드오프 확인] `available=True` 인 채로 완료된 vectors
        축은 사후 드리프트가 있어도 재조회하지 않는다 — 기존 계약
        (`TestResumeSkipDoesNotReuseCountInReturnValue`)이 잠근 "이미 완료·
        available 한 벡터스토어는 매 재개마다 재조회하지 않는다" 를 이번에
        추가한 드리프트 신호가 깨지 않았음을 확인하는 회귀 대조군이다.

        역변이: `vectors_drift` 가 `available=True` 를 무시하고 무조건 쿼리를
        내게 고치면 이 테스트가 잡는다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecnodrift-pack", ["g1"])
        vec1 = _FakeChromaVec({"g1": "vecnodrift-pack"})
        pack_load.delete_pack("vecnodrift-pack", graph, docs, vec1)

        vec2 = _FakeChromaVec({"g1": "vecnodrift-pack"})  # available=True, 내용도 있음
        pack_load.delete_pack("vecnodrift-pack", graph, docs, vec2, resume=True)
        assert not vec2._collection.get_where_calls, (
            "이미 done 이고 available 인 vectors 축인데 재개 호출이 벡터스토어를 재조회했다"
        )
        journal = delete_journal.load_journal(tmp_path, "vecnodrift-pack")
        assert journal["axes"]["vectors"]["done"] is True

    def test_available_is_read_exactly_once_per_delete_pack_call(
        self, live, tmp_path
    ):
        """[리뷰 P2] `delete_pack` 은 shaped 벡터스토어의 `available` 을 호출당
        정확히 한 번만 읽어야 한다. 위 드리프트 탐침이 한 번 읽고
        `_delete_pack_vectors` 가 내부에서 다시 읽으면, 상태를 가진 property 의
        두 번째 접근이 던지는 예외가 vectors 축이 저널에 기록되기 전에
        `delete_pack` 밖으로 새 나간다 — 앞 세 축(node_twin_loop/
        doc_node_extra_and_sources/graph_nodes)은 이미 커밋됐어도 vectors 축
        자체는 시도됐다는 기록조차 없이 유실된다.

        역변이: `_delete_pack_vectors` 가 caller 가 넘긴 값 대신
        `vec.available` 을 다시 읽거나, 드리프트 탐침이 값을 캐시하지 않고
        매번 `getattr` 하면 이 테스트가 잡는다 — 두 번째 읽기에서 예외가 나
        vectors 축이 커밋 전에 유실된다.
        """
        graph, docs = live
        _seed_pack(graph, docs, tmp_path, "vecdoubleread-pack", ["k1"])
        vec = _AvailableRaisesOnSecondRead()
        pack_load.delete_pack("vecdoubleread-pack", graph, docs, vec)
        assert vec._reads == 1, (
            f"available 을 {vec._reads}번 읽었다 — 호출당 정확히 1번이어야 한다"
        )
        journal = delete_journal.load_journal(tmp_path, "vecdoubleread-pack")
        assert journal["axes"]["vectors"]["done"] is True, (
            "available 이중 읽기로 vectors 축이 저널에 기록되지 못했다: "
            f"{journal['axes']['vectors']!r}"
        )


# ---------------------------------------------------------------------------
# 5. 로컬 지적 7 — doc 축(node_twin_loop) sticky 실패 플래그 + doc→graph 게이팅.
# ---------------------------------------------------------------------------

class TestDocAxisStickyFailureFlag:
    def test_doc_delete_failure_leaves_node_twin_loop_not_done_and_gates_graph_axis(
        self, live, tmp_path
    ):
        """[로컬 지적 7] `node_twin_loop` 의 개별 `docs.delete_node_doc()` 실패는
        기존처럼 삼키고 계속 진행하되(관용 계약 불변), 하나라도 삼켰으면 sticky
        플래그로 `done=False` 를 고정해야 한다 — "루프가 예외 없이 끝났다" 만으로
        `done` 을 정하면 개별 예외는 항상 삼켜지므로 그 조건이 거의 항상 참이 돼
        결함을 못 잡는다. 부수로 doc→graph 게이팅(node_twin_loop이 done 이
        아니면 graph 축 진입 안 함, v3/v4 carry-forward)도 같이 고정한다.

        역변이: sticky 플래그 없이 "루프가 예외 없이 끝났다" 만으로 done을
        정하면, 이 테스트의 `done is False` 단언이 잡는다. 게이팅이 빠지면
        "graph 축 무변형" 단언이 잡는다(개별 노드 실패를 무시하고 graph 축까지
        같이 밀어붙이는 회귀).
        """
        graph, docs = live
        node_ids = ["d1", "d2", "d3"]
        _seed_pack(graph, docs, tmp_path, "docfail-pack", node_ids)

        real_delete_node_doc = docs.delete_node_doc

        def _fail_for_d2(space, node_id):
            if node_id == "d2":
                raise RuntimeError("시뮬레이션된 doc 축 실패")
            return real_delete_node_doc(space, node_id)

        import unittest.mock as mock
        with mock.patch.object(docs, "delete_node_doc", side_effect=_fail_for_d2):
            pack_load.delete_pack("docfail-pack", graph, docs, _NoVec())  # 예외 없이 정상 반환(기존 관용 계약)

        journal = delete_journal.load_journal(tmp_path, "docfail-pack")
        axis = journal["axes"]["node_twin_loop"]
        assert axis["done"] is False, (
            f"doc 축에서 개별 예외를 삼켰는데(d2 실패) done=True로 잘못 기록됐다: {axis!r}"
        )
        assert _live_node_ids(graph, node_ids) == set(node_ids), (
            "node_twin_loop이 done이 아닌데 graph 축이 진입해 노드를 지웠다 — doc→graph 게이팅 위반"
        )
        assert journal["axes"]["graph_nodes"].get("done") is not True

        pack_load.delete_pack("docfail-pack", graph, docs, _NoVec(), resume=True)
        journal2 = delete_journal.load_journal(tmp_path, "docfail-pack")
        assert journal2["axes"]["node_twin_loop"]["done"] is True
        assert journal2["axes"]["graph_nodes"]["done"] is True
        assert _live_node_ids(graph, node_ids) == set()


# ---------------------------------------------------------------------------
# 6. 여러 중단 지점 — 첫 축(doc) 직후, 두 번째 축(graph) 직후는 실 SIGKILL.
#    두 지점 모두: 재실행(무플래그) → 보고만 하고 멈춘다 → resume=True → 완주.
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
    def test_sigkill_right_after_doc_axis_commits_reports_then_completes_only_with_resume_flag(
        self, tmp_path, monkeypatch
    ):
        """1번째 훅 위치: doc 축(node_twin_loop + doc_node_extra_and_sources)의
        마지막 커밋 직후·graph 축 진입 이전에 실 SIGKILL. [리드 재정의 핵심 계약]
        재개는 두 단계다 — 플래그 없이 재실행하면 보고만 하고 멈추며(무쓰기),
        `resume=True` 를 줘야만 graph·vectors를 마저 끝낸다.

        역변이: 크래시 훅을 없애 doc 축 커밋과 저널 갱신을 분리하지 않으면(둘을
        하나의 "죽지 않는" 단계로 합치면) 이 크래시 지점 자체가 재현 불가능해져
        테스트가 무의미하게 항상 통과한다 — 그래서 killed(`returncode < 0`)를
        먼저 확인한다. 1단계(무플래그) 단언이 없으면 자동 재개가 되살아나도 이
        테스트가 못 잡는다 — 그래서 무쓰기 확인을 2단계보다 먼저 둔다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        proc = _run_kill_script(tmp_path, f"""
            from opencrab.auth import Principal, principal_scope
            from opencrab.ontology.builder import OntologyBuilder
            from opencrab.pack import load as pack_load
            from opencrab.pack.ownership import get_pack, create_pack
            from opencrab.stores.local_graph_store import LocalGraphStore
            from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
            from opencrab.stores.sql_store import SQLStore

            graph = LocalGraphStore(os.path.join({str(tmp_path)!r}, "graph.db"))
            docs = LocalSQLDocStore(os.path.join({str(tmp_path)!r}, "doc.db"))
            pack_sql = SQLStore("sqlite:///" + os.path.join({str(tmp_path)!r}, "opencrab.db"))
            from sqlalchemy import text as _sa_text
            with pack_sql._engine.begin() as _conn:
                _conn.execute(_sa_text(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES (:uid, :uid, 0) ON CONFLICT (user_id) DO NOTHING"
                ), {{"uid": "crash-user"}})
            if get_pack(pack_sql, "crash-pack") is None:
                create_pack(pack_sql, "crash-user", "crash-pack")
            principal = Principal(user_id="crash-user", is_local=True, disabled=False)
            with principal_scope(principal):
                builder = OntologyBuilder(graph, docs, pack_sql)
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
            assert journal_before["axes"]["node_twin_loop"]["done"] is True
            assert journal_before["axes"]["doc_node_extra_and_sources"]["done"] is True
            assert journal_before["axes"]["graph_nodes"].get("done") is not True

            # 1단계: 플래그 없이 재실행 — 보고만 하고 멈춘다, 무쓰기.
            with pytest.raises(delete_journal.DeletePackJournalPending):
                pack_load.delete_pack("crash-pack", graph, docs, _NoVec())
            journal_still = delete_journal.load_journal(tmp_path, "crash-pack")
            assert journal_still == journal_before, (
                "플래그 없는 재실행인데 저널이 바뀌었다 — 무쓰기 계약 위반"
            )
            assert graph.get_node("Document", "k1") is not None, (
                "graph 축이 아직 안 끝났어야 하는데 플래그 없이 지워졌다"
            )
            assert graph.get_node("Document", "k2") is not None

            # 2단계: 명시 플래그로 완주.
            _n, _c, _v = pack_load.delete_pack("crash-pack", graph, docs, _NoVec(), resume=True)
            journal_after = delete_journal.load_journal(tmp_path, "crash-pack")
            assert journal_after["axes"]["graph_nodes"]["done"] is True
            assert journal_after["axes"]["vectors"]["done"] is True
            assert graph.get_node("Document", "k1") is None
            assert graph.get_node("Document", "k2") is None
        finally:
            graph.close()
            docs.close()

    def test_sigkill_right_after_graph_axis_commits_reports_then_completes_only_with_resume_flag(
        self, tmp_path, monkeypatch
    ):
        """2번째 훅 위치: graph 축 완료 직후. [리드 재정의 핵심 계약] 무플래그
        재실행은 보고만 하고 멈추며(doc 축은 다시 불리지 않는다 — 이미 done),
        `resume=True` 를 줘야만 vectors만 마저 실행한다 — doc/graph를 다시
        실행하지 않는다는 점에서 위 테스트와 검증 축이 다르다(이 테스트는
        "이미 done인 축은 건드리지 않는다"를 두 단계 모두에서 고정한다).

        역변이: 재개가 이미 done인 축까지 무조건 다시 도는(멱등성에 기대 "그냥
        다 재실행"으로 단순화한) 구현이면, 최종 상태로는 못 잡을 수 있다(멱등이라
        결과가 같다) — 그래서 doc 축 실행 카운터를 몽키패치로 세어 "무플래그
        단계·resume 단계 모두에서 doc 축 함수가 안 불렸다"를 직접 확인한다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        proc = _run_kill_script(tmp_path, f"""
            from opencrab.auth import Principal, principal_scope
            from opencrab.ontology.builder import OntologyBuilder
            from opencrab.pack import load as pack_load
            from opencrab.pack.ownership import get_pack, create_pack
            from opencrab.stores.local_graph_store import LocalGraphStore
            from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
            from opencrab.stores.sql_store import SQLStore

            graph = LocalGraphStore(os.path.join({str(tmp_path)!r}, "graph.db"))
            docs = LocalSQLDocStore(os.path.join({str(tmp_path)!r}, "doc.db"))
            pack_sql = SQLStore("sqlite:///" + os.path.join({str(tmp_path)!r}, "opencrab.db"))
            from sqlalchemy import text as _sa_text
            with pack_sql._engine.begin() as _conn:
                _conn.execute(_sa_text(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES (:uid, :uid, 0) ON CONFLICT (user_id) DO NOTHING"
                ), {{"uid": "crash-user"}})
            if get_pack(pack_sql, "crash-pack-2") is None:
                create_pack(pack_sql, "crash-user", "crash-pack-2")
            principal = Principal(user_id="crash-user", is_local=True, disabled=False)
            with principal_scope(principal):
                builder = OntologyBuilder(graph, docs, pack_sql)
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
            doc_calls: list[str] = []
            real_delete_node_doc = docs.delete_node_doc

            def _counted(space, node_id):
                doc_calls.append(node_id)
                return real_delete_node_doc(space, node_id)

            # 1단계: 플래그 없이 재실행 — 보고만 하고 멈춘다, doc 축 재실행 없음.
            with mock.patch.object(docs, "delete_node_doc", side_effect=_counted):
                with pytest.raises(delete_journal.DeletePackJournalPending):
                    pack_load.delete_pack("crash-pack-2", graph, docs, _NoVec())
            assert doc_calls == [], f"플래그 없는 재실행인데 doc 축이 실행됐다: {doc_calls}"

            # 2단계: 명시 플래그로 완주 — 그래도 doc 축은 이미 done이라 재실행 안 됨.
            with mock.patch.object(docs, "delete_node_doc", side_effect=_counted):
                pack_load.delete_pack("crash-pack-2", graph, docs, _NoVec(), resume=True)

            assert doc_calls == [], (
                f"doc 축이 이미 done인데도 resume=True 재개가 다시 실행했다: {doc_calls}"
            )
            journal = delete_journal.load_journal(tmp_path, "crash-pack-2")
            assert journal["axes"]["vectors"]["done"] is True
        finally:
            graph.close()
            docs.close()


# ---------------------------------------------------------------------------
# 7. 요약 표시 — 재개 실행의 확인 건수와 "이전 부분 실행 불명" 마킹.
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

        pack_load.delete_pack("summary-pack", graph, docs, _NoVec(), resume=True)
        out = capsys.readouterr().out

        assert "재개" in out, f"재개 표시가 요약에 없다: {out!r}"
        assert "알 수 없" in out, f"이전 중단분 불명 표시가 요약에 없다: {out!r}"
        # vectors 축은 1회차에서 이미 done(구조적 미지원, `_NoVec`)이라 2회차(재개)는
        # 축 스킵 분기를 탄다 — 저널이 kind/vec_available 을 저장하지 않으므로 원래
        # 갈래를 추측하지 않고 중립 라벨("이전 완료")을 낸다(#327 로컬 지적).
        assert "이전 완료" in out, f"스킵된 vectors 축 표시가 중립 라벨이 아니다: {out!r}"


# ---------------------------------------------------------------------------
# 7-보충. 스킵된 축(graph_nodes/vectors)이 이전 실행의 count 를 이번 호출
#         반환값에 재합산하지 않는다 — "재개 실행은 이번 실행 확인 건수만
#         낸다"(§1) 를 반환값 자체로 고정한다(요약 문자열이 아니라).
# ---------------------------------------------------------------------------

class TestResumeSkipDoesNotReuseCountInReturnValue:
    def test_graph_nodes_skip_branch_does_not_add_prior_count_to_node_del(
        self, live, tmp_path
    ):
        """1회차가 노드 3개를 정상 완료시키면 `graph_nodes` 축에 `done=True,
        count=3`이 남는다. `delete_pack`은 완료 저널을 스스로 지우지 않으므로
        2회차 `resume=True`는 이미 done인 `graph_nodes`를 스킵 분기(`elif
        _axis_done("graph_nodes")`)로 탄다. 그 분기는 이번 실행에서 아무
        노드도 지우지 않았으므로 반환값의 `node_del`은 0이어야 한다.

        역변이: `elif _axis_done("graph_nodes"): node_del +=
        axes["graph_nodes"].get("count", 0)` 를 되살리면 `node_del`이 3이
        되어 이 단언이 잡는다.
        """
        graph, docs = live
        node_ids = ["g1", "g2", "g3"]
        _seed_pack(graph, docs, tmp_path, "graphskip-pack", node_ids)

        node_del, _chunk_sql_del, _chunk_vec_del = pack_load.delete_pack(
            "graphskip-pack", graph, docs, _NoVec()
        )
        assert node_del == 3
        journal = delete_journal.load_journal(tmp_path, "graphskip-pack")
        assert journal["axes"]["graph_nodes"] == {"done": True, "count": 3}

        node_del2, _chunk_sql_del2, _chunk_vec_del2 = pack_load.delete_pack(
            "graphskip-pack", graph, docs, _NoVec(), resume=True
        )
        assert node_del2 == 0, (
            f"과거 graph_nodes count(3)가 재개 반환값에 섞였다: node_del={node_del2!r}"
        )

    def test_vectors_skip_branch_does_not_reuse_prior_count_or_backend_label(
        self, live, tmp_path, capsys
    ):
        """1회차가 chroma 모양 벡터스토어로 2건을 확인·삭제해 `vectors` 축에
        `done=True, count=2`가 남는다. 2회차 `resume=True`는 이미 done인
        `vectors`를 스킵 분기(`else: chunk_vec_del = ...`)로 탄다. 그 분기는
        벡터스토어에 어떤 파괴적 호출도 하지 않으므로 반환값은 `0`이어야
        하고(§ docstring `int | None` 계약: `0`="이번 호출은 시도 안 함"),
        표시는 과거 백엔드를 추측하지 않는 중립 라벨("이전 완료")이어야 한다.

        2회차에 넘기는 `_FakeChromaVec({})`은 실제로 호출되지 않아야 한다 —
        축이 이미 done이라 `_delete_pack_vectors` 자체를 안 부른다.

        역변이: `chunk_vec_del = axes["vectors"].get("count")`를 되살리면 첫
        단언이, `vec_skipped` 분기를 제거해 `vec_available=True` 기본값으로
        되돌리면 마지막 단언("미지원" not in out)이 잡는다.
        """
        graph, docs = live
        vec = _FakeChromaVec({"v1": "vectorskip-pack", "v2": "vectorskip-pack"})

        pack_load.delete_pack("vectorskip-pack", graph, docs, vec)
        capsys.readouterr()  # 1회차 출력은 버린다 — 2회차(재개) 출력만 본다
        journal = delete_journal.load_journal(tmp_path, "vectorskip-pack")
        assert journal["axes"]["vectors"] == {"done": True, "clean": True, "count": 2}

        vec2 = _FakeChromaVec({})
        _node_del, _chunk_sql_del, chunk_vec_del = pack_load.delete_pack(
            "vectorskip-pack", graph, docs, vec2, resume=True
        )
        out = capsys.readouterr().out

        assert not vec2._collection.get_where_calls, (
            "이미 done인 vectors 축인데 재개 호출이 벡터스토어를 다시 조회했다"
        )
        assert chunk_vec_del == 0, (
            f"과거 vectors count(2)가 재개 반환값에 섞였다: chunk_vec_del={chunk_vec_del!r}"
        )
        assert "이전 완료" in out, f"스킵된 vectors 축 표시가 중립 라벨이 아니다: {out!r}"
        assert "미지원" not in out, f"가용했던 백엔드가 미지원으로 오표시됐다: {out!r}"


# ---------------------------------------------------------------------------
# 8. 락 경합 — holder가 쥔 채 contender의 타임아웃/비차단 실패를 직접 관측.
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

        holder 는 반드시 **다른 스레드**에서 쥐어야 한다 — `locking.file_lock` 이
        내부적으로 `threading.RLock` 을 쓰므로(같은 락 이름에 재진입 허용, 위
        `test_two_threads_contend...` 참고) 같은 스레드에서 다시 잡으면 즉시
        통과해 버려 대기 자체가 재현되지 않는다.

        역변이: `delete_pack`이 아예 락을 안 잡으면, holder가 쥔 동안 호출한
        `delete_pack`이 타임아웃 없이 그냥 통과해 버린다 — 이 테스트가 그것을 잡는다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        graph, docs = live
        from opencrab import locking

        filename = delete_journal.lock_filename("busy-pack")
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def _hold():
            with locking.file_lock(filename, str(tmp_path)):
                holder_ready.set()
                release_holder.wait(timeout=10)

        t = threading.Thread(target=_hold)
        t.start()
        try:
            assert holder_ready.wait(timeout=5), "holder가 락을 못 잡았다"
            with pytest.raises(TimeoutError):
                pack_load.delete_pack("busy-pack", graph, docs, _NoVec(), lock_timeout=0.2)
        finally:
            release_holder.set()
            t.join(timeout=10)

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
