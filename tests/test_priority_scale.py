"""P-scale routing (owner ruling 2026-09-18).

Every finding carries a P0-P3 score derived from its severity. P0/P1
(critical/high) block the PR; P2/P3 (medium/low) are filed as prioritised
issues and are not addressed in the surfacing PR, so they must leave the
blocking list on every surface: specialist verdicts, the lead's own findings,
the aggregate decision, and the rendered comment/issue text.
"""

import json
from unittest.mock import MagicMock, patch

from vigil.github_review import _format_finding, _format_inline_comment
from vigil.issue_manager import _build_issue_body, priority_label_for
from vigil.models import DECISION_NOT_REVIEWED, Finding, PersonaVerdict, Severity
from vigil.personas import (
    _DEFAULT_LEAD_PROMPT,
    _ENTERPRISE_LEAD_PROMPT,
    Persona,
    ReviewProfile,
)
from vigil.reviewer import _route_findings_by_priority, review_diff


def _finding(severity: str, message: str = "msg", suggestion: str | None = None) -> Finding:
    return Finding(
        file="a.py", line=1, severity=Severity(severity), category="bug",
        message=message, suggestion=suggestion,
    )


def _verdict(decision: str, findings: list[Finding]) -> PersonaVerdict:
    return PersonaVerdict(
        persona="Logic", session_id="VGL-000000", decision=decision,
        checks={}, findings=findings, observations=[],
    )


# ---------- Severity -> P-scale ----------

class TestSeverityPriority:
    def test_mapping_is_the_fleet_mapping(self):
        assert Severity.critical.priority == "P0"
        assert Severity.high.priority == "P1"
        assert Severity.medium.priority == "P2"
        assert Severity.low.priority == "P3"

    def test_only_p0_p1_block(self):
        assert Severity.critical.blocks_review
        assert Severity.high.blocks_review
        assert not Severity.medium.blocks_review
        assert not Severity.low.blocks_review

    def test_finding_exposes_priority(self):
        assert _finding("medium").priority == "P2"

    def test_priority_label_follows_score(self):
        assert priority_label_for(_finding("critical")) == "Critical Priority"
        assert priority_label_for(_finding("high")) == "High Priority"
        assert priority_label_for(_finding("medium")) == "Medium Priority"
        assert priority_label_for(_finding("low")) == "Low Priority"


# ---------- Rendering ----------

class TestPriorityRendered:
    def test_review_body_finding_carries_score_and_keeps_severity_tag(self):
        text = _format_finding(_finding("high"), persona="Logic")
        assert "**P1**" in text
        # Existing parsers key on the severity tag; it must survive.
        assert "**[HIGH]**" in text

    def test_inline_comment_carries_score_in_text_and_metadata(self):
        text = _format_inline_comment(_finding("critical"), persona="Security", session_id="VGL-1")
        assert text.startswith("\U0001f534 **P0** **[CRITICAL]**")
        assert '"priority": "P0"' in text or '"priority":"P0"' in text

    def test_issue_body_heading_carries_score(self):
        body = _build_issue_body(_finding("low", suggestion="do x"), persona="Logic")
        assert "P3 · LOW — bug" in body


# ---------- Lead prompts get the rubric too ----------

class TestLeadPromptHasPriorityRubric:
    """Both built-in lead prompts define their own finding schema and never
    receive ``VERDICT_SCHEMA`` — they need the P-scale definitions spliced in
    separately, or a lead finding is scored against no rubric at all and can
    be mislabeled ``high``, turning a bounded P2 into a blocking finding.
    """

    def test_default_lead_prompt_has_the_rubric(self):
        assert "P0 —" in _DEFAULT_LEAD_PROMPT
        assert "P1 —" in _DEFAULT_LEAD_PROMPT
        assert "P2 —" in _DEFAULT_LEAD_PROMPT
        assert "P3 —" in _DEFAULT_LEAD_PROMPT
        assert "__PRIORITY_RUBRIC__" not in _DEFAULT_LEAD_PROMPT

    def test_enterprise_lead_prompt_has_the_rubric(self):
        assert "P0 —" in _ENTERPRISE_LEAD_PROMPT
        assert "P1 —" in _ENTERPRISE_LEAD_PROMPT
        assert "P2 —" in _ENTERPRISE_LEAD_PROMPT
        assert "P3 —" in _ENTERPRISE_LEAD_PROMPT
        assert "__PRIORITY_RUBRIC__" not in _ENTERPRISE_LEAD_PROMPT


# ---------- Specialist routing ----------

