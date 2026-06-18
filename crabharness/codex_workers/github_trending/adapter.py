from __future__ import annotations

from pathlib import Path
from typing import Any

from crabharness.models import (
    ArtifactBundle,
    ArtifactFile,
    DelegationJob,
    MissionSpec,
    ValidationReport,
)

from codex_workers._base import run_validation


def collect_bundle(
    root_dir: Path,
    mission: MissionSpec,
    job: DelegationJob,
    run_id: str,
    progress_path: Path | None = None,
    error_log_path: Path | None = None,
) -> ArtifactBundle:
    """Collect GitHub trending repos. Stub implementation."""
    workspace_dir = root_dir / "_workspace"

    files: list[ArtifactFile] = [
        ArtifactFile(
            kind="progress_log",
            path=str(progress_path or workspace_dir / "github-trending-progress.json"),
            format="json",
            description="GitHub trending crawl progress.",
        ),
    ]

    # Stub: simulate finding 5 repos
    summary: dict[str, Any] = {
        "language": job.target.get("language", "python"),
        "since": job.target.get("since", "weekly"),
        "repos_count": 5,
        "repos": [
            {"name": "anthropics/anthropic-sdk-python", "stars": 1200, "topic": "ai-sdk"},
            {"name": "openai/swarm", "stars": 800, "topic": "agents"},
            {"name": "karpathy/minGPT", "stars": 600, "topic": "llm"},
            {"name": "simonw/llm", "stars": 400, "topic": "cli"},
            {"name": "langchain-ai/langchain", "stars": 3200, "topic": "rag"},
        ],
    }

    return ArtifactBundle(
        run_id=run_id,
        mission_id=mission.mission_id,
        worker_id=job.worker_id,
        job_id=job.job_id,
        target_ref=job.target,
        files=files,
        metrics={"repos_count": 5, "avg_stars": 1240},
        summary=summary,
    )


def _field_check(bundle: ArtifactBundle, field: str) -> bool:
    if field == "repos":
        return bundle.summary.get("repos_count", 0) > 0
    return field in bundle.summary


def validate_bundle(bundle: ArtifactBundle, mission: MissionSpec) -> ValidationReport:
    """Validate GitHub trending bundle."""
    return run_validation(
        bundle,
        mission,
        field_check=_field_check,
        missing_message=lambda field: f"Required field `{field}` is missing from GitHub trending bundle.",
        default_required_fields=["repos"],
        fail_check=lambda _bundle, passed, issues: passed == 0 and bool(issues),
    )
