"""Cross-specialist deduplication of the OBSERVATION path (F2iLLC/vigil#96).

Vigil merged duplicate *findings* across specialists (reviewer Step 3.5) but
aggregated *observations* verbatim. Observations are what Vigil auto-files
GitHub issues from, so one defect that six specialists each mentioned became
six near-identical issues while the review body claimed a zero-duplication rule
had been applied.

These tests pin the intended behaviour of the fix: same defect collapses to one,
genuinely different defects both survive, the survivor is the most severe of the
group, and every contributing specialist stays recoverable.
"""

import json
from unittest.mock import MagicMock, patch

from vigil.context_manager import stable_finding_key
from vigil.cross_specialist_dedup import (
    consensus_persona,
    merge_specialist_findings,
    merge_specialist_observations,
)
from vigil.issue_manager import create_issues_for_observations
from vigil.models import Finding, PersonaVerdict, ReviewResult, Severity
from vigil.personas import Persona, ReviewProfile
from vigil.reviewer import review_diff


# The six specialists that filed relara#1032/1033/1034/1036/1037/1038 for one
# defect. Their names are the only thing that differed in the reported bug.
SPECIALISTS = ["Security", "Logic", "Performance", "Testing", "Docs", "Architecture"]

# One defect, six voices. Same component + same predicate => same
# stable_finding_key, which is exactly the grouping the findings path uses.
COMPONENT = "src/vigil"
PREDICATE = "observation path skips cross specialist merge"

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def handler():
+    return compute()
     pass
