"""Contract tests for job-level permissions in the reusable workflow (F2iLLC/vigil#116).

``dismiss`` and ``resolve-addressed`` are capped at job level to least
privilege. ``review`` is deliberately NOT capped (it may need
``id-token: write`` for Anthropic workload identity federation; capping it is
an owner decision, see #111/#116), and the workflow has no top-level
``permissions`` so callers can still opt into ``id-token``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "reusable-vigil.yml"
EXPECTED = {"contents": "read", "pull-requests": "write", "issues": "write"}
CAPPED_JOBS = ("dismiss", "resolve-addressed")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("job", CAPPED_JOBS)
def test_capped_job_has_exact_permissions(job: str) -> None:
    assert _workflow()["jobs"][job].get("permissions") == EXPECTED


@pytest.mark.parametrize("job", CAPPED_JOBS)
def test_capped_job_has_no_id_token_or_contents_write(job: str) -> None:
    perms = _workflow()["jobs"][job].get("permissions") or {}
    assert "id-token" not in perms
    assert perms.get("contents") != "write"


def test_review_job_is_not_capped_and_no_top_level_permissions() -> None:
    wf = _workflow()
    assert "permissions" not in wf["jobs"]["review"]
    assert "permissions" not in wf
