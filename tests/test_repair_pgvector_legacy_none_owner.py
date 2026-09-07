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


def _dsn_with_ambiguous_query_host() -> str:
    """실제로는 연결에 성공하지만 ``target_identity_reason``이 대상 서버를
    하나로 특정할 수 없다고 판단해야 하는 DSN을 만든다.

    ``OPENCRAB_PG_TEST_URL``에 ``?host=localhost``(이미 쿼리가 있으면
    ``&host=localhost``)를 덧붙인다. authority의 host는 그대로 있어 연결은
    실제 테스트 DB로 정상적으로 이어지지만(-- 엑조틱한 유닉스 소켓이나
    다중 호스트 클러스터 없이도 실제 연결 성공 케이스를 재현할 수 있다),
    쿼리 문자열의 ``host`` 키가 authority를 덮어쓸 수 있는 형태이므로
    ``target_identity_reason``은 이를 거부해야 한다(codex r7 지적의 실측
    재현: authority만 보면 안전해 보이지만 실제로는 아닌 DSN)."""
    base = _pg_url()
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}host=localhost"


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


# ---------------------------------------------------------------------------
# 대상 서버 식별 불가 시 fail-closed (#306 r7) -- 세 분기를 각각 고정
# ---------------------------------------------------------------------------


class TestApplyIsRefusedWhenTargetIdentityIsAmbiguous:
    """분기 1 (팀리드 지적, 가장 중요): ``--apply``, ``--skip-backup`` 없음,
    대상 서버 식별 불가 -> ``EXIT_PRECONDITION``이고 DB 쓰기가 0건이어야 한다.
    이 체크가 사라지면 롤백 없는 수리가 조용히 통과한다."""

    def test_apply_without_skip_backup_is_refused_and_db_is_untouched(
        self, pg_store, tmp_path
    ):
        _seed_and_contaminate(pg_store, "legacy")
        before = _all_rows(pg_store)
        backup_path = tmp_path / "backup.json"

        code = repair.main(
            [
                "--pg-url", _dsn_with_ambiguous_query_host(), "--table", pg_store._table,
                "--apply", "--backup-to", str(backup_path),
            ]
        )

        assert code == repair.EXIT_PRECONDITION
        assert not backup_path.exists()
        assert _all_rows(pg_store) == before, "대상 서버를 특정할 수 없는데 DB가 바뀌었다"


class TestApplyProceedsWithoutSnapshotWhenSkipBackupIsExplicit:
    """분기 2: ``--apply --skip-backup``, 대상 서버 식별 불가 -> 수리는
    진행되고 그 사유가 CLI 출력에 나타나야 한다(운영자가 직접 롤백을
    포기한 경로이므로 조용히 진행해서는 안 되고 반드시 알려야 한다)."""

    def test_apply_with_skip_backup_proceeds_and_prints_the_reason(self, pg_store, capsys):
        _seed_and_contaminate(pg_store, "legacy")

        code = repair.main(
            [
                "--pg-url", _dsn_with_ambiguous_query_host(), "--table", pg_store._table,
                "--apply", "--skip-backup",
            ]
        )

        assert code == repair.EXIT_OK
        out = capsys.readouterr().out
        assert "query string sets" in out, "사유가 CLI 출력에 나타나지 않았다"
        legacy = next(row for row in _all_rows(pg_store) if row[0] == "legacy")
        assert legacy[1] == "", "--skip-backup 경로인데 실제로 수리가 진행되지 않았다"


