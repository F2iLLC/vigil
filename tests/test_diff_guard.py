from __future__ import annotations

import pytest
import typer

from vigil import cli
from vigil.diff_guard import DEFAULT_MAX_DIFF_BYTES, prepare_diff


def _file_diff(path: str, payload: str) -> str:
    lines = payload.splitlines() or [""]
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{line}\n" for line in lines)
    )


def test_default_generated_paths_are_excluded_before_size_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = _file_diff("f2i.bio/blog/entry/index.html", "x" * 100_000)
    raw += _file_diff("src/app.py", "print('ok')")

    prepared = prepare_diff(raw)

    assert prepared.excluded_files == ("f2i.bio/blog/entry/index.html",)
    assert "index.html" not in prepared.diff
    assert "src/app.py" in prepared.diff
    assert not prepared.over_limit


def test_per_file_stub_bounds_context_but_total_diff_cap_still_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = _file_diff("src/large.json", "x" * (DEFAULT_MAX_DIFF_BYTES + 1))

    prepared = prepare_diff(raw)

    assert prepared.over_limit
    assert prepared.stubbed_files[0][0] == "src/large.json"
    assert "Vigil omitted this file diff" in prepared.diff
    assert len(prepared.diff.encode("utf-8")) < 500


def test_repo_ignore_file_and_action_patterns_are_additive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".vigil").mkdir()
    (tmp_path / ".vigil" / "ignore").write_text("# local generated data\nreports/**/*.csv\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_DIFF_IGNORE", "vendor/**\n")
    raw = _file_diff("f2i.bio/assets/index.html", "small")
    raw += _file_diff("reports/monthly/rows.csv", "small")
    raw += _file_diff("vendor/generated.bin", "small")
    raw += _file_diff("src/keep.py", "small")

    prepared = prepare_diff(raw)

    assert prepared.excluded_files == (
        "f2i.bio/assets/index.html",
        "reports/monthly/rows.csv",
        "vendor/generated.bin",
    )
    assert "src/keep.py" in prepared.diff


def test_two_megabyte_diff_stops_before_model_and_posts_explanation(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.delenv("VIGIL_DIFF_IGNORE", raising=False)
    raw = _file_diff("src/bulk.txt", "x" * (2 * 1024 * 1024))
    monkeypatch.setattr(cli, "parse_pr_url", lambda _url: ("owner", "repo", 12))
    monkeypatch.setattr(cli, "get_pr_data", lambda *_args: {"title": "large", "diff": raw})
    comments = []
    monkeypatch.setattr(cli, "post_diff_limit_comment", lambda *args: comments.append(args) or True)
    monkeypatch.setattr(cli, "review_diff", lambda *_args, **_kwargs: pytest.fail("model call must not happen"))

    with pytest.raises(typer.Exit) as exc:
        cli.review(
            "https://github.com/owner/repo/pull/12",
            model="gemini/gemini-3.1-flash-lite",
            lead_model=None,
            profile="default",
            output_json=False,
            post=True,
            force=False,
            reason="",
        )

    assert exc.value.exit_code == 1
    assert len(comments) == 1
    assert "NO REVIEW WAS POSTED" in comments[0][-1]
    assert "Vigil diff exceeds configured limit" in capsys.readouterr().out


def test_generated_two_megabyte_diff_is_excluded_and_under_cap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIGIL_DIFF_IGNORE", raising=False)
    raw = _file_diff("f2i.bio/blog/entry/index.html", "x" * (2 * 1024 * 1024))
    raw += _file_diff("src/app.py", "print('ok')")
    prepared = prepare_diff(raw)

    assert prepared.excluded_files == ("f2i.bio/blog/entry/index.html",)
    assert prepared.filtered_bytes <= DEFAULT_MAX_DIFF_BYTES
    assert not prepared.over_limit


def test_cli_sends_filtered_generated_diff_to_review_when_under_cap(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.delenv("VIGIL_DIFF_IGNORE", raising=False)
    raw = _file_diff("f2i.bio/blog/entry/index.html", "x" * (2 * 1024 * 1024))
    raw += _file_diff("src/app.py", "print('ok')")
    pr_data = {
        "title": "generated site rebuild",
        "author": "contributor",
        "additions": 1,
        "deletions": 0,
        "changed_files": 2,
        "diff": raw,
    }
    monkeypatch.setattr(cli, "parse_pr_url", lambda _url: ("owner", "repo", 12))
    monkeypatch.setattr(cli, "get_pr_data", lambda *_args: pr_data)
    seen_diffs = []

    def stop_after_review(diff, *_args, **_kwargs):
        seen_diffs.append(diff)
        raise RuntimeError("test stop after the review boundary")

    monkeypatch.setattr(cli, "review_diff", stop_after_review)
    with pytest.raises(typer.Exit):
        cli.review(
            "https://github.com/owner/repo/pull/12",
            model="gemini/gemini-3.1-flash-lite",
            lead_model=None,
            profile="default",
            output_json=False,
            post=False,
            force=False,
            reason="",
        )

    assert len(seen_diffs) == 1
    assert "f2i.bio/blog/entry/index.html" not in seen_diffs[0]
    assert "src/app.py" in seen_diffs[0]
