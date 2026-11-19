"""vigil #110/#115/#119 — a truncated or decision-less reply is never an APPROVE.

A reply cut off by max_tokens used to fall through the prose fallback to a
nested finding object, and ``raw.get("decision", "APPROVE")`` then turned a
blocking verdict into APPROVE. A specialist must surface as ERROR and the lead
must raise.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from vigil.personas import Persona, ReviewProfile
from vigil.reviewer import _run_lead_review, review_diff

TRUNCATED = '{"decision":"REQUEST_CHANGES","findings":[{"file":"a.py","line":1,"severity":"high","message":"bug"}'
BAD_REPLIES = [
    TRUNCATED, "{}", '{"findings": []}',
    # vigil #136: present but not a string
    '{"decision": 1, "findings": []}', '{"decision": null, "findings": []}',
]


def _resp(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content), finish_reason="length")]
    return resp


def _lead_ok(decision: str = "APPROVE") -> MagicMock:
    return _resp(json.dumps({"decision": decision, "summary": "ok", "findings": []}))


def _diff() -> str:
    return (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
    )


def _pr_context() -> dict:
    return {"title": "t", "author": "u", "head": "f", "base": "main", "additions": 1,
            "deletions": 0, "changed_files": 1, "body": "", "head_sha": "a" * 40,
            "url": "https://github.com/o/r/pull/1"}


def _profile() -> ReviewProfile:
    return ReviewProfile(
        name="test",
        specialists=[Persona(name="Logic", focus="Bugs", system_prompt="p", file_patterns=["*.py"])],
        lead_prompt="You are the lead.",
    )


@pytest.mark.parametrize("reply", BAD_REPLIES)
@patch("vigil.reviewer.send_alerts_for_verdicts", return_value=0)
@patch("vigil.reviewer._call_llm_with_retry")
def test_bad_specialist_reply_is_error_not_approve(mock_llm, _alerts, reply):
    mock_llm.side_effect = [_resp(reply), _lead_ok()]
    result = review_diff(_diff(), _pr_context(), _profile())
    [verdict] = result.specialist_verdicts
    assert verdict.decision == "ERROR"
    assert verdict.decision != "APPROVE"


@pytest.mark.parametrize("reply", BAD_REPLIES)
@patch("vigil.reviewer._call_llm_with_retry")
def test_bad_lead_reply_raises(mock_llm, reply):
    mock_llm.return_value = _resp(reply)
    with pytest.raises(ValueError, match="lead reviewer"):
        _run_lead_review("lead", "pr", [], "m")


@pytest.mark.parametrize("reply", BAD_REPLIES)
@patch("vigil.reviewer.send_alerts_for_verdicts", return_value=0)
@patch("vigil.reviewer._call_llm_with_retry")
def test_bad_lead_reply_never_yields_approve_result(mock_llm, _alerts, reply):
    spec = _resp(json.dumps({"decision": "APPROVE", "checks": {}, "findings": [], "observations": []}))
    mock_llm.side_effect = [spec, _resp(reply)]
    # vigil #134: review_diff does not catch the lead's ValueError; it propagates.
    with pytest.raises(ValueError, match="lead reviewer"):
        review_diff(_diff(), _pr_context(), _profile())


@patch("vigil.reviewer.send_alerts_for_verdicts", return_value=0)
@patch("vigil.reviewer._call_llm_with_retry")
def test_well_formed_approve_still_approves(mock_llm, _alerts):
    spec = _resp(json.dumps({"decision": "APPROVE", "findings": []}))
    mock_llm.side_effect = [spec, _lead_ok()]
    result = review_diff(_diff(), _pr_context(), _profile())
    assert result.specialist_verdicts[0].decision == "APPROVE"
    assert result.decision == "APPROVE"