class TestRouteFindingsByPriority:
    def test_p2_p3_move_to_observations_and_verdict_approves(self):
        v = _verdict("REQUEST_CHANGES", [_finding("medium", "edge"), _finding("low", "polish")])
        filed = _route_findings_by_priority(v)
        assert [f.message for f in filed] == ["edge", "polish"]
        assert v.findings == []
        assert [f.message for f in v.observations] == ["edge", "polish"]
        assert v.decision == "APPROVE"

    def test_p0_p1_stay_blocking(self):
        v = _verdict("REQUEST_CHANGES", [_finding("high", "real"), _finding("medium", "edge")])
        filed = _route_findings_by_priority(v)
        assert [f.message for f in filed] == ["edge"]
        assert [f.message for f in v.findings] == ["real"]
        assert [f.message for f in v.observations] == ["edge"]
        assert v.decision == "REQUEST_CHANGES"

    def test_filed_findings_go_ahead_of_model_observations(self):
        v = _verdict("APPROVE", [_finding("low", "filed")])
        v.observations = [_finding("low", "obs", suggestion="do y")]
        _route_findings_by_priority(v)
        assert [f.message for f in v.observations] == ["filed", "obs"]

    def test_request_changes_with_no_findings_is_left_alone(self):
        # A blocking verdict that names nothing is not ours to soften here.
        v = _verdict("REQUEST_CHANGES", [])
        assert _route_findings_by_priority(v) == []
        assert v.decision == "REQUEST_CHANGES"


# ---------- End-to-end through review_diff ----------

