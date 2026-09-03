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

import re
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


def find_job(job_id: str) -> dict:
    """Return the job named ``job_id``.

    The #89 assertions are about what a *job* can do to the run conclusion,
    not about one step, so they need the whole job rather than ``find_step``.
    """
    wf = load_workflow()
    try:
        return wf["jobs"][job_id]
    except KeyError:  # pragma: no cover - guards a rename, not a code path
        raise AssertionError(
            f"no job {job_id!r} in {WORKFLOW.name}; jobs are {sorted(wf['jobs'])}"
        ) from None


def job_script(job_id: str) -> str:
    """All ``run`` bodies of a job, concatenated."""
    return "\n".join(step.get("run", "") for step in find_job(job_id).get("steps", []))


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


# --------------------------------------------------------------------------
# trigger guards (issue #82, third recurrence)
#
# The workflow could not see the thing it exists to watch. Its triggers were
# `push: branches: [main]`, a daily cron and `workflow_dispatch`, and *moving
# the alias fires none of them* — moving `v1` is a tag push, not a push to
# `main`. So after every release the tracking issue kept asserting drift that
# had already been cleared, for up to a full cron period. Observed on
# 2026-09-01: the release moved `v1` to `dfba3d0` (== `origin/main`) at
# 03:28Z, the drift check's last run was 13:56Z the previous day, and issue
# #85 stayed open saying "1 commit behind" the whole time.
#
# A check that is wrong in the ALARMING direction is how a control becomes
# background noise — see the 26 ignored red runs this file already documents.
# These tests pin the two triggers that close the gap, and, just as
# importantly, pin the shape of the tag filter: a pattern that also matched
# semver would make the problem worse, not better.
# --------------------------------------------------------------------------
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
RELEASE_WORKFLOW_NAME = "Release (move major alias tag)"


def triggers(wf: dict) -> dict:
    """Return the workflow's trigger map, working around YAML 1.1.

    ``yaml.safe_load`` parses the bare key ``on:`` as the *boolean* ``True``
    (YAML 1.1 treats ``on``/``off``/``yes``/``no`` as booleans), so
    ``wf["on"]`` raises ``KeyError`` and any test that reaches for it with a
    ``.get(...)`` default silently passes by inspecting a key that does not
    exist. Look under both, and let the caller assert it found something.
    """
    return wf.get("on", wf.get(True))


class UnsupportedFilterPattern(Exception):
    """Raised for a filter pattern whose meaning is not unambiguous.

    This is not a limitation to work around — it is the point. See
    ``test_the_translator_refuses_the_ambiguous_quantified_class`` below.
    """


