"""Regression tests for the public setup and workflow documentation."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")
CALLER = (ROOT / ".github" / "workflows" / "vigil.yml").read_text(encoding="utf-8")
REUSABLE = (ROOT / ".github" / "workflows" / "reusable-vigil.yml").read_text(
    encoding="utf-8"
)

ACTION_YAML = yaml.safe_load(ACTION)
REUSABLE_YAML = yaml.safe_load(REUSABLE)


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


# --- F2iLLC/vigil#51 item 1 ("fail loudly") -------------------------------
#
# A venv/install failure inside the composite action must never be
# indistinguishable from a passing review at the check level. These tests
# assert the contract from the parsed YAML (not regex over raw text) so a
# future edit that quietly drops the guard step, its `if: always()`, the
# `review-ran` output, or the `advisory` default gets caught here instead
# of by another silent outage. See tests/test_fail_loudly_guard.py for
# behavioral (shell-level) coverage of the guard step itself.


def _find_step(steps, *, step_id=None, name=None):
    for step in steps:
        if step_id is not None and step.get("id") == step_id:
            return step
        if name is not None and step.get("name") == name:
            return step
    return None


def test_action_declares_a_review_ran_output():
    outputs = ACTION_YAML.get("outputs") or {}
    assert "review-ran" in outputs, "action.yml must declare a review-ran output"
    assert outputs["review-ran"]["value"] == "${{ steps.guard.outputs.review-ran }}"
    assert "review-ran" in README, "README should document the review-ran output"


def test_guard_step_exists_and_always_runs():
    steps = ACTION_YAML["runs"]["steps"]
    guard = _find_step(steps, step_id="guard")
    assert guard is not None, (
        "action.yml must have a step with id: guard that verifies Vigil "
        "actually ran"
    )
    assert guard.get("if") == "always()", (
        "the guard step must run with if: always() so it still executes "
        "when an earlier step (e.g. the venv install) failed"
    )
    assert guard.get("shell") == "bash"

    # The guard step reads steps.install.outcome and steps.run.outcome, so
    # both referenced steps need stable ids for that to resolve.
    install_step = _find_step(steps, step_id="install")
    assert install_step is not None
    run_step = _find_step(steps, name="Run Vigil")
    assert run_step is not None
    assert run_step.get("id") == "run", (
        "the 'Run Vigil' step needs an id so the guard step can read "
        "steps.run.outcome"
    )


def test_reusable_workflow_advisory_still_defaults_to_true():
    # `on:` is parsed by PyYAML's default (YAML 1.1) resolver as the
    # boolean key True rather than the string "on".
    trigger_key = True if True in REUSABLE_YAML else "on"
    advisory = REUSABLE_YAML[trigger_key]["workflow_call"]["inputs"]["advisory"]

    assert advisory["default"] is True, (
        "advisory must stay true by default -- flipping it turns every "
        "Vigil infrastructure hiccup into a merge blocker fleet-wide and "
        "is a deliberate operator decision, not a side effect of the "
        "fail-loudly guard (F2iLLC/vigil#51 item 1 explicitly keeps this "
        "unchanged; only item 3 documents the tradeoff)."
    )
    # The tradeoff must actually be documented, not just preserved.
    assert "advisory" in REUSABLE, "sanity: the input is still present"
