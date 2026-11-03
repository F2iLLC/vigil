"""Prompt caching layout for Claude models and per-review token usage telemetry."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import vigil.reviewer as reviewer
from vigil.personas import Persona, ReviewProfile
from vigil.reviewer import REVIEW_FRAME, review_diff

CLAUDE = "claude-sonnet-5-5"
GEMINI = "gemini/gemini-3.1-flash-lite"
MARKER = "UNIQUE_DIFF_MARKER_7731"
DIFF = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
    f"-old = 1\n+new = '{MARKER}'\n"
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
    "-Old docs\n+New docs\n"
)
PR_CONTEXT = {"title": "Test PR", "author": "user", "head": "feature", "base": "main",
              "additions": 2, "deletions": 2, "changed_files": 2, "body": ""}
SPECIALIST_JSON = json.dumps({"decision": "APPROVE", "checks": {}, "findings": [], "observations": []})
LEAD_JSON = json.dumps({"decision": "APPROVE", "summary": "ok", "findings": []})


def _profile(*personas):
    return ReviewProfile(name="test", specialists=list(personas), lead_prompt="LEAD PROMPT")


def _full(name):
    return Persona(name=name, focus="f", system_prompt=f"PROMPT {name}")


def _docs_only(name):
    return Persona(name=name, focus="f", system_prompt=f"PROMPT {name}", file_patterns=["*.md"])


def _response(content, usage=None):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    resp.usage = usage
    return resp


def _run(profile, model, usage=None):
    calls = []

    def fake_completion(model, messages, **kwargs):
        calls.append(messages)
        is_lead = messages[-1]["content"] == "LEAD PROMPT" or messages[0]["content"] == "LEAD PROMPT"
        return _response(LEAD_JSON if is_lead else SPECIALIST_JSON, usage)

    with patch("vigil.reviewer.completion", side_effect=fake_completion), \
            patch("vigil.reviewer.send_alerts_for_verdicts", return_value=0):
        review_diff(DIFF, PR_CONTEXT, profile, model=model)
    return calls


def _pr_part(messages):
    return messages[1]["content"][0]


def test_claude_layout_shares_one_cached_prefix_and_keeps_pr_text_out_of_system():
    calls = _run(_profile(_full("A"), _full("B"), _docs_only("C")), CLAUDE)
    a, b, c, lead = calls

    for messages in calls:
        assert messages[0] == {"role": "system", "content": REVIEW_FRAME}
        assert messages[-1]["role"] == "system"
        for message in messages:
            if message["role"] == "system":
                assert MARKER not in json.dumps(message), "PR text must never be sent with system authority"

    assert [m[-1]["content"] for m in calls] == ["PROMPT A", "PROMPT B", "PROMPT C", "LEAD PROMPT"]
    # A, B and the lead send the identical full-diff block: one shared, cache-marked prefix.
    assert a[:2] == b[:2] == lead[:2]
    assert _pr_part(a)["cache_control"] == {"type": "ephemeral"}
    assert MARKER in _pr_part(a)["text"]
    # C reviews only the docs, so its block is unique and is not marked (a write never read costs more).
    assert "cache_control" not in _pr_part(c)
    assert MARKER not in _pr_part(c)["text"]
    # Lead verdicts stay in the user role, after the cached block.
    assert lead[2]["role"] == "user" and lead[2]["content"].startswith("## Specialist Verdicts")


def test_blocks_used_once_are_not_cache_marked():
    calls = _run(_profile(_docs_only("C")), CLAUDE)
    assert all("cache_control" not in _pr_part(messages) for messages in calls)


def test_non_claude_models_keep_the_original_message_shape():
    calls = _run(_profile(_full("A"), _docs_only("C")), GEMINI)
    a, c, lead = calls
    assert a[0] == {"role": "system", "content": "PROMPT A"}
    assert a[1]["role"] == "user" and isinstance(a[1]["content"], str) and MARKER in a[1]["content"]
    assert len(a) == 2 and len(c) == 2
    assert lead[0] == {"role": "system", "content": "LEAD PROMPT"}
    assert lead[1]["content"].startswith(a[1]["content"] + "\n\n---\n\n## Specialist Verdicts")
    assert "cache_control" not in json.dumps(calls)


def test_usage_is_summarised_per_review(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                            cache_creation_input_tokens=30, cache_read_input_tokens=50)
    _run(_profile(_full("A"), _full("B")), CLAUDE, usage=usage)

    err = capsys.readouterr().err
    assert "Vigil token usage: calls=3 input=300 output=60 cache_write=90 cache_read=150" in err
    assert "| 3 | 300 | 90 | 150 | 60 |" in summary.read_text(encoding="utf-8")
    assert reviewer._usage_log is None, "the collector is cleared after each review"


def test_missing_usage_fields_count_as_zero():
    usage = {"prompt_tokens": 7, "completion_tokens": None}
    reviewer._usage_log = []
    try:
        reviewer._record_usage(GEMINI, _response("{}", usage))
        assert reviewer._usage_summary(reviewer._usage_log) == {
            "calls": 1, "input": 7, "output": 0, "cache_write": 0, "cache_read": 0}
    finally:
        reviewer._usage_log = None
