from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from crabharness.models import (
    ArtifactBundle,
    ArtifactFile,
    DelegationJob,
    MissionSpec,
    ValidationIssue,
    ValidationReport,
)

from codex_workers._base import run_validation


def _db_path(root_dir: Path) -> Path:
    return root_dir / "nara.db"


def _workspace_dir(root_dir: Path) -> Path:
    return root_dir / "_workspace"


def _fetchone_dict(cursor: sqlite3.Cursor, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = cursor.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def collect_soeak_bundle(
    root_dir: Path,
    mission: MissionSpec,
    job: DelegationJob,
    run_id: str,
    progress_path: Path | None = None,
    error_log_path: Path | None = None,
) -> ArtifactBundle:
    db_path = _db_path(root_dir)
    workspace_dir = _workspace_dir(root_dir)
    progress_file = progress_path or workspace_dir / "soeak-detail-crawler-progress.json"
    error_file = error_log_path or workspace_dir / "soeak-detail-crawler-errors.ndjson"
    bid_no = str(job.target.get("bid_no", ""))
    bid_ord = str(job.target.get("bid_ntce_ord", "000"))
    files: list[ArtifactFile] = [
        ArtifactFile(kind="sqlite_dataset", path=str(db_path), format="sqlite", description="Primary SOEAK analysis database."),
        ArtifactFile(
            kind="progress_log",
            path=str(progress_file),
            format="json",
            description="Latest crawler progress snapshot.",
        ),
        ArtifactFile(
            kind="error_log",
            path=str(error_file),
            format="ndjson",
            description="Crawler error event log.",
        ),
    ]

    summary: dict[str, Any] = {
        "bid_no": bid_no,
        "bid_ntce_ord": bid_ord,
        "case": None,
        "bidders_count": 0,
        "reserve_price_count": 0,
        "progress": None,
        "db_exists": db_path.exists(),
    }

    metrics: dict[str, Any] = {}

    if db_path.exists():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.cursor()
            case = _fetchone_dict(
                cursor,
                """
                SELECT *
                FROM analysis_soeak_cases
                WHERE bid_ntce_no = ? AND bid_ntce_ord = ?
                """,
                (bid_no, bid_ord),
            )
            bidders_count = cursor.execute(
                """
                SELECT COUNT(*)
                FROM analysis_soeak_bidders
                WHERE bid_ntce_no = ?
                """,
                (bid_no,),
            ).fetchone()[0]
            reserve_price_count = cursor.execute(
                """
                SELECT COUNT(*)
                FROM analysis_soeak_reserve_prices
                WHERE bid_ntce_no = ?
                """,
                (bid_no,),
            ).fetchone()[0]
        finally:
            connection.close()

        summary["case"] = case
        summary["bidders_count"] = bidders_count
        summary["reserve_price_count"] = reserve_price_count
        metrics.update(
            {
                "bidders_count": bidders_count,
                "reserve_price_count": reserve_price_count,
                "winner_rate": None if case is None else case.get("winner_rate"),
                "reserve_ratio": None if case is None else case.get("reserve_ratio"),
            }
        )

    if progress_file.exists():
        summary["progress"] = json.loads(progress_file.read_text(encoding="utf-8"))

    return ArtifactBundle(
        run_id=run_id,
        mission_id=mission.mission_id,
        worker_id=job.worker_id,
        job_id=job.job_id,
        target_ref=job.target,
        files=files,
        metrics=metrics,
        summary=summary,
    )


def _field_check(bundle: ArtifactBundle, field: str) -> bool:
    if field == "bidders":
        return int(bundle.summary.get("bidders_count", 0) or 0) > 0
    if field == "reserve_prices":
        return int(bundle.summary.get("reserve_price_count", 0) or 0) > 0
    case = bundle.summary.get("case") or {}
    return case.get(field) not in (None, "", [])


def _extra_checks(bundle: ArtifactBundle, issues: list[ValidationIssue]) -> None:
    if not bundle.summary.get("db_exists"):
        issues.append(
            ValidationIssue(
                code="missing_db",
                severity="error",
                message="Expected `nara.db` does not exist in the workspace root.",
            )
        )

    progress = bundle.summary.get("progress") or {}
    if progress.get("fatal"):
        issues.append(
            ValidationIssue(
                code="fatal_progress",
                severity="error",
                message="Crawler progress file reports a fatal execution error.",
            )
        )


def validate_soeak_bundle(bundle: ArtifactBundle, mission: MissionSpec) -> ValidationReport:
    return run_validation(
        bundle,
        mission,
        field_check=_field_check,
        missing_message=lambda field: f"Required field `{field}` is missing from the SOEAK artifact bundle.",
        default_required_fields=None,
        extra_checks=_extra_checks,
        fail_check=lambda _bundle, passed, issues: passed == 0 and bool(issues),
    )



def doctor(root_dir):
    """Check SOEAK worker runtime prerequisites."""
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(root_dir)
    runtime = root / "worker_runtime"
    checks = [
        {"name": "node", "ok": shutil.which("node") is not None},
        {"name": "npm", "ok": shutil.which("npm") is not None},
        {"name": "worker_runtime", "ok": runtime.exists()},
        {"name": "runtime_package", "ok": (runtime / "package.json").exists()},
        {"name": "runtime_node_modules", "ok": (runtime / "node_modules").exists()},
        {"name": "worker_script", "ok": (root / "soeak-detail-crawler.ts").exists()},
    ]
    doctor_stdout = ""
    doctor_stderr = ""
    doctor_ok = False
    if checks[1]["ok"] and (runtime / "package.json").exists():
        completed = subprocess.run(
            ["npm", "run", "doctor"],
            cwd=runtime,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        doctor_stdout = completed.stdout.strip()
        doctor_stderr = completed.stderr.strip()
        doctor_ok = completed.returncode == 0
    checks.append({"name": "playwright_module", "ok": doctor_ok})
    return {
        "worker_id": "codex.soeak.detail",
        "root_dir": str(root),
        "runtime_dir": str(runtime),
        "checks": checks,
        "ok": all(check["ok"] for check in checks),
        "doctor_stdout": doctor_stdout,
        "doctor_stderr": doctor_stderr,
    }
