"""Contract tests for the ``F2iLLC/vigil@<sha>`` pins in the reusable workflow
(F2iLLC/vigil#88).

WHY THIS FILE EXISTS
--------------------
``.github/workflows/reusable-vigil.yml`` pins the composite action at a fixed
commit SHA in three separate ``uses:`` steps, independently of the `v1` alias
that every other consumer follows. That independence is a real risk surface,
not just a documentation nit:

1. A split-brain partial bump — one job's pin advanced, another job's left
   behind — would silently run different review logic in different jobs of
   the same workflow. GitHub gives no error for this; it is a plain YAML
   string.
2. The reusable workflow passes a fixed set of ``with:`` inputs to the pinned
   action. If a caller-side input is removed from (or never existed in)
   ``action.yml`` at the pinned SHA, GitHub *warns*, it does not *fail* --
   the input is silently dropped and whatever it was supposed to configure
   just stops taking effect. That is exactly the failure mode a stale inline
   comment in this workflow used to claim was happening (falsely, as of the
   pin this repo carries today) -- see F2iLLC/vigil#88.

These tests are pure static parsing of the repo's own files. No network, no
git calls that need a remote -- they just read the YAML off disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
REUSABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reusable-vigil.yml"
ACTION_YML = REPO_ROOT / "action.yml"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def iter_vigil_pin_steps(workflow: dict):
    """Yield every step across every job whose ``uses:`` pins F2iLLC/vigil."""
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith("F2iLLC/vigil@"):
                yield step


def test_all_vigil_pins_in_reusable_workflow_agree() -> None:
    """Every ``F2iLLC/vigil@<sha>`` reference in this workflow must be the
    same SHA.

    This does not assert *which* SHA -- that is an owner decision
    (F2iLLC/vigil#88) -- only that the three jobs (``review``, ``dismiss``,
    ``resolve-addressed``) cannot drift apart from each other and run
    different review logic on different triggers of the same PR.
    """
    workflow = load_yaml(REUSABLE_WORKFLOW)
    pins = {step["uses"] for step in iter_vigil_pin_steps(workflow)}

    assert pins, "expected at least one F2iLLC/vigil@<sha> step in the reusable workflow"
    assert len(pins) == 1, (
        "the reusable workflow's F2iLLC/vigil pins have split: "
        f"found {sorted(pins)!r} -- every job must run the same pinned commit"
    )


def test_every_with_input_passed_to_the_pin_exists_in_action_yml() -> None:
    """Every input key the reusable workflow passes under ``with:`` to a
    ``F2iLLC/vigil@...`` step must be declared in the repo's own
    ``action.yml``.

    This is the real bug class the old (now-corrected) NOTE comment was
    worried about: the reusable workflow silently passing an input the
    pinned action does not accept. GitHub only warns on an unknown
    composite-action input -- it does not fail the run -- so nothing else
    would catch this.
    """
    workflow = load_yaml(REUSABLE_WORKFLOW)
    action = load_yaml(ACTION_YML)
    declared_inputs = set((action.get("inputs") or {}).keys())

    assert declared_inputs, "action.yml declared no inputs -- can't validate against it"

    for step in iter_vigil_pin_steps(workflow):
        passed_inputs = set((step.get("with") or {}).keys())
        unknown = passed_inputs - declared_inputs
        assert not unknown, (
            f"step {step.get('name') or step.get('uses')!r} passes input(s) "
            f"{sorted(unknown)!r} that action.yml does not declare -- GitHub "
            "will silently drop these rather than failing the run"
        )


def test_reusable_workflow_no_longer_claims_inputs_are_inert_at_the_pin() -> None:
    """Lock in the F2iLLC/vigil#88 correction: the workflow must not assert
    that inputs it passes are absent from ``action.yml`` at the pinned SHA.

    That claim was true when originally written but was invalidated when the
    pin was bumped in PR #73 (``d587e2b``): ``action.yml`` at the pin this
    repo currently carries is byte-identical to ``main``'s, so `force` /
    `reason` / `context-provider` / `context-label` / `context-token` are all
    declared there. This guards against the false claim silently coming back
    (e.g. via a revert) without re-verifying it against the actual pin.
    """
    text = REUSABLE_WORKFLOW.read_text(encoding="utf-8")

    assert "none of them exist at the pinned SHA" not in text
    assert "stay\nINERT" not in text
    assert "stay INERT" not in text
    assert "INERT until this pin is advanced" not in text
