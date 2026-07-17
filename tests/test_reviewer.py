"""Tests for reviewer.py: non-blocking persona logic and decision filtering."""

import json
import pytest
from unittest.mock import patch, MagicMock

from vigil.models import Finding, PersonaVerdict, ReviewResult, Severity
from vigil.personas import Persona, ReviewProfile
from vigil.reviewer import _parse_json_response, _parse_findings, _is_transient_llm_error, review_diff


# ---------- _parse_json_response ----------

class TestParseJsonResponse:

    def test_plain_json(self):
        raw = '{"decision": "APPROVE", "findings": []}'
        assert _parse_json_response(raw)["decision"] == "APPROVE"

    def test_code_fenced_json(self):
        raw = '```json\n{"decision": "APPROVE"}\n```'
        assert _parse_json_response(raw)["decision"] == "APPROVE"

    def test_json_with_trailing_text(self):
        raw = '{"decision": "APPROVE"} some trailing text'
        assert _parse_json_response(raw)["decision"] == "APPROVE"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_json_response("not json at all")


# ---------- _parse_findings ----------

class TestParseFindings:

    def test_basic_finding(self):
        raw = [{"file": "a.py", "line": 10, "severity": "high",
                "category": "bug", "message": "Off by one"}]
        findings = _parse_findings(raw)
        assert len(findings) == 1
        assert findings[0].file == "a.py"
        assert findings[0].severity == Severity.high

    def test_null_file_coerced(self):
        raw = [{"file": None, "line": 1, "severity": "low",
                "category": "test", "message": "msg"}]
        findings = _parse_findings(raw)
        assert findings[0].file == "unknown"

    def test_invalid_line_coerced(self):
        raw = [{"file": "a.py", "line": "not-a-number", "severity": "low",
                "category": "test", "message": "msg"}]
        findings = _parse_findings(raw)
        assert findings[0].line is None


# ---------- Non-blocking persona logic in review_diff ----------

class TestNonBlockingPersonaLogic:

    def _make_mock_profile(self, blocking=True):
        """Create a profile with one specialist (optionally non-blocking)."""
        persona = Persona(
            name="TestSpecialist",
            focus="Testing",
            system_prompt="You are a test reviewer.",
            blocking=blocking,
        )
        return ReviewProfile(
            name="test",
            specialists=[persona],
            lead_prompt="You are the lead.",
        )

    def _mock_completion(self, decision="REQUEST_CHANGES", findings=None, observations=None):
        """Build a mock LLM response."""
        resp = {
            "decision": decision,
            "checks": {"test_check": "CONCERN"},
            "findings": findings or [
                {"file": "a.py", "line": 1, "severity": "high",
                 "category": "bug", "message": "Found a bug"}
            ],
            "observations": observations or [],
        }
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(resp)))]
        return mock_resp

    def _mock_lead_response(self, decision="APPROVE"):
        resp = {"decision": decision, "summary": "Looks good", "findings": []}
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(resp)))]
        return mock_resp

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_blocking_persona_keeps_findings(self, mock_completion, mock_alerts):
        """Blocking personas keep their findings as-is."""
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._mock_completion(decision="REQUEST_CHANGES"),
            self._mock_lead_response(decision="REQUEST_CHANGES"),
        ]

        profile = self._make_mock_profile(blocking=True)
        pr_context = {
            "title": "Test PR", "author": "user", "head": "feature",
            "base": "main", "additions": 10, "deletions": 5,
            "changed_files": 1, "body": "Test",
        }

        result = review_diff("diff --git a/a.py b/a.py\n", pr_context, profile)
        # Blocking persona should keep findings
        specialist = result.specialist_verdicts[0]
        assert len(specialist.findings) == 1
        assert len(specialist.observations) == 0

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_nonblocking_persona_moves_findings_to_observations(self, mock_completion, mock_alerts):
        """Non-blocking personas move findings to observations and force APPROVE."""
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._mock_completion(decision="REQUEST_CHANGES"),
            self._mock_lead_response(decision="APPROVE"),
        ]

        profile = self._make_mock_profile(blocking=False)
        pr_context = {
            "title": "Test PR", "author": "user", "head": "feature",
            "base": "main", "additions": 10, "deletions": 5,
            "changed_files": 1, "body": "Test",
        }

        result = review_diff("diff --git a/a.py b/a.py\n", pr_context, profile)
        specialist = result.specialist_verdicts[0]
        # Findings should be moved to observations
        assert len(specialist.findings) == 0
        assert len(specialist.observations) == 1
        # Decision should be forced to APPROVE
        assert specialist.decision == "APPROVE"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_nonblocking_observations_in_result(self, mock_completion, mock_alerts):
        """Non-blocking observations appear in result.observations."""
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._mock_completion(decision="REQUEST_CHANGES"),
            self._mock_lead_response(decision="APPROVE"),
        ]

        profile = self._make_mock_profile(blocking=False)
        pr_context = {
            "title": "Test PR", "author": "user", "head": "feature",
            "base": "main", "additions": 10, "deletions": 5,
            "changed_files": 1, "body": "Test",
        }

        result = review_diff("diff --git a/a.py b/a.py\n", pr_context, profile)
        assert len(result.observations) == 1
        assert result.observations[0].message == "Found a bug"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    def test_observation_sources_tracked(self, mock_completion, mock_alerts):
        """observation_sources tracks which persona produced each observation."""
        mock_alerts.return_value = 0
        mock_completion.side_effect = [
            self._mock_completion(decision="REQUEST_CHANGES"),
            self._mock_lead_response(decision="APPROVE"),
        ]

        profile = self._make_mock_profile(blocking=False)
        pr_context = {
            "title": "Test PR", "author": "user", "head": "feature",
            "base": "main", "additions": 10, "deletions": 5,
            "changed_files": 1, "body": "Test",
        }

        result = review_diff("diff --git a/a.py b/a.py\n", pr_context, profile)
        assert len(result.observation_sources) == 1
        persona_name, finding = result.observation_sources[0]
        assert persona_name == "TestSpecialist"
        assert finding.message == "Found a bug"


