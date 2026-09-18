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
from vigil.models import Finding, PersonaVerdict, Severity
from vigil.personas import Persona, ReviewProfile
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


# ---------- Specialist routing ----------

class TestRouteFindingsByPriority:
    def test_p2_p3_move_to_observations_and_verdict_approves(self):
        v = _verdict("REQUEST_CHANGES", [_finding("medium", "edge"), _finding("low", "polish")])
        filed = _route_findings_by_priority(v)
        assert filed == 2
        assert v.findings == []
        assert [f.message for f in v.observations] == ["edge", "polish"]
        assert v.decision == "APPROVE"

    def test_p0_p1_stay_blocking(self):
        v = _verdict("REQUEST_CHANGES", [_finding("high", "real"), _finding("medium", "edge")])
        filed = _route_findings_by_priority(v)
        assert filed == 1
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
        assert _route_findings_by_priority(v) == 0
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
