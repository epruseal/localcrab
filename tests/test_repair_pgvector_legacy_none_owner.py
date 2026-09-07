"""scripts/repair_pgvector_legacy_none_owner.py (#306, #197의 데이터 후속).

#197/PR#303 이전 pgvector 쓰기 경로(``str(meta.get("pack_id", ""))``)는 메타 키가
있고 값이 파이썬 ``None``일 때 리터럴 문자열 ``"None"``을 ``pack_id`` 컬럼에 그대로
적었다. 그 코드는 이미 고쳐졌지만(``test_vector_slot_ownership.py::
TestNonePackIdIsStoredAsUnowned`` 참고), **그 버그가 과거에 써놓은 행**은 여전히
남아 있어 정당한 소유자의 재적재를 막는다. 이 모듈은 그 잔존 데이터를 복구하는
스크립트를 검증한다. 현재 스토어/게이트 코드 자체에는 결함이 없다.

``pg`` 백엔드 전용이다(sqlite-vec/chroma는 이 오염 형태가 구조적으로 없다, 설계
§2.1). ``tests/_vec_helpers.py::build_vector_store("pg", ...)``를 재사용해 실 PG
연결이 없으면(``OPENCRAB_PG_TEST_URL`` 미설정) 깔끔히 skip한다.

레거시 오염 행은 ``upsert_texts``로 정상 행을 만든 뒤 원시 SQL로 ``pack_id``/
``metadata``를 덮어써 재현한다(``TestNullPackIdIsUnowned``/
``TestNonePackIdIsStoredAsUnowned``과 같은 기법) -- 현재(수정된) ``upsert_texts``는
이 형태를 다시는 만들지 않기 때문이다.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from _vec_helpers import build_vector_store
from sqlalchemy import text

# scripts/ is not a package (tests/test_migrate_pack_ownership.py's identical
# pattern) -- import it directly off sys.path instead.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import repair_pgvector_legacy_none_owner as repair  # noqa: E402

# ---------------------------------------------------------------------------
# 픽스처 / 헬퍼
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_store(tmp_path):
    s = build_vector_store("pg", tmp_path)
    assert s.available
    yield s
    try:
        with s._engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {s._table}"))
    except Exception:
        pass
    if hasattr(s, "close"):
        s.close()


def _seed_and_contaminate(store, node_id, *, document="레거시 원본", genuine_none_pack=False):
    """레거시 버그가 만들었던 정확한 오염 형태를 원시 SQL로 재현한다.

    ``genuine_none_pack=False``(기본): 진짜 오염 -- ``pack_id`` 컬럼은 리터럴
    ``'None'``, ``metadata``의 ``pack_id`` 키 값은 JSON null. 이것이 복구 대상이다.

    ``genuine_none_pack=True``: 대조군 -- ``pack_id`` 컬럼은 똑같이 ``'None'``
    이지만 ``metadata``의 값은 JSON 문자열 ``"None"``(진짜 "None"이라는 이름의
    팩). containment 조건(``metadata @> '{"pack_id": null}'``)에 걸리지 않아야
    하므로 탐지/복구 어느 쪽에서도 손대면 안 된다.
    """
    store.upsert_texts(texts=[document], metadatas=[{"space": "s"}], ids=[node_id])
    patch = json.dumps({"pack_id": "None" if genuine_none_pack else None})
    with store._engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {store._table} SET pack_id = 'None', "
                "metadata = metadata || CAST(:patch AS jsonb) "
                "WHERE node_id = :id"
            ),
            {"id": node_id, "patch": patch},
        )


def _all_rows(store):
    """``(node_id, pack_id, document, metadata)`` 튜플 전량, node_id 순."""
    with store._engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    f"SELECT node_id, pack_id, document, metadata FROM {store._table} "
                    "ORDER BY node_id"
                )
            )
            .mappings()
            .all()
        )
    return [
        (r["node_id"], r["pack_id"], r["document"], repair._as_dict_metadata(r["metadata"]))
        for r in rows
    ]


def _pg_url() -> str:
    return os.environ["OPENCRAB_PG_TEST_URL"]


# ---------------------------------------------------------------------------
# 핵심 acceptance criteria: 복구 전 거부 -> 복구 후 재적재 성공
# ---------------------------------------------------------------------------


class TestLegitimateOwnerReingestAfterRepair:
    def test_gate_rejects_the_reingest_before_repair(self, pg_store):
        """레거시 오염 행이 있으면 #197 게이트가 정당한 재적재를 막는다(현재 코드는
        정상 동작 중 -- 이 테스트는 그 사실 자체를 문서화하는 회귀 테스트다)."""
        _seed_and_contaminate(pg_store, "shared")
        with pytest.raises(ValueError):
            pg_store.upsert_texts(texts=["재적재 시도"], metadatas=[{"pack_id": ""}], ids=["shared"])

    def test_reingest_succeeds_after_repair(self, pg_store):
        _seed_and_contaminate(pg_store, "shared")
        rows = repair.repair(pg_store._engine, pg_store._table, backup_to=None)
        assert len(rows) == 1

        pg_store.upsert_texts(texts=["재적재 성공"], metadatas=[{"pack_id": ""}], ids=["shared"])
        hit = pg_store.get_by_id("shared")
        assert hit["document"] == "재적재 성공"


# ---------------------------------------------------------------------------
# 탐지 정확도 (위양성 0) -- 대조군 4종
# ---------------------------------------------------------------------------


class TestDetectionIsExactWithNoFalsePositives:
    def test_detect_reports_only_the_contaminated_row(self, pg_store):
        pg_store.upsert_texts(texts=["정상 소유"], metadatas=[{"pack_id": "A"}], ids=["owned"])
        pg_store.upsert_texts(texts=["미소유"], metadatas=[{"pack_id": ""}], ids=["unowned"])
        _seed_and_contaminate(pg_store, "genuine_none_pack", genuine_none_pack=True)
        _seed_and_contaminate(pg_store, "legacy")

        detected = repair.detect(pg_store._engine, pg_store._table)
        assert [r["node_id"] for r in detected] == ["legacy"]
        assert detected[0]["pack_id"] == "None"


# ---------------------------------------------------------------------------
# 적용 -- 오염 행만 고치고 대조군은 완전히 불변
# ---------------------------------------------------------------------------


class TestApplyRepairsAndPreservesControlRows:
    def test_apply_fixes_only_the_contaminated_row(self, pg_store):
        pg_store.upsert_texts(texts=["정상"], metadatas=[{"pack_id": "A"}], ids=["owned"])
        pg_store.upsert_texts(texts=["미소유"], metadatas=[{"pack_id": ""}], ids=["unowned"])
        _seed_and_contaminate(pg_store, "genuine_none_pack", genuine_none_pack=True)
        _seed_and_contaminate(pg_store, "legacy")

        before = {row[0]: row[1:] for row in _all_rows(pg_store) if row[0] != "legacy"}
        repair.repair(pg_store._engine, pg_store._table, backup_to=None)
        after = {row[0]: row[1:] for row in _all_rows(pg_store) if row[0] != "legacy"}

        assert after == before, "대조군 행이 실행 전후로 바뀌었다"
        legacy = next(row for row in _all_rows(pg_store) if row[0] == "legacy")
        assert legacy[1] == ""


class TestDryRunWritesNothing:
    def test_dry_run_leaves_the_full_table_untouched(self, pg_store):
        pg_store.upsert_texts(texts=["정상"], metadatas=[{"pack_id": "A"}], ids=["owned"])
        _seed_and_contaminate(pg_store, "legacy")

        before = _all_rows(pg_store)
        code = repair.main(["--pg-url", _pg_url(), "--table", pg_store._table])
        after = _all_rows(pg_store)

        assert code == repair.EXIT_OK
        assert after == before, "dry-run이 테이블을 바꿨다"


# ---------------------------------------------------------------------------
# 백업 게이트
# ---------------------------------------------------------------------------


class TestApplyRequiresBackupOrExplicitSkip:
    def test_apply_without_backup_flags_is_rejected(self, pg_store):
        _seed_and_contaminate(pg_store, "legacy")
        before = _all_rows(pg_store)

        code = repair.main(["--pg-url", _pg_url(), "--table", pg_store._table, "--apply"])

        assert code == repair.EXIT_USAGE
        assert _all_rows(pg_store) == before

    def test_apply_with_an_existing_backup_path_is_rejected(self, pg_store, tmp_path):
        _seed_and_contaminate(pg_store, "legacy")
        backup_path = tmp_path / "backup.json"
        backup_path.write_text("{}", encoding="utf-8")
        before = _all_rows(pg_store)

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--backup-to", str(backup_path),
            ]
        )

        assert code == repair.EXIT_BACKUP
        assert _all_rows(pg_store) == before


class TestCliMessageMatchesWhatActuallyHappenedOnZeroRows:
    """대상 행이 0건이면 ``repair()``는 백업 파일을 만들지 않는다(#306 검증자
    지적). CLI가 이 경우에도 "backup written"을 출력하면 실제로 없는 파일을
    있다고 주장하는 셈이라 운영자를 오도한다."""

    def test_apply_with_zero_target_rows_does_not_claim_a_backup_file(
        self, pg_store, tmp_path, capsys
    ):
        pg_store.upsert_texts(
            texts=["오염되지 않은 정상 행"], metadatas=[{"space": "s"}], ids=["clean0"]
        )
        backup_path = tmp_path / "backup.json"

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--backup-to", str(backup_path),
            ]
        )

        assert code == repair.EXIT_OK
        assert not backup_path.exists(), "0건인데 백업 파일이 실제로 생겼다"
        out = capsys.readouterr().out
        assert "backup written" not in out, (
            "0건이라 파일이 없는데 CLI가 backup written 을 출력했다"
        )


class TestBackupFailureRollsBackTheWholeTransaction:
    def test_backup_write_failure_leaves_the_db_unchanged(self, pg_store, tmp_path, monkeypatch):
        _seed_and_contaminate(pg_store, "legacy")
        before = _all_rows(pg_store)

        def _boom(backup_to, snapshot):
            raise OSError("simulated backup write failure")

        monkeypatch.setattr(repair, "write_backup_atomic", _boom)

        with pytest.raises(OSError):
            repair.repair(
                pg_store._engine, pg_store._table, backup_to=str(tmp_path / "backup.json")
            )

        assert _all_rows(pg_store) == before, "백업 실패에도 DB 트랜잭션이 커밋됐다"


class TestCountMismatchAborts:
    def test_repair_raises_and_rolls_back_on_precount_disagreement(self, pg_store, monkeypatch):
        pg_store.upsert_texts(texts=["정상"], metadatas=[{"pack_id": "A"}], ids=["owned"])
        _seed_and_contaminate(pg_store, "legacy")
        before = _all_rows(pg_store)

        # 전체 행 수(2)는 오염 행 수(1)와 항상 달라 인위적으로 불일치를 만든다.
        monkeypatch.setattr(repair, "_count_sql", lambda table: f"SELECT count(*) FROM {table}")

        with pytest.raises(repair.CountMismatchError):
            repair.repair(pg_store._engine, pg_store._table, backup_to=None)

        assert _all_rows(pg_store) == before


# ---------------------------------------------------------------------------
# 롤백
# ---------------------------------------------------------------------------


class TestRollback:
    def _repair_and_snapshot(self, pg_store, tmp_path, node_ids):
        for node_id in node_ids:
            _seed_and_contaminate(pg_store, node_id)
        backup_path = tmp_path / "backup.json"
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))
        return backup_path

    def test_round_trip_restores_the_literal_none_value(self, pg_store, tmp_path):
        backup_path = self._repair_and_snapshot(pg_store, tmp_path, ["legacy"])
        assert repair.detect(pg_store._engine, pg_store._table) == []

        snapshot = repair.load_snapshot(str(backup_path))
        statuses = repair.rollback(pg_store._engine, pg_store._table, snapshot)

        assert statuses == {"legacy": repair.ROLLBACK_STATUS_DONE}
        restored = next(row for row in _all_rows(pg_store) if row[0] == "legacy")
        assert restored[1] == "None"

    def test_permanently_deleted_row_is_reported_as_deleted(self, pg_store, tmp_path):
        backup_path = self._repair_and_snapshot(pg_store, tmp_path, ["a", "b"])
        with pg_store._engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {pg_store._table} WHERE node_id = 'a'"))

        snapshot = repair.load_snapshot(str(backup_path))
        statuses = repair.rollback(pg_store._engine, pg_store._table, snapshot)

        assert statuses["a"] == repair.ROLLBACK_STATUS_DELETED
        assert statuses["b"] == repair.ROLLBACK_STATUS_DONE

    def test_deleted_and_recreated_row_is_skipped_via_xmin_but_siblings_roll_back(
        self, pg_store, tmp_path
    ):
        backup_path = self._repair_and_snapshot(pg_store, tmp_path, ["a", "b"])
        with pg_store._engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {pg_store._table} WHERE node_id = 'a'"))
        pg_store.upsert_texts(texts=["다른 내용"], metadatas=[{"pack_id": "new"}], ids=["a"])

        snapshot = repair.load_snapshot(str(backup_path))
        statuses = repair.rollback(pg_store._engine, pg_store._table, snapshot)

        assert statuses["a"] == repair.ROLLBACK_STATUS_XMIN_MISMATCH
        assert statuses["b"] == repair.ROLLBACK_STATUS_DONE
        rows = {row[0]: row[1] for row in _all_rows(pg_store)}
        assert rows["a"] == "new", "재생성된 행이 롤백으로 조용히 덮어써졌다"
        assert rows["b"] == "None"

    def test_cli_reports_a_partial_rollback_as_exit_code_5(self, pg_store, tmp_path):
        backup_path = self._repair_and_snapshot(pg_store, tmp_path, ["a", "b"])
        with pg_store._engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {pg_store._table} WHERE node_id = 'a'"))

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_COUNT_MISMATCH

    def test_rollback_preserves_a_subsequent_legitimate_ownership_transfer(self, pg_store, tmp_path):
        """§10.3: 복구 후 그 슬롯을 실제 팩이 정당하게 인수하면, 옛 백업으로의
        롤백은 xmin 불일치로 그 행을 건너뛰고 새 소유권을 그대로 둬야 한다."""
        backup_path = self._repair_and_snapshot(pg_store, tmp_path, ["shared"])

        pg_store.upsert_texts(texts=["진짜 소유"], metadatas=[{"pack_id": "real_pack"}], ids=["shared"])

        snapshot = repair.load_snapshot(str(backup_path))
        statuses = repair.rollback(pg_store._engine, pg_store._table, snapshot)

        assert statuses["shared"] == repair.ROLLBACK_STATUS_XMIN_MISMATCH
        hit = pg_store.get_by_id("shared")
        assert hit["metadata"]["pack_id"] == "real_pack", "롤백이 정당한 신규 소유권을 덮어썼다"


class TestRollbackWarningBannerPrecedesTheDestructiveCall:
    """xmin 한계 경고는 문서/docstring 뿐 아니라 실행 시 stderr 로도 나가야 하고,
    그 출력은 실제 롤백(파괴적 동작)보다 반드시 먼저 일어나야 한다(팀리드 지적,
    #306 라운드4). ``repair.rollback`` 을 스파이로 감싸 호출 시점에 stderr 를
    선점 확인함으로써 순서를 실증한다."""

    def test_warning_appears_on_stderr_before_rollback_is_called(
        self, pg_store, tmp_path, capsys, monkeypatch
    ):
        _seed_and_contaminate(pg_store, "legacy")
        backup_path = tmp_path / "backup.json"
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))

        calls = []
        real_rollback = repair.rollback

        def spy_rollback(engine, table, snapshot):
            # main()이 여기 도달하기 전에 이미 경고를 stderr 에 썼어야 한다.
            captured = capsys.readouterr()
            assert "WARNING" in captured.err and "xmin" in captured.err, (
                "롤백 실행 전에 stderr 경고 배너가 없었다"
            )
            calls.append("warned_before_rollback")
            return real_rollback(engine, table, snapshot)

        monkeypatch.setattr(repair, "rollback", spy_rollback)

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_OK
        assert calls == ["warned_before_rollback"], "rollback()이 호출되지 않았다"


class TestBackupSignatureAndSnapshotIntegrity:
    def test_signature_mismatch_is_rejected_and_nothing_changes(self, pg_store, tmp_path):
        backup_path = tmp_path / "backup.json"
        _seed_and_contaminate(pg_store, "legacy")
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))
        before = _all_rows(pg_store)

        tampered = json.loads(backup_path.read_text(encoding="utf-8"))
        tampered["table"] = "some_other_table"
        backup_path.write_text(json.dumps(tampered), encoding="utf-8")

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_BACKUP
        assert _all_rows(pg_store) == before

    def test_duplicate_node_id_in_snapshot_is_rejected(self, tmp_path):
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps(
                {
                    "table": "t",
                    "database": "d",
                    "rows": [
                        {"node_id": "x", "prior_pack_id": "None", "metadata": {}, "xmin": "1"},
                        {"node_id": "x", "prior_pack_id": "None", "metadata": {}, "xmin": "2"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))


# ---------------------------------------------------------------------------
# CLI 종료 코드
# ---------------------------------------------------------------------------


class TestCliExitCodeMatrix:
    def test_bad_table_identifier_is_rejected(self):
        code = repair.main(["--pg-url", "postgresql://x", "--table", "bad-table; drop"])
        assert code == repair.EXIT_USAGE

    def test_connection_failure_is_reported_as_precondition_failure(self):
        code = repair.main(
            [
                "--pg-url", "postgresql://nouser:nopass@127.0.0.1:1/doesnotexist",
                "--table", "vtest",
            ]
        )
        assert code == repair.EXIT_PRECONDITION

    def test_rollback_without_apply_is_rejected(self, pg_store, tmp_path):
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps({"table": "t", "database": "d", "rows": []}), encoding="utf-8"
        )

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_USAGE


# ---------------------------------------------------------------------------
# 실동시성 (§10.3) -- 단일 CTE 문의 FOR UPDATE 직렬화를 실제 잠금 대기로 증명
# ---------------------------------------------------------------------------


class TestConcurrentRepairsSerializeViaRowLock:
    def test_a_second_connection_blocks_until_the_first_commits_then_finds_nothing_left(
        self, pg_store
    ):
        _seed_and_contaminate(pg_store, "shared")
        table = pg_store._table

        conn_a = pg_store._engine.connect()
        trans_a = conn_a.begin()
        rows_a = conn_a.execute(text(repair._repair_sql(table))).mappings().all()
        assert len(rows_a) == 1, "A가 오염 행의 FOR UPDATE 잠금을 얻지 못했다"

        b_done = threading.Event()
        b_result: dict = {}

        def run_b():
            conn_b = pg_store._engine.connect()
            trans_b = conn_b.begin()
            try:
                b_result["rows"] = conn_b.execute(
                    text(repair._repair_sql(table))
                ).mappings().all()
                trans_b.commit()
            finally:
                conn_b.close()
            b_done.set()

        t = threading.Thread(target=run_b)
        t.start()
        try:
            time.sleep(0.5)
            assert not b_done.is_set(), (
                "B가 A의 미커밋 FOR UPDATE 잠금을 기다리지 않고 곧바로 반환했다"
            )

            trans_a.commit()
            t.join(timeout=5)
            assert b_done.is_set(), "A 커밋 후에도 B가 반환하지 않았다"
            assert list(b_result["rows"]) == [], (
                "A가 이미 처리한 행을 B가 다시 처리했다 -- 이중 처리"
            )
        finally:
            conn_a.close()

        final = {row[0]: row[1] for row in _all_rows(pg_store)}
        assert final["shared"] == "", "최종 상태가 정확히 한 번 복구된 값이 아니다"