# ---------- Decision filtering in review_diff ----------

class TestDecisionFiltering:

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer.completion")
    @patch("vigil.decision_log.filter_known_findings")
    def test_known_findings_suppressed(self, mock_filter, mock_completion, mock_alerts):
        """When repo_key is provided, known findings are filtered out."""
        mock_alerts.return_value = 0
        # The filter returns empty list (all findings suppressed)
        mock_filter.return_value = []

        finding_data = {
            "decision": "REQUEST_CHANGES",
            "checks": {},
            "findings": [
                {"file": "a.py", "line": 1, "severity": "high",
                 "category": "bug", "message": "Known bug"}
            ],
            "observations": [],
        }
        specialist_resp = MagicMock()
        specialist_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(finding_data)))]

        lead_data = {"decision": "APPROVE", "summary": "OK", "findings": []}
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(lead_data)))]

        mock_completion.side_effect = [specialist_resp, lead_resp]

        persona = Persona(name="Logic", focus="Bugs", system_prompt="Test")
        profile = ReviewProfile(name="test", specialists=[persona], lead_prompt="Lead")
        pr_context = {
            "title": "Test", "author": "u", "head": "f", "base": "m",
            "additions": 1, "deletions": 0, "changed_files": 1, "body": "",
        }

        result = review_diff(
            "diff --git a/a.py b/a.py\n", pr_context, profile,
            repo_key="owner/repo",
        )

        # filter_known_findings should have been called
        assert mock_filter.call_count >= 1


# ---------- Transient specialist error handling ----------