class TestReviewDiffPriorityRouting:
    def _resp(self, payload: dict) -> MagicMock:
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
        return resp

    def _profile(self) -> ReviewProfile:
        persona = Persona(name="Logic", focus="Bugs", system_prompt="Test")
        return ReviewProfile(name="test", specialists=[persona], lead_prompt="Lead")

    _ctx = {
        "title": "Test", "author": "u", "head": "f", "base": "m",
        "additions": 1, "deletions": 0, "changed_files": 1, "body": "",
    }

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_specialist_p2_only_review_approves_and_files(self, mock_completion, mock_alerts):
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({
                "decision": "REQUEST_CHANGES", "checks": {},
                "findings": [{"file": "a.py", "line": 1, "severity": "medium",
                              "category": "bug", "message": "Edge case"}],
                "observations": [],
            }),
            # Lead echoes the specialist and objects -- on a filed finding only.
            self._resp({"decision": "REQUEST_CHANGES", "summary": "Edge case", "findings": []}),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        specialist = result.specialist_verdicts[0]
        assert specialist.findings == []
        assert specialist.decision == "APPROVE"
        assert [o.message for o in result.observations] == ["Edge case"]
        assert result.observation_sources == [("Logic", result.observations[0])]
        assert result.decision == "APPROVE"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_lead_p1_keeps_blocking_and_lead_p3_is_filed(self, mock_completion, mock_alerts):
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({"decision": "APPROVE", "checks": {}, "findings": [], "observations": []}),
            self._resp({
                "decision": "REQUEST_CHANGES", "summary": "Broken",
                "findings": [
                    {"file": "a.py", "line": 2, "severity": "high",
                     "category": "bug", "message": "Wrong result"},
                    {"file": "a.py", "line": 3, "severity": "low",
                     "category": "style", "message": "Rename"},
                ],
            }),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        assert result.decision == "REQUEST_CHANGES"
        assert [f.message for f in result.lead_findings] == ["Wrong result"]
        assert [o.message for o in result.observations] == ["Rename"]
        assert result.observation_sources[0][0] == "Lead"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_lead_request_changes_with_no_findings_anywhere_stands(self, mock_completion, mock_alerts):
        # Fail closed: an objection that names no finding is not softened.
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({"decision": "APPROVE", "checks": {}, "findings": [], "observations": []}),
            self._resp({"decision": "REQUEST_CHANGES", "summary": "Unexplained", "findings": []}),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        assert result.decision == "REQUEST_CHANGES"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_lead_block_backed_only_by_filed_findings_downgrades(self, mock_completion, mock_alerts):
        # BLOCK is in BLOCKING_DECISIONS alongside REQUEST_CHANGES; the
        # downgrade rule must not only handle the literal REQUEST_CHANGES value.
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({"decision": "APPROVE", "checks": {}, "findings": [], "observations": []}),
            self._resp({
                "decision": "BLOCK", "summary": "Naming nit",
                "findings": [{"file": "a.py", "line": 3, "severity": "low",
                              "category": "style", "message": "Rename"}],
            }),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        assert result.decision == "APPROVE"
        assert [o.message for o in result.observations] == ["Rename"]

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_lead_block_with_no_own_finding_stands_despite_unrelated_filed_finding(
        self, mock_completion, mock_alerts
    ):
        # BLOCK is the lead's own discovery, never a specialist passthrough
        # (unlike REQUEST_CHANGES, whose decision rule IS "if any specialist
        # returned REQUEST_CHANGES"). An unrelated specialist's filed P2/P3
        # finding is not evidence that this BLOCK rested on it, so it must
        # not downgrade a BLOCK that names no finding of the lead's own
        # (Codex review on #105 — this used to fail open to APPROVE).
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({
                "decision": "APPROVE", "checks": {},
                "findings": [{"file": "a.py", "line": 1, "severity": "medium",
                              "category": "bug", "message": "Edge case"}],
                "observations": [],
            }),
            self._resp({"decision": "BLOCK", "summary": "Unrelated plan misalignment", "findings": []}),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        assert result.decision == "BLOCK"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_total_skip_lead_block_downgrade_lands_on_not_reviewed(self, mock_completion, mock_alerts):
        # Step 2.5 (#79) deliberately leaves a blocking lead verdict standing
        # through a total specialist skip, rather than softening it to
        # NOT_REVIEWED, because the lead read the full diff on its own. But
        # if Step 2.7 then downgrades that same BLOCK to APPROVE on nothing
        # but the lead's own filed P2/P3 finding, the result is an aggregate
        # APPROVE from a review where zero specialists examined anything —
        # exactly the fail-open #79 exists to close, reopened one step later
        # (Codex review on #105). It must land on NOT_REVIEWED instead.
        mock_alerts.return_value = 0
        # No specialist call: the persona's file_patterns don't match "a.py",
        # so it is skipped (reviewed=False) before any model is asked.
        mock_completion.side_effect = [
            self._resp({
                "decision": "BLOCK", "summary": "Minor",
                "findings": [{"file": "a.py", "line": 3, "severity": "low",
                              "category": "style", "message": "Rename"}],
            }),
        ]
        persona = Persona(name="Docs", focus="Docs", system_prompt="Test", file_patterns=["*.md"])
        profile = ReviewProfile(name="test", specialists=[persona], lead_prompt="Lead")
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, profile)
        assert result.decision == DECISION_NOT_REVIEWED

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    @patch("vigil.decision_log.filter_known_findings")
    def test_known_decision_suppression_removes_filed_evidence(
        self, mock_filter, mock_completion, mock_alerts
    ):
        # The only P2/P3 finding is suppressed by the decision log (Step 1.5).
        # The lead never saw it, so it must not count as evidence for
        # downgrading the lead's own unsubstantiated REQUEST_CHANGES.
        mock_alerts.return_value = 0
        mock_filter.return_value = []
        mock_completion.side_effect = [
            self._resp({
                "decision": "REQUEST_CHANGES", "checks": {},
                "findings": [{"file": "a.py", "line": 1, "severity": "medium",
                              "category": "bug", "message": "Edge case"}],
                "observations": [],
            }),
            self._resp({"decision": "REQUEST_CHANGES", "summary": "Unexplained", "findings": []}),
        ]
        result = review_diff(
            "diff --git a/a.py b/a.py\n", self._ctx, self._profile(),
            repo_key="owner/repo",
        )
        assert result.observations == []
        assert result.decision == "REQUEST_CHANGES"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_nonblocking_persona_filed_finding_counts_as_evidence(self, mock_completion, mock_alerts):
        # A non-blocking persona (e.g. Security) moves every finding to
        # observations before P-scale routing ever sees it. Its P2/P3 findings
        # must still count as filed evidence for Step 2.7, or a lead that
        # references one in its summary (per the zero-duplication rule)
        # instead of re-filing it leaves an unsubstantiated REQUEST_CHANGES
        # standing.
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({
                "decision": "REQUEST_CHANGES", "checks": {},
                "findings": [{"file": "a.py", "line": 1, "severity": "medium",
                              "category": "bug", "message": "Edge case"}],
                "observations": [],
            }),
            self._resp({"decision": "REQUEST_CHANGES", "summary": "Echoes Security", "findings": []}),
        ]
        persona = Persona(name="Security", focus="Sec", system_prompt="Test", blocking=False)
        profile = ReviewProfile(name="test", specialists=[persona], lead_prompt="Lead")
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, profile)
        assert result.decision == "APPROVE"
        assert [o.message for o in result.observations] == ["Edge case"]

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_lead_filed_observation_dedupes_against_specialist(self, mock_completion, mock_alerts):
        # A lead P2/P3 finding that paraphrases a specialist's already-filed
        # observation at the same location must merge with it, not become a
        # second near-identical issue (F2iLLC/vigil#96's failure mode, now
        # reachable from the lead's own filed findings too).
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._resp({
                "decision": "APPROVE", "checks": {},
                "findings": [{"file": "a.py", "line": 5, "severity": "low",
                              "category": "style", "message": "Rename this loop variable"}],
                "observations": [],
            }),
            self._resp({
                "decision": "REQUEST_CHANGES", "summary": "Naming",
                "findings": [{"file": "a.py", "line": 5, "severity": "low",
                              "category": "style", "message": "Loop variable name is unclear"}],
            }),
        ]
        result = review_diff("diff --git a/a.py b/a.py\n", self._ctx, self._profile())
        assert len(result.observations) == 1
        assert result.observation_sources[0][0] == "Logic + Lead"
