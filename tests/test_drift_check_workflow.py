"""Tests for the ``Tag drift check`` workflow (issues #58, #82).

WHY THIS FILE EXISTS
--------------------
``.github/workflows/tag-drift-check.yml`` was added to remove the *silence*
around release-pin drift: `v1` is what every consumer pins, so a merge to
`main` ships nothing until the alias moves, and the gap had already been
found four times by a human reading SHAs by hand.

The workflow did not do that. Between 2026-08-12 and 2026-08-30 it ran 26
times and failed 26 times, and on the drifted runs it emitted no annotation,
no commit list and no job summary at all — only a bare ``Process completed
with exit code 3``. The cause is a shell-flag bug: GitHub's default ``run``
shell is ``bash --noprofile --norc -e -o pipefail {0}``, and a step body that
opens with ``set -uo pipefail`` does **not** clear the inherited ``-e``. So

    out="$(bash .github/scripts/tagctl.sh drift ...)"; rc=$?

aborts the whole step the instant ``tagctl.sh`` exits non-zero — which is
exactly and only the case the reporting code below it was written to handle.
Every drift branch in that workflow was unreachable.

These tests execute the workflow's own ``run`` bodies, under the same shell
GitHub would use, against a throwaway repository with real drift in it, and
assert that a report actually comes out. They fail against the pre-fix
workflow for the right reason.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tag-drift-check.yml"
TAGCTL = REPO_ROOT / ".github" / "scripts" / "tagctl.sh"

# GitHub Actions' default `run` shell on Linux. The `-e` is the whole bug:
# it is inherited by the step body and `set -uo pipefail` does not undo it.
DEFAULT_SHELL = "bash --noprofile --norc -e -o pipefail {0}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    )
    return out.stdout.decode().strip()


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def find_step(step_id: str) -> dict:
    """Return the step with ``id: <step_id>`` from any job in the workflow."""
    wf = load_workflow()
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if step.get("id") == step_id:
                return step
    raise AssertionError(f"no step with id={step_id!r} in {WORKFLOW.name}")


def run_step(step: dict, repo: Path, tmp_path: Path) -> dict:
    """Execute a workflow step's ``run`` body the way the runner would.

    Honours the step's ``shell:`` if it declares one, otherwise uses GitHub's
    default (which carries ``-e``). Returns stdout/stderr plus the contents of
    the two files the step writes its report into.
    """
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")

    summary = tmp_path / "summary.md"
    output = tmp_path / "output.txt"
    summary.write_text("", encoding="utf-8")
    output.write_text("", encoding="utf-8")

    template = step.get("shell") or DEFAULT_SHELL
    argv = [str(script) if part == "{0}" else part for part in shlex.split(template)]

    proc = subprocess.run(
        argv,
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
        },
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "summary": summary.read_text(encoding="utf-8"),
        "output": output.read_text(encoding="utf-8"),
    }


def commit(repo: Path, message: str, filename: str = "f.txt") -> str:
    (repo / filename).write_text(message + "\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def drifted_repo(tmp_path: Path) -> Path:
    """A repo where ``v1`` is two commits behind ``origin/main``.

    The alias is deliberately **annotated**, matching how `v1` has existed for
    most of this repo's life, so the peel is exercised rather than bypassed.
    """
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".github" / "scripts").mkdir(parents=True)
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / ".github" / "scripts" / "tagctl.sh").write_text(
        TAGCTL.read_text(encoding="utf-8"), encoding="utf-8"
    )

    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "test@example.com")

    first = commit(r, "one")
    git(r, "add", ".github")
    git(r, "commit", "-m", "tooling")
    git(r, "tag", "-a", "v1", "-m", "annotated release alias", first)

    commit(r, "two")
    commit(r, "three")
    head = git(r, "rev-parse", "HEAD")

    # The workflow compares against `origin/main`, which on a runner is a
    # remote-tracking ref rather than a branch. Create it directly so the
    # fixture needs no second repository.
    git(r, "update-ref", "refs/remotes/origin/main", head)
    return r


# --------------------------------------------------------------------------
# the regression this file exists for
# --------------------------------------------------------------------------
def test_alias_step_reports_drift_instead_of_dying_silently(
    drifted_repo: Path, tmp_path: Path
) -> None:
    """Real drift must produce a readable report, not a bare exit code.

    Against the pre-fix workflow this fails on the first assertion: the step
    dies at the command substitution, so the summary is empty, `rc` is never
    written, and a maintainer looking at the run learns nothing about which
    commits are stranded.
    """
    result = run_step(find_step("alias"), drifted_repo, tmp_path)

    assert result["summary"].strip(), (
        "the drift step wrote no job summary at all — this is the failure mode "
        "that made 26 consecutive red runs unreadable\n"
        f"stdout={result['stdout']!r} stderr={result['stderr']!r}"
    )
    assert "rc=3" in result["output"], (
        "the step must record the drift return code as a step output so the "
        f"job can act on it; got {result['output']!r}"
    )
    assert "::warning::" in result["stdout"], (
        "drift must raise an annotation, otherwise it is invisible on the run "
        f"page; got {result['stdout']!r}"
    )
    # The fixture tags `v1` at the first commit and then lands three more
    # (`tooling`, `two`, `three`), so the alias is exactly three behind.
    assert "3 commit(s) behind" in result["summary"], (
        f"the summary must state how far behind the alias is; got {result['summary']!r}"
    )
    # The whole point of the report: name the commits that are merged but not
    # shipped, so "closed defect still live in the fleet" is legible.
    assert "two" in result["summary"] and "three" in result["summary"], (
        f"the summary must list the unshipped commits; got {result['summary']!r}"
    )


def test_alias_step_reports_a_missing_alias(tmp_path: Path) -> None:
    """`v1` absent is a distinct, louder failure than `v1` behind."""
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".github" / "scripts").mkdir(parents=True)
    (r / ".github" / "scripts" / "tagctl.sh").write_text(
        TAGCTL.read_text(encoding="utf-8"), encoding="utf-8"
    )
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "test@example.com")
    commit(r, "one")
    git(r, "add", ".github")
    git(r, "commit", "-m", "tooling")
    git(r, "update-ref", "refs/remotes/origin/main", git(r, "rev-parse", "HEAD"))

    result = run_step(find_step("alias"), r, tmp_path)

    assert "rc=4" in result["output"], f"got {result['output']!r}"
    assert "::error::" in result["stdout"], f"got {result['stdout']!r}"
    assert "missing" in result["summary"].lower(), f"got {result['summary']!r}"


def test_alias_step_stays_quiet_when_in_sync(
    drifted_repo: Path, tmp_path: Path
) -> None:
    """No drift must report in-sync and succeed — the signal has to be able
    to go green, or it saturates and stops being read."""
    git(drifted_repo, "tag", "-f", "-a", "v1", "-m", "moved", "origin/main")

    result = run_step(find_step("alias"), drifted_repo, tmp_path)

    assert "rc=0" in result["output"], f"got {result['output']!r}"
    assert "::warning::" not in result["stdout"]
    assert "in sync" in result["summary"]


def test_pins_step_reports_stale_pins_instead_of_dying_silently(
    drifted_repo: Path, tmp_path: Path
) -> None:
    """Same shell bug, second surface. The self-pin table was never rendered
    on any of the 26 red runs for exactly this reason."""
    wf_dir = drifted_repo / ".github" / "workflows"
    stale = git(drifted_repo, "rev-parse", "HEAD~1")
    (wf_dir / "reusable.yml").write_text(
        f"jobs:\n  x:\n    steps:\n      - uses: F2iLLC/vigil@{stale}\n",
        encoding="utf-8",
    )
    git(drifted_repo, "add", ".github/workflows/reusable.yml")
    git(drifted_repo, "commit", "-m", "add a stale self-pin")
    git(drifted_repo, "update-ref", "refs/remotes/origin/main",
        git(drifted_repo, "rev-parse", "HEAD"))

    result = run_step(find_step("pins"), drifted_repo, tmp_path)

    assert "rc=3" in result["output"], (
        "the pins step must record its return code rather than aborting; got "
        f"{result['output']!r} stdout={result['stdout']!r}"
    )
    assert "::warning::" in result["stdout"], f"got {result['stdout']!r}"
    assert "reusable.yml" in result["summary"], (
        f"the stale pin must be named in the summary; got {result['summary']!r}"
    )


# --------------------------------------------------------------------------
# structural guards
# --------------------------------------------------------------------------
def test_reporting_steps_do_not_run_under_errexit() -> None:
    """Belt and braces for the bug above.

    Both steps hand-roll their own error handling around a command that is
    *expected* to exit non-zero. Inheriting ``-e`` makes that handling dead
    code, and the failure is silent, so pin the shell explicitly rather than
    relying on someone re-deriving this.
    """
    for step_id in ("alias", "pins"):
        step = find_step(step_id)
        shell = step.get("shell")
        assert shell is not None, (
            f"step {step_id!r} relies on the default shell, which carries -e; "
            "a non-zero tagctl.sh exit will abort it before it can report"
        )
        assert "-e" not in shlex.split(shell), (
            f"step {step_id!r} declares shell {shell!r}, which still enables "
            "errexit"
        )


def test_alias_and_pins_are_independent_jobs() -> None:
    """The two surfaces must not share a pass/fail.

    A self-pin goes stale the moment any commit lands after it is bumped, so
    that surface is red in the steady state. When one job covered both, the
    alias signal was permanently masked: the check had been red since the day
    it was created, and nobody could tell the 21-hour #79/#80 drift from the
    background noise. Keeping them separate lets the alias check be green when
    the fleet is actually current.
    """
    wf = load_workflow()
    jobs = wf["jobs"]

    def job_of(step_id: str) -> str:
        for name, job in jobs.items():
            if any(s.get("id") == step_id for s in job.get("steps", [])):
                return name
        raise AssertionError(f"no job owns step {step_id!r}")

    assert job_of("alias") != job_of("pins"), (
        "the v1 alias check and the self-pin check must be separate jobs, or a "
        "chronically stale pin permanently masks real release drift"
    )


def test_drift_opens_a_tracking_issue() -> None:
    """Issue #82's actual ask: the drift has to be visible in the tracker.

    Four recurrences, and every time the visible symptom was a *closed* defect
    still live in the fleet. A red scheduled workflow demonstrably does not
    carry that signal — 26 of them were ignored — so drift must also surface
    as an open issue that closes itself when the alias catches up.
    """
    wf = load_workflow()
    trackers = [
        job
        for job in wf["jobs"].values()
        if (job.get("permissions") or {}).get("issues") == "write"
    ]
    assert trackers, (
        "no job in the workflow can write to the issue tracker, so drift "
        "remains invisible where #82 says it needs to be visible"
    )
    body = "\n".join(
        step.get("run", "") for job in trackers for step in job.get("steps", [])
    )
    assert "gh issue create" in body and "gh issue close" in body, (
        "the tracking job must both open the issue on drift and close it once "
        "the alias catches up; a tracker that only opens becomes noise"
    )