class TestRepairDirectCallDefendsAgainstAmbiguousTarget:
    """분기 3: ``main()``을 거치지 않고 ``repair()``를 직접 호출해도(예: 다른
    스크립트가 이 모듈을 라이브러리로 쓰는 경우) 대상 서버 식별 불가 시
    내부 방어 체크가 걸려야 한다. ``main()``의 fail-closed 게이트가 유일한
    방어선이면 안 된다 -- codex r7이 지적한 "두 호출 경로가 어긋난다" 구조적
    간극을 막는다."""

    def test_repair_called_directly_with_backup_to_raises_and_db_is_untouched(
        self, pg_store, tmp_path
    ):
        _seed_and_contaminate(pg_store, "legacy")
        backup_path = tmp_path / "backup.json"
        engine = repair.connect(_dsn_with_ambiguous_query_host())
        before = _all_rows(pg_store)

        with pytest.raises(repair.SnapshotError):
            repair.repair(engine, pg_store._table, backup_to=str(backup_path))

        assert not backup_path.exists()
        assert _all_rows(pg_store) == before, "예외가 났는데 DB가 바뀌었다(롤백 실패)"

    def test_repair_called_directly_without_backup_to_is_unaffected(self, pg_store):
        """``backup_to=None``(=``--skip-backup``에 대응)이면 식별 게이트
        자체가 걸리지 않고 수리가 정상 진행된다 -- 이 게이트는 백업을
        만들어야 할 때만 발동해야 한다."""
        _seed_and_contaminate(pg_store, "legacy")
        engine = repair.connect(_dsn_with_ambiguous_query_host())

        rows = repair.repair(engine, pg_store._table, backup_to=None)

        assert len(rows) == 1
        legacy = next(row for row in _all_rows(pg_store) if row[0] == "legacy")
        assert legacy[1] == ""


