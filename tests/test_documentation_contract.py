"""Regression tests for the public setup and workflow documentation."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")
CALLER = (ROOT / ".github" / "workflows" / "vigil.yml").read_text(encoding="utf-8")
REUSABLE = (ROOT / ".github" / "workflows" / "reusable-vigil.yml").read_text(
    encoding="utf-8"
)


def _action_default(input_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(input_name)}:\n"
        rf"(?:^    .*\n)*?"
        rf"^    default: [\"']?([^\"'\n]+)",
        ACTION,
        re.MULTILINE,
    )
    assert match, f"Could not find the {input_name!r} default in action.yml"
    return match.group(1).strip()


def test_readme_tracks_the_action_model_and_profile_defaults():
    model = _action_default("model")
    profile = _action_default("profile")

    assert f"Default: {model}" in README
    assert f"| `profile` | `{profile}` |" in README


def test_readme_documents_every_public_action_input():
    documented_inputs = {
        "pr-url",
        "command",
        "model",
        "lead-model",
        "profile",
        "force",
        "reason",
        "github-token",
        "gemini-api-key",
        "openai-api-key",
        "anthropic-api-key",
    }

    for input_name in documented_inputs:
        assert f"| `{input_name}` |" in README


def test_public_examples_do_not_recommend_the_stale_v1_tag():
    stale_reference = "uses: F2iLLC/vigil@v1"

    assert stale_reference not in README
    assert stale_reference not in CALLER
    assert stale_reference not in REUSABLE


def test_central_workflow_owns_approval_token_and_current_model():
    model = _action_default("model")

    assert "workflow_call:" in REUSABLE
    assert "VIGIL_REVIEW_TOKEN:" in REUSABLE
    assert "github-token: ${{ secrets.VIGIL_REVIEW_TOKEN || github.token }}" in REUSABLE
    assert f"default: {model}" in REUSABLE
    assert "uses: ./.github/workflows/reusable-vigil.yml" in CALLER


def test_readme_explains_comment_fallback_and_central_caller():
    assert "cannot satisfy a required-approval branch rule" in README
    assert "F2iLLC/vigil/.github/workflows/reusable-vigil.yml@main" in README