"""


def _obs(
    message,
    persona_hint="",
    sev=Severity.medium,
    component=COMPONENT,
    predicate=PREDICATE,
    file="src/vigil/reviewer.py",
):
    return Finding(
        file=file,
        line=715,
        severity=sev,
        category="duplication",
        message=message,
        suggestion=f"Merge observations across specialists. {persona_hint}".strip(),
        component=component,
        predicate=predicate,
    )


def _verdict(persona, observations, decision="APPROVE"):
    return PersonaVerdict(
        persona=persona,
        session_id=f"VGL-{abs(hash(persona)) % 1000000:06d}",
        decision=decision,
        checks={},
        findings=[],
        observations=list(observations),
    )


def _six_verdicts(severities=None):
    """Six specialists, each phrasing the SAME defect differently."""
    severities = severities or [Severity.medium] * 6
    return [
        _verdict(
            name,
            [
                _obs(
                    f"{name} notes the observation path is never deduplicated "
                    f"across specialists, unlike findings.",
                    persona_hint=name,
                    sev=sev,
                )
            ],
        )
        for name, sev in zip(SPECIALISTS, severities)
    ]


# ---------- the premise the fix rests on ----------

class TestGroupingPremise:

    def test_six_paraphrases_share_one_stable_key(self):
        """Same component + predicate is one identity even with six wordings."""
        keys = {
            stable_finding_key(v.observations[0]) for v in _six_verdicts()
        }
        assert len(keys) == 1

    def test_bare_paraphrase_without_a_predicate_does_not_share_a_key(self):
        """Why the issue_manager in-run guard alone could not catch this.

        With no structured predicate the key falls back to the message's
        content words, so six sentences produce six keys — which is precisely
        how six issues got filed for one defect.
        """
        keys = {
            stable_finding_key(
                _obs(f"{name} says the observations are duplicated", predicate="")
            )
            for name in SPECIALISTS
        }
        assert len(keys) > 1


# ---------- merge_specialist_observations ----------

class TestMergeSpecialistObservations:

    def test_six_specialists_one_defect_merges_to_one(self):
        deduped, merged = merge_specialist_observations(_six_verdicts())
        assert len(deduped) == 1
        assert len(merged) == 1
        assert merged[0].count == 6

    def test_different_defects_are_not_collapsed(self):
        """Over-collapsing would be a worse bug than the one being fixed."""
        v1 = _verdict("Security", [_obs("Token is logged", predicate="token logged")])
        v2 = _verdict(
            "Performance",
            [_obs("N+1 query in the loop", predicate="n plus one query")],
        )
        deduped, merged = merge_specialist_observations([v1, v2])
        assert len(deduped) == 2
        assert merged == []

    def test_different_components_are_not_collapsed(self):
        """Same predicate in two unrelated components stays two observations."""
        v1 = _verdict("Security", [_obs("Unvalidated input", component="src/api")])
        v2 = _verdict("Logic", [_obs("Unvalidated input", component="src/worker")])
        deduped, _ = merge_specialist_observations([v1, v2])
        assert len(deduped) == 2

    def test_representative_is_the_highest_severity(self):
        severities = [
            Severity.low,
            Severity.medium,
            Severity.critical,
            Severity.high,
            Severity.low,
            Severity.medium,
        ]
        deduped, merged = merge_specialist_observations(_six_verdicts(severities))
        assert len(deduped) == 1
        assert deduped[0].severity is Severity.critical
        assert merged[0].finding.severity is Severity.critical
        # The critical one was Performance's (index 2).
        assert "Performance" in deduped[0].message

    def test_severity_tie_keeps_the_first_encountered(self):
        """Deterministic and stable under input order."""
        deduped, _ = merge_specialist_observations(_six_verdicts())
        assert deduped[0].message.startswith("Security")

    def test_all_contributing_specialists_are_recoverable(self):
        _, merged = merge_specialist_observations(_six_verdicts())
        assert merged[0].specialists == SPECIALISTS
        assert len(merged[0].original_findings) == 6
        assert {info.specialist for info in merged[0].verdict_info} == set(SPECIALISTS)

    def test_single_specialist_observation_is_untouched(self):
        v = _verdict("Security", [_obs("Only one voice")])
        deduped, merged = merge_specialist_observations([v])
        assert deduped == [v.observations[0]]
        assert merged == []

    def test_no_observations_returns_empty(self):
        assert merge_specialist_observations([_verdict("Security", [])]) == ([], [])

    def test_findings_path_is_left_alone(self):
        """merge_specialist_findings must not see observations."""
        v = _verdict("Security", [_obs("An observation")])
        assert merge_specialist_findings([v]) == ([], [])


# ---------- consensus_persona ----------

class TestConsensusPersona:

    def test_names_every_contributor(self):
        rendered = consensus_persona(SPECIALISTS)
        for name in SPECIALISTS:
            assert name in rendered

    def test_single_name_is_rendered_as_itself(self):
        assert consensus_persona(["Security"]) == "Security"

    def test_empty_falls_back_to_vigil(self):
        assert consensus_persona([]) == "Vigil"

    def test_bounded_so_an_issue_title_cannot_overrun(self):
        many = [f"Specialist{i:02d}" for i in range(40)]
        rendered = consensus_persona(many)
        assert len(rendered) <= 120
        assert rendered.startswith("Specialist00")
        assert "39 others" in rendered

    def test_survives_specialist_name_validation(self):
        """The separator degrades to a space, never to nothing."""
        from vigil.utils import validate_specialist_name

        validated = validate_specialist_name(consensus_persona(["Security", "Logic"]))
        assert "Security" in validated
        assert "Logic" in validated


# ---------- reviewer wiring (the reported regression) ----------

def _llm_response(payload: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return resp


def _specialist_response(name: str, predicate: str = PREDICATE, component: str = COMPONENT):
    return _llm_response({
        "decision": "APPROVE",
        "checks": {"scan": "PASS"},
        "findings": [],
        "observations": [{
            "file": "src/vigil/reviewer.py",
            "line": 715,
            "severity": "medium",
            "category": "duplication",
            "message": (
                f"{name} notes the observation path is never deduplicated "
                f"across specialists, unlike findings."
            ),
            "suggestion": f"Merge observations across specialists. {name}",
            "component": component,
            "predicate": predicate,
        }],
    })


def _lead_response():
    return _llm_response({"decision": "APPROVE", "summary": "Looks good", "findings": []})


def _pr_context():
    return {
        "title": "Test PR", "author": "user", "head": "feature", "base": "main",
        "additions": 1, "deletions": 0, "changed_files": 1, "body": "",
    }


def _profile(names):
    return ReviewProfile(
        name="test",
        specialists=[
            Persona(name=n, focus="f", system_prompt="p", file_patterns=["*.py"])
            for n in names
        ],
        lead_prompt="You are the lead.",
    )


class TestReviewerAggregatesDedupedObservations:

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_six_specialists_one_defect_yields_one_observation(self, mock_llm, mock_alerts):
        """The reported bug: six specialists, one defect, six issues filed."""
        mock_alerts.return_value = 0
        mock_llm.side_effect = [
            _specialist_response(name) for name in SPECIALISTS
        ] + [_lead_response()]

        result = review_diff(DIFF, _pr_context(), _profile(SPECIALISTS))

        assert len(result.observations) == 1, (
            "one defect described six ways must aggregate to one observation"
        )
        assert len(result.observation_sources) == 1
        # The persona_map in issue_manager is keyed on id(obs) — the surviving
        # representative must be the object recorded in observation_sources.
        source_persona, source_obs = result.observation_sources[0]
        assert source_obs is result.observations[0]
        for name in SPECIALISTS:
            assert name in source_persona, "attribution must not be dropped"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_distinct_observations_both_survive(self, mock_llm, mock_alerts):
        mock_alerts.return_value = 0
        mock_llm.side_effect = [
            _specialist_response("Security", predicate="token logged"),
            _specialist_response("Performance", predicate="n plus one query"),
            _lead_response(),
        ]

        result = review_diff(DIFF, _pr_context(), _profile(["Security", "Performance"]))

        assert len(result.observations) == 2
        assert len(result.observation_sources) == 2
        assert {p for p, _ in result.observation_sources} == {"Security", "Performance"}

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_unmerged_observation_keeps_its_own_persona(self, mock_llm, mock_alerts):
        """A lone observation is attributed to exactly the specialist that raised it."""
        mock_alerts.return_value = 0
        mock_llm.side_effect = [
            _specialist_response("Security"),
            _lead_response(),
        ]

        result = review_diff(DIFF, _pr_context(), _profile(["Security"]))

        assert result.observation_sources == [("Security", result.observations[0])]


# ---------- end to end at the issue-creation boundary ----------

class TestIssuesFiledForMergedObservations:
    """Mocks follow tests/test_issue_manager.py: patch create_issue,
    _fetch_all_issues and ensure_priority_label, never the HTTP client."""

    def _result_from(self, verdicts):
        deduped, merged = merge_specialist_observations(verdicts)
        by_id = {id(info.finding): info for info in merged}
        sources = [
            (
                consensus_persona(by_id[id(o)].specialists)
                if id(o) in by_id
                else next(v.persona for v in verdicts if o in v.observations),
                o,
            )
            for o in deduped
        ]
        return ReviewResult(
            decision="APPROVE",
            summary="All good",
            commit_sha="abc1234",
            pr_url="https://github.com/o/r/pull/1",
            model="test-model",
            specialist_verdicts=verdicts,
            lead_findings=[],
            observations=deduped,
            observation_sources=sources,
        )

    @patch("vigil.issue_manager.create_issue")
    @patch("vigil.issue_manager._fetch_all_issues")
    @patch("vigil.issue_manager.ensure_priority_label")
    def test_six_same_defect_observations_file_one_issue(
        self, mock_label, mock_fetch, mock_create
    ):
        mock_label.return_value = True
        mock_fetch.return_value = []
        mock_create.return_value = "https://github.com/o/r/issues/1"

        result = self._result_from(_six_verdicts())
        issues = create_issues_for_observations("o", "r", "token", result)

        assert mock_create.call_count == 1, (
            "one defect must file one issue, not one per specialist"
        )
        assert len(issues) == 1

        # The single filed issue names every specialist that raised it.
        persona = mock_create.call_args[0][4]
        for name in SPECIALISTS:
            assert name in persona

    @patch("vigil.issue_manager.create_issue")
    @patch("vigil.issue_manager._fetch_all_issues")
    @patch("vigil.issue_manager.ensure_priority_label")
    def test_two_distinct_observations_still_file_two_issues(
        self, mock_label, mock_fetch, mock_create
    ):
        mock_label.return_value = True
        mock_fetch.return_value = []
        mock_create.side_effect = [
            "https://github.com/o/r/issues/1",
            "https://github.com/o/r/issues/2",
        ]

        verdicts = [
            _verdict("Security", [_obs("Token is logged", predicate="token logged")]),
            _verdict(
                "Performance",
                [_obs("N+1 query in the loop", predicate="n plus one query")],
            ),
        ]
        result = self._result_from(verdicts)
        issues = create_issues_for_observations("o", "r", "token", result)

        assert mock_create.call_count == 2
        assert len(issues) == 2

    @patch("vigil.issue_manager.create_issue")
    @patch("vigil.issue_manager._fetch_all_issues")
    @patch("vigil.issue_manager.ensure_priority_label")
    def test_issue_body_names_the_contributing_specialists(
        self, mock_label, mock_fetch, mock_create
    ):
        """The body's Reviewer line and observation_sources agree by construction."""
        from vigil.issue_manager import _build_issue_body

        mock_label.return_value = True
        mock_fetch.return_value = []
        mock_create.return_value = "https://github.com/o/r/issues/1"

        result = self._result_from(_six_verdicts())
        create_issues_for_observations("o", "r", "token", result)

        persona = mock_create.call_args[0][4]
        body = _build_issue_body(result.observations[0], persona)
        assert f"**Reviewer:** {persona}" in body
        assert persona == result.observation_sources[0][0]