class TestRollbackIsRefusedWhenTargetIdentityIsAmbiguous:
    """분기 4 (이중 적대검증에서 두 채널이 독립적으로 발견한 실결함): ``main()``의
    ``--rollback-from`` 경로는 ``identity_reason``을 계산만 하고 쓰지 않았다.
    대상 서버를 하나로 특정할 수 없으면 서명 검사 자체가 엉뚱한 서버의 host/port
    와 비교하게 되므로, 스냅샷을 읽기 전에 거부해야 한다. ``--rollback-from``에는
    ``--apply``의 ``--skip-backup``에 대응하는 탈출구가 없다 -- 롤백은 결속이
    선택 사항이 아니다."""

    def test_rollback_is_refused_and_db_is_untouched(self, pg_store, tmp_path):
        backup_path = tmp_path / "backup.json"
        _seed_and_contaminate(pg_store, "legacy")
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))
        before = _all_rows(pg_store)

        code = repair.main(
            [
                "--pg-url", _dsn_with_ambiguous_query_host(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_PRECONDITION
        assert _all_rows(pg_store) == before, "대상 서버를 특정할 수 없는데 롤백이 실행됐다"


class TestTargetIdentityRejectsMultiHostAuthority:
    """이중 적대검증(외부 CLI 채널)이 새로 발견한 실결함: 콤마로 구분한 다중
    호스트 authority(``postgresql://u:p@h1,h2/db``, libpq의 다중-호스트/failover
    문법)는 ``engine.url.host``가 ``'h1,h2'``라는 비어 있지 않은 문자열이 되어
    빈 host 검사를 통과했지만, 실제 연결은 h1/h2 가운데 그때그때 다른 서버로
    갈 수 있어 스냅샷을 한 서버에 결속할 수 없다. 이 DSN은 실제로 연결하지
    않으므로(``connect()``는 지연 연결) 실 PostgreSQL 없이도 이 테스트를 돌릴
    수 있다."""

    def test_comma_separated_host_authority_is_rejected(self):
        engine = repair.connect("postgresql://u:p@host-a,host-b/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "host-a,host-b" in reason


class TestTargetIdentityRejectsAmbiguousEnvironment:
    """이중 적대검증(3라운드, 새 컨텍스트 검증자)이 실측 재현한 실결함: DSN이
    host를 확정적으로 적어도 ``PGHOSTADDR``/``PGSERVICE``/``PGSERVICEFILE``
    환경변수가 설정돼 있으면 실제 연결은 DSN과 무관한 서버로 갈 수 있다.
    존재하지 않는 host 이름을 DSN에 적고 ``PGHOSTADDR``를 실 테스트 서버
    주소로 설정했더니 연결이 실제로 성공했다(직접 재현, 이 테스트 자체는
    연결하지 않으므로 실 PostgreSQL 없이도 돌릴 수 있다). ``PGPORT``도 DSN이
    포트를 생략했을 때만 같은 방식으로 적용된다. ``PGHOST``는 DSN이 host를
    이미 준 경우 무해하므로(libpq는 명시적 conninfo 값을 환경보다 우선한다)
    거부 대상에 넣지 않는다 -- 그 경우는 host 자체가 비어 있을 때만 걸리는
    빈 host 검사(위)로 이미 충분하다."""

    def test_pghostaddr_env_var_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
        engine = repair.connect("postgresql://u:p@localhost/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "PGHOSTADDR" in reason

    def test_pgservice_env_var_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PGSERVICE", "some-service")
        engine = repair.connect("postgresql://u:p@localhost/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "PGSERVICE" in reason

    def test_pgservicefile_env_var_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PGSERVICEFILE", "/nonexistent/service.conf")
        engine = repair.connect("postgresql://u:p@localhost/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "PGSERVICEFILE" in reason

    def test_pgport_env_var_is_rejected_when_dsn_omits_port(self, monkeypatch):
        monkeypatch.setenv("PGPORT", "1")
        engine = repair.connect("postgresql://u:p@localhost/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "PGPORT" in reason

    def test_pgport_env_var_is_harmless_when_dsn_states_port_explicitly(self, monkeypatch):
        monkeypatch.setenv("PGPORT", "1")
        engine = repair.connect("postgresql://u:p@localhost:5432/db")

        assert repair.target_identity_reason(engine) is None

    def test_pghost_env_var_alone_is_harmless_when_dsn_states_host(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "some-other-host")
        engine = repair.connect("postgresql://u:p@localhost/db")

        assert repair.target_identity_reason(engine) is None


class TestTargetIdentityRejectsUnknownQueryKeys:
    """이중 적대검증(4라운드, 새 컨텍스트 검증자)이 실측 재현한 실결함:
    psycopg2는 query 문자열의 ``dsn`` 키를 conninfo 문자열로 병합하고, 그
    문자열 안의 ``hostaddr``/``host``/``service``는 kwargs에 같은 키가 없는
    한 그대로 살아남아 authority와 무관하게 실제 연결 대상을 정한다.
    ``host``/``port``/``service``/``hostaddr`` 이름만 나열하는 블록리스트로는
    이런 드라이버별 캐리어 키를 계속 놓칠 수 있으므로, 알려진 안전 키만
    허용하는 화이트리스트로 이 범주 전체를 막는다."""

    def test_psycopg2_dsn_carrier_key_is_rejected(self):
        engine = repair.connect(
            "postgresql://u:p@localhost/db?dsn=hostaddr%3D127.0.0.1"
        )

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "dsn" in reason

    def test_unrecognized_query_key_is_rejected_even_if_not_a_known_carrier(self):
        engine = repair.connect(
            "postgresql://u:p@localhost/db?some_future_driver_option=x"
        )

        assert repair.target_identity_reason(engine) is not None

    def test_known_safe_query_keys_are_still_harmless(self):
        engine = repair.connect(
            "postgresql://u:p@localhost/db?sslmode=require&connect_timeout=5"
        )

        assert repair.target_identity_reason(engine) is None


class TestTargetIdentityRejectsSchemaBindingEscapeHatches:
    """이중 적대검증(코덱스 리뷰)이 실측 재현한 실결함: ``options`` query 키는
    이전까지 ``_SAFE_TARGET_QUERY_KEYS``에 안전 키로 올라 있었지만,
    ``options=-c search_path=other_schema``처럼 접속 시점 스키마를 바꿀 수
    있다. 스냅샷 서명은 table/database/host/port만 기록하고 스키마는 기록하지
    않으므로, 수리와 롤백이 ``options``로 서로 다른 스키마를 가리키면
    서명은 그대로 일치해 통과하면서도 실제로는 의도한 것과 다른 릴레이션에
    적용된다. ``PGOPTIONS`` 환경변수도 DSN이 옵션을 생략했을 때 같은 방식으로
    적용되므로 같은 이유로 거부한다."""

    def test_options_query_key_is_rejected(self):
        engine = repair.connect(
            "postgresql://u:p@localhost/db?options=-c%20search_path%3Dother"
        )

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "options" in reason

    def test_pgoptions_env_var_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PGOPTIONS", "-c search_path=other")
        engine = repair.connect("postgresql://u:p@localhost/db")

        reason = repair.target_identity_reason(engine)

        assert reason is not None
        assert "PGOPTIONS" in reason


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

    def test_host_mismatch_is_rejected_and_nothing_changes(self, pg_store, tmp_path):
        backup_path = tmp_path / "backup.json"
        _seed_and_contaminate(pg_store, "legacy")
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))
        before = _all_rows(pg_store)

        tampered = json.loads(backup_path.read_text(encoding="utf-8"))
        tampered["host"] = "some-other-host.invalid"
        backup_path.write_text(json.dumps(tampered), encoding="utf-8")

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_BACKUP
        assert _all_rows(pg_store) == before

    def test_port_mismatch_is_rejected_and_nothing_changes(self, pg_store, tmp_path):
        backup_path = tmp_path / "backup.json"
        _seed_and_contaminate(pg_store, "legacy")
        repair.repair(pg_store._engine, pg_store._table, backup_to=str(backup_path))
        before = _all_rows(pg_store)

        tampered = json.loads(backup_path.read_text(encoding="utf-8"))
        tampered["port"] = 1
        backup_path.write_text(json.dumps(tampered), encoding="utf-8")

        code = repair.main(
            [
                "--pg-url", _pg_url(), "--table", pg_store._table,
                "--apply", "--rollback-from", str(backup_path),
            ]
        )

        assert code == repair.EXIT_BACKUP
        assert _all_rows(pg_store) == before

    def test_missing_host_key_in_snapshot_is_rejected(self):
        """``host``키가 아예 없는 스냅샷(구버전/손상/수기 조작)은 ``None``이
        되어 어떤 현재 host와도 일치하지 않아야 한다(우연 통과 금지)."""
        with pytest.raises(repair.SnapshotError):
            repair.validate_snapshot_signature(
                {"table": "t", "database": "d", "port": 5432},
                table="t", database="d", host="localhost", port=5432,
            )

    def test_missing_port_key_in_snapshot_is_rejected(self):
        """``port``키가 없으면 스냅샷 쪽은 ``None``, 현재 값은 포트 생략을
        5432로 정규화한 값이라 ``None != 5432``로 항상 거부돼야 한다."""
        with pytest.raises(repair.SnapshotError):
            repair.validate_snapshot_signature(
                {"table": "t", "database": "d", "host": "localhost"},
                table="t", database="d", host="localhost", port=None,
            )

    def test_omitted_port_and_explicit_5432_are_compatible(self):
        """스냅샷에 명시적으로 적힌 5432와, 현재 DSN에서 포트를 생략해
        ``None``으로 넘어온 값(5432로 정규화)은 서로 호환돼야 한다."""
        repair.validate_snapshot_signature(
            {"table": "t", "database": "d", "host": "localhost", "port": 5432},
            table="t", database="d", host="localhost", port=None,
        )

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

    def test_non_object_top_level_snapshot_is_rejected(self, tmp_path):
        """이중 적대검증에서 codex 채널이 실측 지적한 결함: 유효한 JSON이지만
        최상위가 객체가 아니면(예: 배열) ``AttributeError``가 그대로 새어나가
        문서화된 백업-검증 종료 코드(4) 대신 처리되지 않은 트레이스백이 됐다.
        ``SnapshotError``로 변환해 기존 예외 계약 안에 넣는다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_non_list_rows_snapshot_is_rejected(self, tmp_path):
        """``rows``가 리스트가 아니면(예: 문자열) 반복 시 각 문자를 순회하며
        ``TypeError``가 새어나갔다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps({"table": "t", "database": "d", "rows": "not-a-list"}),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_row_missing_node_id_is_rejected(self, tmp_path):
        """행에 ``node_id``가 없으면 ``KeyError``가 새어나갔다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps({"table": "t", "database": "d", "rows": [{"xmin": "1"}]}),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_row_missing_xmin_is_rejected(self, tmp_path):
        """``node_id``는 있어도 ``xmin``이 없으면 ``load_snapshot`` 통과 뒤
        ``rollback()``이 실제 롤백 시도 중에야 ``KeyError``를 내 처리되지 않은
        트레이스백이 됐다. 로드 시점에 미리 걸러 exit code 4로 통일한다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps({"table": "t", "database": "d", "rows": [{"node_id": "x"}]}),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_missing_rows_key_is_rejected(self, tmp_path):
        """새 컨텍스트 독립 검증자(round 6)가 실측 지적한 결함: ``rows`` 키가
        아예 없는 스냅샷은 ``snapshot.get("rows", [])``가 빈 리스트로 조용히
        받아들여 ``load_snapshot``은 통과하지만, ``rollback()``은
        ``snapshot["rows"]``를 직접 인덱싱해 서명만 맞으면 나중에
        ``KeyError``를 새어보낸다. 로드 시점에 키 존재 자체를 요구한다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps({"table": "t", "database": "d"}),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_non_string_node_id_is_rejected(self, tmp_path):
        """같은 라운드가 실측 재현한 결함: ``node_id``가 문자열이 아닌
        해시 불가 타입(리스트/객체)이면 존재 검사는 통과하고
        ``set(node_ids)``에서 ``TypeError: unhashable type``이 새어나갔다.
        ``node_id``는 항상 문자열이라는 불변식을 로드 시점에 강제한다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps(
                {
                    "table": "t",
                    "database": "d",
                    "rows": [{"node_id": ["a", "b"], "xmin": "1"}],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_non_utf8_snapshot_bytes_are_rejected(self, tmp_path):
        """새 컨텍스트 독립 검증자(round 7)가 실측 재현한 형제 결함:
        스냅샷 파일에 비UTF-8 바이트가 섞이면(수기 조작, 부분 기록,
        비트 부패) ``open(path, encoding="utf-8")``이 ``UnicodeDecodeError``를
        낸다. 이 예외는 ``UnicodeError``/``ValueError``의 하위형이라
        ``main()``이 잡는 ``(OSError, json.JSONDecodeError, SnapshotError)``
        어디에도 걸리지 않고 그대로 새어나간다. ``load_snapshot``의 계약은
        "손상된 스냅샷은 종료 코드 4"이므로 여기서 미리 막는다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_bytes(b'{"table":"t","database":"d","rows":[]\xff}')

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_truncated_multibyte_snapshot_is_rejected(self, tmp_path):
        """같은 형제 결함의 변형: 멀티바이트 문자 중간에서 파일이 잘리면
        (기록 도중 중단 등) 같은 ``UnicodeDecodeError``가 새어나간다.
        ``json.dumps(..., ensure_ascii=False)``로 "가"를 원시 UTF-8
        3바이트로 인코딩한 뒤 마지막 1바이트를 잘라 그 시퀀스를 미완성으로
        만든다(``write_backup_atomic``도 ``ensure_ascii=False``로 쓴다)."""
        backup_path = tmp_path / "backup.json"
        payload = '{"table":"t","database":"d","rows":[],"note":"가'.encode()
        backup_path.write_bytes(payload[:-1])

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_non_string_xmin_is_rejected(self, tmp_path):
        """외부 채널(round 7)이 실측 재현한 형제 결함: ``xmin``이 문자열이
        아닌 타입(리스트/객체)이면 ``"xmin" not in row`` 검사는 통과하고,
        ``rollback()``이 그 값을 바인드 파라미터로 psycopg2에 넘기는 시점에야
        ``ProgrammingError: can't adapt type 'dict'``가 새어나갔다. 이
        도구가 스스로 쓰는 ``xmin``은 항상 ``xmin::text``로 캐스팅된 문자열
        이므로(``_repair_sql``), 로드 시점에 같은 불변식을 강제한다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_text(
            json.dumps(
                {
                    "table": "t",
                    "database": "d",
                    "rows": [{"node_id": "x", "xmin": {"invalid": "object"}}],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_deeply_nested_snapshot_is_rejected(self, tmp_path):
        """새 컨텍스트 독립 검증자(round 8)가 실측 재현한 형제 결함:
        중첩 깊이가 충분히 큰 JSON 배열(수기 조작이나 손상으로만 나올 수
        있는 형태, 이 도구 자신의 ``write_backup_atomic``은 이런 깊이의
        파일을 만들지 않는다)을 ``json.load()``가 파싱하다 파이썬 재귀
        한계에 걸려 ``RecursionError``를 낸다. 이 예외는 ``Exception``의
        직계 하위형(``ValueError``가 아니다)이라 어떤 catch 튜플에도
        안 걸리고 새어나갔다."""
        backup_path = tmp_path / "backup.json"
        depth = 20000
        backup_path.write_bytes(b"[" * depth + b'"x"' + b"]" * depth)

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_oversized_integer_literal_snapshot_is_rejected(self, tmp_path):
        """같은 라운드가 실측 재현한 변형: 5000자리(파이썬 기본 상한
        4300자리 초과)짜리 정수 리터럴이 어디에든 있으면 ``json.load()``의
        내부 ``int()`` 변환이 ``ValueError: Exceeds the limit ... for
        integer string conversion``을 낸다. ``json.JSONDecodeError``는
        ``ValueError``의 하위형이지만 그 역은 성립하지 않으므로, 이
        ``ValueError``는 ``main()``의 ``(OSError, json.JSONDecodeError,
        SnapshotError)`` 어디에도 걸리지 않고 새어나갔다. 파일 크기
        5킬로바이트 남짓으로 자원 병리형 입력도 아니다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_bytes(b'{"rows": [], "port": ' + b"9" * 5000 + b"}")

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_lone_surrogate_in_node_id_is_rejected(self, tmp_path):
        """새 컨텍스트 독립 검증자(round 9)가 실측 재현한 형제 결함:
        ``\\ud800``처럼 짝 없는 서로게이트 코드포인트는 ``json.load()``가
        오류 없이 파싱해 어떤 코덱으로도 인코딩할 수 없는 ``str``을 만든다.
        ``isinstance(..., str)`` 검사는 통과하지만, ``rollback()``이 그
        값을 psycopg2 바인드 파라미터로 넘기는 시점에 ``UnicodeEncodeError``
        가 새어나갔다(``main()``의 ``--rollback-from`` 분기는 이 예외를
        잡지 않는다)."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_bytes(
            b'{"table":"t","database":"d","rows":[{"node_id":"n\\ud800","xmin":"1"}]}'
        )

        with pytest.raises(repair.SnapshotError):
            repair.load_snapshot(str(backup_path))

    def test_nul_character_in_xmin_is_rejected(self, tmp_path):
        """같은 라운드가 실측 재현한 변형: ``xmin`` 값에 NUL 문자
        (``\\u0000``)가 섞이면 PostgreSQL ``text``가 담을 수 없는 값이라
        psycopg2가 클라이언트 측에서 ``ValueError: A string literal
        cannot contain NUL (0x00) characters.``를 낸다. 이 도구가 스스로
        쓰는 ``xmin``은 DB에서 읽은 순수 숫자 문자열뿐이므로(``_repair_sql``
        의 ``xmin::text``), 이 형태는 수기 조작·손상 스냅샷에서만 나온다."""
        backup_path = tmp_path / "backup.json"
        backup_path.write_bytes(
            b'{"table":"t","database":"d","rows":[{"node_id":"n","xmin":"1\\u0000"}]}'
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

    def test_connection_failure_is_reported_as_precondition_failure(self, capsys):
        code = repair.main(
            [
                "--pg-url", "postgresql://nouser:nopass@127.0.0.1:1/doesnotexist",
                "--table", "vtest",
            ]
        )
        assert code == repair.EXIT_PRECONDITION
        captured = capsys.readouterr()
        assert "nopass" not in captured.out, "authority 비밀번호가 stdout에 그대로 새어나왔다"
        assert "nopass" not in captured.err, "authority 비밀번호가 stderr에 그대로 새어나왔다"
        assert "***" in captured.out, "마스킹된 자리표시자가 출력에 없다"

    def test_unparseable_pg_url_prints_placeholder_without_crashing(self):
        code = repair.main(["--pg-url", "not-a-valid-dsn ::: %%%", "--table", "vtest"])
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
