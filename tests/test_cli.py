import pytest
import typer

from vigil import cli
from vigil.finding_validation import SuppressedFinding
from vigil.models import Finding, ReviewResult, Severity


def _observation(**overrides) -> Finding:
    fields = {
        "file": "src/app.py",
        "line": 10,
        "severity": Severity.medium,
        "category": "style",
        "message": "medium finding",
    }
    fields.update(overrides)
    return Finding(**fields)


def _review_result(observations: list[Finding], commit_sha: str = "abc1234") -> ReviewResult:
    return ReviewResult(
        decision="APPROVE",
        summary="ok",
        commit_sha=commit_sha,
        specialist_verdicts=[],
        lead_findings=[],
        observations=observations,
    )


class TestValidateObservationsAgainstHead:
    """cli._validate_observations_against_head (#106).

    ``create_issues_for_observations`` runs before ``post_review``, and
    ``post_review``'s own #74 head-content check only ever looks at
    ``specialist_verdicts[].findings`` / ``lead_findings`` — never
    ``result.observations``. A P2/P3 finding routed there by the P-scale
    (#102) could therefore be filed as a standing issue with nothing having
    checked it against the reviewed commit at all.
    """

    def test_stale_observation_is_dropped_and_result_mutated(self, monkeypatch):
        live = _observation(message="live")
        stale = _observation(message="stale")
        result = _review_result([live, stale])

        def fake_validate(findings, owner, repo, head_sha, token, diff_files=None):
            assert findings == [live, stale]
            assert (owner, repo, head_sha, token) == ("F2iLLC", "demo", "abc1234", "tok")
            return [live], [SuppressedFinding(stale, "suggested_fix_already_present", "evidence")]

        monkeypatch.setattr(cli, "validate_findings_against_head", fake_validate)

        suppressed = cli._validate_observations_against_head(
            result, "F2iLLC", "demo", "tok", "diff text",
        )

        assert result.observations == [live]
        assert [s.finding for s in suppressed] == [stale]

    def test_no_observations_never_calls_validation(self, monkeypatch):
        result = _review_result([])
        calls = []
        monkeypatch.setattr(
            cli, "validate_findings_against_head",
            lambda *a, **k: calls.append(1) or ([], []),
        )

        suppressed = cli._validate_observations_against_head(
            result, "F2iLLC", "demo", "tok", "diff text",
        )

        assert suppressed == []
        assert calls == []

    def test_no_commit_sha_never_calls_validation(self, monkeypatch):
        result = _review_result([_observation()], commit_sha="")
        calls = []
        monkeypatch.setattr(
            cli, "validate_findings_against_head",
            lambda *a, **k: calls.append(1) or ([], []),
        )

        suppressed = cli._validate_observations_against_head(
            result, "F2iLLC", "demo", "tok", "diff text",
        )

        assert suppressed == []
        assert calls == []

    def test_nothing_suppressed_leaves_observations_untouched(self, monkeypatch):
        live = _observation()
        result = _review_result([live])
        monkeypatch.setattr(
            cli, "validate_findings_against_head",
            lambda *a, **k: ([live], []),
        )

        suppressed = cli._validate_observations_against_head(
            result, "F2iLLC", "demo", "tok", "diff text",
        )

        assert suppressed == []
        assert result.observations == [live]


def test_resolve_addressed_checks_dismissed_threads_before_unchanged_head_skip(monkeypatch):
    calls: list[str] = []

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(cli, "parse_pr_url", lambda pr_url: ("F2iLLC", "demo", 1))
    monkeypatch.setattr(
        cli,
        "get_pr_data",
        lambda *args: {"head_sha": "same-sha", "diff": ""},
    )
    monkeypatch.setattr(cli, "get_last_reviewed_sha", lambda *args: "same-sha")

    def fake_resolve_dismissed_threads(*args):
        calls.append("dismissed")
        return 1

    def fail_if_compared(*args):
        raise AssertionError("unchanged heads should not compare commits")

    monkeypatch.setattr(cli, "resolve_dismissed_threads", fake_resolve_dismissed_threads)
    monkeypatch.setattr(cli, "get_changed_files_between_commits", fail_if_compared)

    with pytest.raises(typer.Exit) as exc:
        cli.resolve_addressed("https://github.com/F2iLLC/demo/pull/1")

    assert exc.value.exit_code == 0
    assert calls == ["dismissed"]