def filter_pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a GitHub Actions filter pattern into an anchored regex.

    Filter patterns are NOT regexes, and the difference is exactly what this
    fix had to get right. Per GitHub's filter-pattern cheat sheet:

      ``*``   Matches zero or more characters, but does not match ``/``.
              (``Octo*`` matches ``Octocat``)
      ``**``  Matches zero or more of any character.
      ``?``   Matches zero or one of the preceding character.
      ``+``   Matches one or more of the preceding character.
      ``[]``  Matches one alphanumeric character listed in the brackets or
              included in ranges; ranges may only use ``a-z``, ``A-Z``, ``0-9``.
              (``[CB]at`` matches ``Cat`` or ``Bat``; ``[1-2]00`` matches
              ``100`` and ``200``)

    Two things follow that matter here. First, a bracket expression matches
    exactly ONE character, so a pattern built only from literals and bracket
    classes has a fixed width and cannot match a longer string — which is how
    the tag filter is kept away from semver. Second, ``+`` and ``?`` are
    documented as quantifying "the preceding CHARACTER", and a bracket range
    is not a character; this translator therefore refuses to guess what
    ``[0-9]+`` means rather than encoding one reading of it as if it were
    settled fact.

    Filter patterns are whole-string matches, so the result is anchored.
    """
    out = ["\\A"]
    # The regex fragment emitted for the most recent atom, or None when the
    # previous token cannot legally carry a quantifier.
    last_atom: str | None = None
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 1
            if i >= len(pattern):
                raise UnsupportedFilterPattern(f"trailing backslash in {pattern!r}")
            last_atom = re.escape(pattern[i])
            out.append(last_atom)
        elif ch == "*":
            if pattern.startswith("**", i):
                out.append(".*")
                i += 1
            else:
                out.append("[^/]*")
            last_atom = None
        elif ch in "?+":
            if last_atom is None:
                raise UnsupportedFilterPattern(
                    f"{ch!r} in {pattern!r} has no unambiguous preceding character; "
                    "GitHub documents it as quantifying 'the preceding character', "
                    "and a class or wildcard is not a character"
                )
            quant = "?" if ch == "?" else "+"
            out[-1] = f"(?:{out[-1]}){quant}"
            last_atom = None
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                raise UnsupportedFilterPattern(f"unterminated '[' in {pattern!r}")
            body = pattern[i + 1 : end]
            if not re.fullmatch(r"(?:[A-Za-z0-9]-[A-Za-z0-9]|[A-Za-z0-9])+", body):
                raise UnsupportedFilterPattern(
                    f"bracket body {body!r} in {pattern!r} is not alphanumeric; "
                    "ranges may only use a-z, A-Z and 0-9"
                )
            out.append(f"[{body}]")
            # A bracket class is deliberately NOT a quantifiable atom here.
            last_atom = None
            i = end
        elif ch == "!":
            raise UnsupportedFilterPattern(
                f"negation is not modelled by this helper: {pattern!r}"
            )
        else:
            last_atom = re.escape(ch)
            out.append(last_atom)
        i += 1
    out.append("\\Z")
    return re.compile("".join(out))


def workflow_names_by_file() -> dict[Path, str]:
    """Every workflow in ``.github/workflows`` mapped to its declared ``name``."""
    names: dict[Path, str] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            names[path] = doc["name"]
    return names


def test_the_translator_matches_githubs_own_documented_examples() -> None:
    """Sanity-check the helper against the cheat sheet's worked examples.

    The tests below are only as trustworthy as this translation, so pin it to
    the examples GitHub itself publishes rather than to our reading of them.
    """
    assert filter_pattern_to_regex("Octo*").fullmatch("Octocat")
    assert filter_pattern_to_regex("[CB]at").fullmatch("Cat")
    assert filter_pattern_to_regex("[CB]at").fullmatch("Bat")
    assert not filter_pattern_to_regex("[CB]at").fullmatch("Hat")
    assert filter_pattern_to_regex("[1-2]00").fullmatch("100")
    assert filter_pattern_to_regex("[1-2]00").fullmatch("200")
    assert not filter_pattern_to_regex("[1-2]00").fullmatch("300")
    # `*` does not cross a path separator; `**` does.
    assert not filter_pattern_to_regex("releases/*").fullmatch("releases/1/2")
    assert filter_pattern_to_regex("releases/**").fullmatch("releases/1/2")


def test_the_translator_refuses_the_ambiguous_quantified_class() -> None:
    """`v[0-9]+` is the pattern this fix deliberately did NOT use.

    It is the obvious thing to write and it reads like a regex, but GitHub
    documents `+` as matching "one or more of the preceding CHARACTER", and a
    bracket range is not a character. On the literal reading `v[0-9]+` means
    `v`, one digit, then a literal `+` sign — which matches no tag this repo
    will ever push. Betting a monitoring control on which reading the runner
    implements is precisely the class of mistake issue #82 is about, so the
    translator refuses it and the workflow spells the widths out instead.
    """
    with pytest.raises(UnsupportedFilterPattern):
        filter_pattern_to_regex("v[0-9]+")


def test_the_drift_check_runs_when_the_alias_tag_is_pushed() -> None:
    """A hand-pushed alias move must re-run the check immediately.

    Pre-fix, the only `push` filter was `branches: [main]`. Moving `v1` is a
    tag push, so the workflow did not run, and the tracking issue went on
    claiming drift until the next daily cron. The 2026-08-30 and 2026-08-31
    alias moves were both made this way.
    """
    on = triggers(load_workflow())
    assert on is not None, (
        "no trigger map found — note that `yaml.safe_load` parses the key "
        "`on:` as the boolean True, not the string 'on'"
    )
    push = on.get("push")
    assert isinstance(push, dict) and "tags" in push, (
        "the drift check does not trigger on any tag push, so moving the `v1` "
        "alias — the only event that can clear the drift — does not re-run it; "
        f"push filter is {push!r}"
    )
    patterns = push["tags"]
    assert patterns, "the `tags` filter is present but empty"

    matchers = [filter_pattern_to_regex(p) for p in patterns]

    def matches(tag: str) -> bool:
        return any(m.fullmatch(tag) for m in matchers)

    for alias in ("v1", "v2", "v10"):
        assert matches(alias), (
            f"the alias tag {alias!r} does not match any of {patterns!r}, so a "
            "release that moves it would not re-run this check"
        )


def test_the_tag_filter_cannot_match_a_semver_tag() -> None:
    """Firing on the semver push would make the drift report WORSE.

    `release.yml` triggers on `v[0-9]+.[0-9]+.[0-9]+`. If this workflow shared
    that trigger it would start *before* the release job had moved the alias,
    report the drift that release is about to clear, refresh the tracking issue
    with it — and then never re-run once the alias actually landed, because
    nothing else fires. A stale alarm the release itself just re-armed is
    strictly worse than the daily cron.
    """
    push = triggers(load_workflow())["push"]
    matchers = [filter_pattern_to_regex(p) for p in push["tags"]]

    for semver in ("v1.2.0", "v1.1.0", "v1.0.0", "v2.0.0", "v10.20.30", "v0.0.1"):
        offenders = [
            p for p, m in zip(push["tags"], matchers) if m.fullmatch(semver)
        ]
        assert not offenders, (
            f"pattern(s) {offenders!r} match the semver tag {semver!r}; the drift "
            "check would then run on the release tag push, before the alias has "
            "moved, and leave a false 'behind' report nothing re-runs to clear"
        )


def test_the_two_tag_filters_partition_the_release_events() -> None:
    """Cross-check against `release.yml` rather than against our own reading.

    The two workflows must respond to disjoint tag pushes: the alias move is
    this file's business, the semver cut is `release.yml`'s. Pinning the
    property against the *other* file's live filter catches a future edit to
    either one that lets them overlap.

    `release.yml` uses `v[0-9]+.[0-9]+.[0-9]+`, which the translator above
    refuses (see its docstring). It does not need to be translated: the
    separating property is structural. Every release pattern contains a
    literal `.` that a matching tag must supply, and every drift pattern is a
    fixed-width run of literals and single-character classes with no `.` in
    it. A fixed-width dotless pattern cannot match a string containing a dot,
    and a pattern requiring a dot cannot match an alias tag, which has none.
    """
    drift_patterns = triggers(load_workflow())["push"]["tags"]
    release_wf = yaml.safe_load(
        (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")
    )
    release_patterns = triggers(release_wf)["push"]["tags"]

    assert all("." in p for p in release_patterns), (
        "release.yml's tag filter no longer requires a literal dot, so it may "
        f"now also fire on an alias push: {release_patterns!r}"
    )
    assert not any("." in p or "*" in p for p in drift_patterns), (
        "the drift check's tag filter gained a `.` or a `*`, either of which "
        f"can reach a semver tag: {drift_patterns!r}"
    )
    for alias in ("v1", "v2", "v10"):
        assert "." not in alias  # the premise, stated so it cannot rot



def test_the_drift_check_runs_when_the_release_workflow_completes() -> None:
    """The automated release path is invisible to the tag trigger.

    `release.yml`'s `publish` job force-pushes `refs/tags/v1` using the default
    `GITHUB_TOKEN`, and GitHub deliberately does not start new workflow runs
    from events created with that token. The 2026-09-01T03:28Z release proves
    it: the alias moved and *no* run of any workflow was created by that tag
    push. So the tag filter alone leaves the automated path uncovered, and
    `workflow_run` is the only trigger that closes it.
    """
    on = triggers(load_workflow())
    wr = on.get("workflow_run")
    assert isinstance(wr, dict), (
        "no `workflow_run` trigger: a release performed by `release.yml` moves "
        "the alias with GITHUB_TOKEN, which starts no workflow runs, so nothing "
        "would re-run this check until the next daily cron"
    )
    assert RELEASE_WORKFLOW_NAME in (wr.get("workflows") or []), (
        f"the `workflow_run` trigger must name {RELEASE_WORKFLOW_NAME!r}; got "
        f"{wr.get('workflows')!r}"
    )
    assert "completed" in (wr.get("types") or []), (
        "the trigger must fire on `completed`; a release that fails after "
        f"`publish` has pushed the tag has still moved the alias. Got {wr.get('types')!r}"
    )


def test_the_workflow_run_trigger_names_a_workflow_that_exists() -> None:
    """`workflow_run` matches on the workflow's NAME, not its filename.

    That makes it silently breakable in two directions: rename the release
    workflow's `name:` and this trigger stops firing, or move the file and the
    same. Nothing fails loudly at the time — the drift check simply reverts to
    once-a-day and the tracking issue goes stale again, which is the exact
    defect being fixed. So resolve the name against every workflow file's
    declared `name:` rather than hardcoding a path.
    """
    named = workflow_names_by_file()
    matches = [path for path, name in named.items() if name == RELEASE_WORKFLOW_NAME]
    assert matches, (
        f"the drift check's `workflow_run` trigger names {RELEASE_WORKFLOW_NAME!r}, "
        "but no workflow in .github/workflows declares that name — the trigger "
        f"can never fire. Declared names: {sorted(named.values())!r}"
    )
    assert len(matches) == 1, (
        f"{RELEASE_WORKFLOW_NAME!r} is declared by more than one workflow "
        f"({[p.name for p in matches]!r}); `workflow_run` would be ambiguous"
    )
    # And it is still the workflow that actually moves the alias, not some
    # other file that happened to adopt the name.
    assert "git push --force origin" in matches[0].read_text(encoding="utf-8"), (
        f"{matches[0].name} carries the release workflow's name but no longer "
        "pushes a tag; the drift check would be reacting to the wrong workflow"
    )


def test_the_original_triggers_are_preserved() -> None:
    """The new triggers are additive. Each old one still covers a real case.

    `push: branches: [main]` catches the merge that *creates* the drift (so it
    is reported within a minute of landing, not the next morning); the cron is
    the backstop for an alias moved by some path neither new trigger sees, and
    re-states a drift left unaddressed for a day; `workflow_dispatch` is how a
    maintainer confirms the fleet by hand. Dropping any of them while adding
    the new ones would trade one blind spot for another.
    """
    on = triggers(load_workflow())
    assert on is not None

    assert "main" in (on["push"].get("branches") or []), (
        f"push-to-main trigger lost; got {on['push'].get('branches')!r}"
    )
    assert on.get("schedule"), "the daily cron backstop was removed"
    assert any("cron" in entry for entry in on["schedule"]), (
        f"the schedule entry declares no cron: {on['schedule']!r}"
    )
    # `workflow_dispatch:` with no inputs parses as None, so test membership,
    # not truthiness.
    assert "workflow_dispatch" in on, "manual dispatch was removed"


def test_the_alias_job_stays_report_only() -> None:
    """Regression guard: this job must never be able to move a tag.

    It now runs on tag pushes and on the completion of the release workflow,
    which puts it one careless edit away from looking like a natural place to
    "just move the alias while we're here". It is not. The release is a
    fleet-wide, human-gated act that belongs to `release.yml` and its `release`
    environment; a reporter that can also ship is no longer a reporter. The
    permissions are the enforcement, so assert them exactly.
    """
    wf = load_workflow()
    alias_job = next(
        job
        for job in wf["jobs"].values()
        if any(s.get("id") == "alias" for s in job.get("steps", []))
    )
    assert alias_job.get("permissions") == {
        "contents": "read",
        "issues": "write",
    }, (
        "the alias job's permissions must stay exactly `contents: read` + "
        "`issues: write`. `contents: write` would make this report-only check "
        f"capable of performing a release. Got {alias_job.get('permissions')!r}"
    )
    body = "\n".join(step.get("run", "") for step in alias_job["steps"])
    assert "git push" not in body, (
        "the drift check must never push anything — it reports on the release, "
        "it does not perform one"
    )
# --------------------------------------------------------------------------
# #89: the run conclusion, and the over-correction guard
# --------------------------------------------------------------------------
# These four are deliberately static (YAML/text) rather than shell-driven.
# They assert a property of the *workflow definition* — "which surface is
# allowed to fail the run" — which is not observable from executing one step
# body, and which must stay checkable on any box.
def test_pins_job_cannot_fail_the_run() -> None:
    """The self-pin job must not be able to turn the run red (#89).

    GitHub rolls a run's conclusion up from its jobs, so splitting the two
    surfaces into separate jobs (defect 2) fixed the job signal but left the
    run signal saturated. The run conclusion was `failure` on 12 of 12 runs.
    Only 3 of those postdate the split: two were genuinely red because `v1`
    really was behind (#82), and one — run 33503843618 — had `v1 alias vs
    main` green and was still reported `failure`. So since the split, every
    run in which the fleet was actually healthy (1 of 1) was still reported
    red.

    And as wired it could not have done otherwise. ``tagctl.sh``'s ``cmd_pins``
    decides "current" by exact equality against ``origin/main``'s head, while
    writing a new pin SHA into a workflow file is *itself* a commit on main —
    so a pin expressed as a SHA is one behind the head its own bump created.
    All three real self-pins are SHAs, so the gate cannot pass. A gate that
    cannot pass is broken, not policy. Hence: report only.

    The claim is scoped deliberately. ``cmd_pins`` is *not* broken: a pin
    written as a moving ref is reported current when that ref is at main —
    see ``test_pins_reports_all_current_when_the_pin_is_at_head`` in
    ``tests/test_release_tooling.py``. Re-expressing the self-pins as ``@v1``
    is one of the options #88 is deciding, and report-only is the interim
    wiring that removes the false red without pre-empting that ruling.
    """
    script = job_script("pins")
    assert "exit 1" not in script, (
        "a step in the pins job still exits non-zero, which fails the job and "
        "therefore rolls the whole run up to `failure` — the by-design-red "
        "state that made the run conclusion unreadable, red on 12 of 12 runs "
        "including the one post-split run where the fleet was healthy"
    )
    assert "::error::" not in script, (
        "the pins job still raises an ::error:: annotation; this surface is "
        "expected to be stale between bumps, so it reports with ::warning::"
    )


def test_alias_job_still_fails_on_drift() -> None:
    """The over-correction guard for the test above.

    Making the pins surface report-only is only correct because something else
    still gates. Real `v1` drift means every consumer pinning
    ``F2iLLC/vigil@v1`` is running without merged fixes — that must still turn
    the run red. If a later change quietens the alias job too, the workflow
    goes permanently green and reproduces the original silence of #58/#82
    from the opposite direction.
    """
    steps = find_job("alias").get("steps", [])
    failing = [
        step
        for step in steps
        if "exit 1" in step.get("run", "")
        and "steps.alias.outputs.rc" in str(step.get("if", ""))
    ]
    assert failing, (
        "the alias job no longer has a step that fails on drift; the workflow "
        "can now never go red, so merged-but-unshipped fixes are silent again"
    )


def test_pins_step_still_reports_everything() -> None:
    """Report-only must mean *report*, not go quiet.

    The fix for #89 removes the failure, not the information. The annotation
    is what shows up on the run page, and the table is the only place a
    maintainer learns *which* file holds *which* stale pin — dropping either
    would turn a noisy-but-informative surface into a silent one, which is
    the defect this whole file was created to remove.
    """
    body = find_step("pins")["run"]
    assert "::warning::" in body, (
        "the pins step must still raise an annotation, or a stale pin is "
        "invisible on the run page"
    )
    assert "$GITHUB_STEP_SUMMARY" in body, (
        "the pins step must still write a job summary"
    )
    assert "| file | pin | state |" in body, (
        "the per-file pin table must survive; without it the report names no "
        "file and no SHA and cannot be acted on"
    )

    report = find_step("report")
    assert "::warning::" in report["run"], (
        "the stale-pin step must still annotate the run"
    )
    assert "::error::" not in report["run"] and "exit 1" not in report["run"], (
        "the stale-pin step must not be able to fail the job"
    )


def test_pins_summary_declares_it_is_report_only() -> None:
    """A surface that cannot fail must say so, in the place people read.

    Otherwise the next maintainer sees a green job next to a table of stale
    pins, reads it as a bug, and "fixes" it by restoring ``exit 1`` — putting
    the run back to permanently red. The summary therefore states the intent,
    the mechanism (a bump creates the very head it is then behind), and which
    surface actually gates, and points at #88/#89 for the ruling and the
    reasoning.
    """
    body = find_step("pins")["run"]
    assert "report-only" in body, (
        "the job summary must declare this surface report-only, or the next "
        "reader restores the hard failure"
    )
    assert "unsatisfiable by construction" in body, (
        "the summary must say *why* it cannot fail, not just that it does not"
    )
    assert "#89" in body and "#88" in body, (
        "the summary must point at the issue that made this call (#89) and "
        "the stale pin still awaiting a ruling (#88)"
    )
    assert "v1 alias vs main" in body, (
        "the summary must name the surface that does gate, so a green pins "
        "job is not mistaken for 'nothing is checking anything'"
    )