class TestTransientSpecialistErrors:

    def _make_profile(self):
        persona = Persona(name="Architecture", focus="Design", system_prompt="You are an architect.")
        return ReviewProfile(name="test", specialists=[persona], lead_prompt="You are the lead.")

    def _mock_lead_response(self, decision="APPROVE"):
        resp = {"decision": decision, "summary": "Looks good", "findings": []}
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(resp)))]
        return mock_resp

    def _pr_context(self):
        return {
            "title": "Test PR", "author": "user", "head": "feature",
            "base": "main", "additions": 3, "deletions": 1,
            "changed_files": 1, "body": "",
        }

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_503_produces_observation_not_finding(self, mock_llm, mock_alerts):
        """A 503 from a specialist should produce a non-blocking observation, not a finding."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [Exception("503 Service Unavailable"), lead_resp]

        profile = self._make_profile()
        result = review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        specialist = result.specialist_verdicts[0]
        assert specialist.decision == "APPROVE"
        assert len(specialist.findings) == 0
        assert len(specialist.observations) == 1
        assert "unavailable" in specialist.observations[0].message.lower()
        assert specialist.observations[0].category == "reviewer_unavailable"
        assert specialist.observations[0].severity == Severity.low

    def test_quota_429_is_not_transient(self):
        """Quota and billing 429s should remain blocking reviewer errors."""
        assert not _is_transient_llm_error(
            Exception("429: You exceeded your current quota, please check your plan and billing details")
        )
        assert _is_transient_llm_error(Exception("429 rate_limit: retry after 30 seconds"))

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_503_does_not_block_overall_review(self, mock_llm, mock_alerts):
        """A 503 specialist error should not cause the overall review to REQUEST_CHANGES."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [Exception("GeminiException 503 UNAVAILABLE"), lead_resp]

        profile = self._make_profile()
        result = review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        assert result.decision == "APPROVE"
        assert len(result.lead_findings) == 0

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_timeout_produces_observation_not_finding(self, mock_llm, mock_alerts):
        """A timeout from a specialist should produce a non-blocking observation."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [Exception("Request timed out after 30s"), lead_resp]

        profile = self._make_profile()
        result = review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        specialist = result.specialist_verdicts[0]
        assert specialist.decision == "APPROVE"
        assert len(specialist.findings) == 0
        assert specialist.observations[0].category == "reviewer_unavailable"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_non_transient_error_still_produces_error_finding(self, mock_llm, mock_alerts):
        """A non-transient error (e.g. bad model config) should still produce an ERROR verdict."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [Exception("Invalid API key: authentication failed"), lead_resp]

        profile = self._make_profile()
        result = review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        specialist = result.specialist_verdicts[0]
        assert specialist.decision == "ERROR"
        assert len(specialist.findings) == 1
        assert specialist.findings[0].category == "reviewer_error"

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_quota_429_produces_error_finding(self, mock_llm, mock_alerts):
        """A non-retryable 429 should produce an ERROR verdict, not a skipped specialist."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [
            Exception("429: insufficient_quota - check billing details"),
            lead_resp,
        ]

        profile = self._make_profile()
        result = review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        specialist = result.specialist_verdicts[0]
        assert specialist.decision == "ERROR"
        assert len(specialist.findings) == 1
        assert specialist.findings[0].category == "reviewer_error"
        assert len(specialist.observations) == 0

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_specialist_observations_are_sent_to_lead(self, mock_llm, mock_alerts):
        """Skipped-specialist observations should be visible to the lead reviewer."""
        mock_alerts.return_value = 0
        lead_json = json.dumps({"decision": "APPROVE", "summary": "Looks good", "findings": []})
        lead_resp = MagicMock()
        lead_resp.choices = [MagicMock(message=MagicMock(content=lead_json))]
        mock_llm.side_effect = [Exception("503 Service Unavailable"), lead_resp]

        profile = self._make_profile()
        review_diff("diff --git a/a.py b/a.py\n", self._pr_context(), profile)

        lead_messages = mock_llm.call_args_list[1].kwargs["messages"]
        lead_prompt = lead_messages[1]["content"]
        assert "Observations:" in lead_prompt
        assert "review skipped" in lead_prompt.lower()
        assert "No findings." in lead_prompt


class TestDocumentationOnlyAutoApprove:

    @patch("vigil.reviewer.send_alerts_for_verdicts")
    @patch("vigil.reviewer._call_llm_with_retry")
    def test_documentation_only_pr_auto_approves_without_llm(self, mock_llm, mock_alerts):
        profile = ReviewProfile(
            name="test",
            specialists=[
                Persona(name="Security", focus="Security", system_prompt="Review security"),
                Persona(name="DX", focus="Docs", system_prompt="Review docs"),
            ],
            lead_prompt="Lead",
        )
        pr_context = {
            "title": "Update docs",
            "author": "user",
            "head": "docs-branch",
            "base": "main",
            "additions": 2,
            "deletions": 0,
            "changed_files": 1,
            "body": "",
            "head_sha": "abcdef123456",
            "url": "https://github.com/o/r/pull/1",
        }
        diff = """\
diff --git a/docs/setup.md b/docs/setup.md
index 1111111..2222222 100644
--- a/docs/setup.md
+++ b/docs/setup.md
@@ -1 +1,2 @@
 # Setup
+New install step
"""

        result = review_diff(diff, pr_context, profile)

        assert result.decision == "APPROVE"
        assert "Documentation-only" in result.summary
        assert len(result.specialist_verdicts) == 2
        assert all(v.decision == "APPROVE" for v in result.specialist_verdicts)
        assert all(v.checks == {"documentation_only": "PASS"} for v in result.specialist_verdicts)
        mock_llm.assert_not_called()
        mock_alerts.assert_not_called()
