"""Tests for the reusable workflow's ``Check review provider key`` step (#114).

A caller who sets three of the four Anthropic federation variables must be
told which one is missing, not "all four". These tests execute the step's own
``run`` body under GitHub's default bash flags and assert on its output.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reusable-vigil.yml"

FED_VARS = [
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
]
CLAUDE = "claude-sonnet-4-5"


def _step_script() -> str:
    data = yaml.safe_load(WORKFLOW.read_text())
    for step in data["jobs"]["review"]["steps"]:
        if step.get("name") == "Check review provider key":
            return step["run"]
    raise AssertionError("Check review provider key step not found")


def run_check(tmp_path: Path, model: str, lead_model: str = "", **values: str):
    out_file = tmp_path / "github_output"
    out_file.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out_file),
        "MODEL": model,
        "LEAD_MODEL": lead_model,
        "GEMINI_API_KEY": "",
        "OPENAI_API_KEY": "",
    }
    for name in FED_VARS:
        env[name] = ""
    env.update(values)
    proc = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step_script()],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    outputs = dict(
        line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line
    )
    warnings = [l for l in proc.stdout.splitlines() if l.startswith("::warning::")]
    return outputs, warnings


def _set(names):
    return {n: f"value-for-{n.lower()}" for n in names}


@pytest.mark.parametrize("absent", FED_VARS)
def test_single_missing_variable_is_named(tmp_path, absent):
    present = [n for n in FED_VARS if n != absent]
    outputs, warnings = run_check(tmp_path, CLAUDE, **_set(present))
    assert len(warnings) == 1
    w = warnings[0]
    assert absent in w
    for n in present:
        assert n not in w
        assert f"value-for-{n.lower()}" not in w
    assert "partial configuration is invalid" in w
    assert "static API keys are disabled" in w
    assert w.endswith("is not configured for the selected model(s).")
    assert outputs["has_key"] == "false"
    assert outputs["use_wif"] == "false"


def test_two_missing_variables_are_named_in_order(tmp_path):
    present = [FED_VARS[1], FED_VARS[3]]
    outputs, warnings = run_check(tmp_path, CLAUDE, **_set(present))
    w = warnings[0]
    assert FED_VARS[0] in w and FED_VARS[2] in w
    assert w.index(FED_VARS[0]) < w.index(FED_VARS[2])
    for n in present:
        assert n not in w
    assert outputs["has_key"] == "false"


def test_none_set_names_all_four(tmp_path):
    outputs, warnings = run_check(tmp_path, CLAUDE)
    assert len(warnings) == 1
    w = warnings[0]
    positions = [w.index(n) for n in FED_VARS]
    assert positions == sorted(positions)
    assert "static API keys are disabled" in w
    assert outputs["has_key"] == "false"
    assert outputs["use_wif"] == "false"


def test_all_four_set_uses_wif(tmp_path):
    outputs, warnings = run_check(tmp_path, CLAUDE, **_set(FED_VARS))
    assert warnings == []
    assert outputs["has_key"] == "true"
    assert outputs["use_wif"] == "true"


def test_claude_model_and_lead_model_name_missing_once(tmp_path):
    absent = FED_VARS[2]
    present = [n for n in FED_VARS if n != absent]
    outputs, warnings = run_check(
        tmp_path, CLAUDE, "anthropic/claude-opus-4", **_set(present)
    )
    assert len(warnings) == 1
    assert len(re.findall(absent, warnings[0])) == 1
    assert outputs["has_key"] == "false"


def test_openai_missing_key_names_no_anthropic_variable(tmp_path):
    outputs, warnings = run_check(tmp_path, "openai/gpt-x")
    assert len(warnings) == 1
    assert "OPENAI_API_KEY" in warnings[0]
    assert "ANTHROPIC_" not in warnings[0]
    assert outputs["has_key"] == "false"
    assert outputs["use_wif"] == "false"
