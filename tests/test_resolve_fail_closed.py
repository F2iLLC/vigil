"""resolve-addressed fails closed when GitHub refuses to resolve a thread.

Refs F2iLLC/vigil#158: on PR #139 GitHub answered ``resolveReviewThread`` with
``FORBIDDEN: Resource not accessible by integration`` and ``"data": null``.
Vigil swallowed an AttributeError, printed ``Auto-resolved 0`` and exited 0.

HTTP is mocked at ``httpx.post`` so the real ``_graphql`` runs; no network.
"""

import httpx
import pytest
from typer.testing import CliRunner

from vigil import cli, comment_manager
from vigil.comment_manager import (
    ThreadResolutionError,
    fetch_review_threads,
    resolve_addressed_threads,
    resolve_threads_batch,
)

FORBIDDEN = {
    "type": "FORBIDDEN",
    "path": ["t0"],
    "message": "Resource not accessible by integration",
}
PR_URL = "https://github.com/F2iLLC/demo/pull/1"


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "https://api.github.com/graphql")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req),
            )

    def json(self):
        return self._body


def serve(monkeypatch, *bodies, status=200):
    queue = list(bodies)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return FakeResponse(queue.pop(0) if len(queue) > 1 else queue[0], status)

    monkeypatch.setattr(comment_manager.httpx, "post", fake_post)
    return calls


# ---------- resolve_threads_batch ----------

def test_forbidden_null_data_raises_naming_error_and_thread(monkeypatch):
    serve(monkeypatch, {"data": None, "errors": [FORBIDDEN]})
    with pytest.raises(ThreadResolutionError) as exc:
        resolve_threads_batch(["PRRT_a"], "tok")
    msg = str(exc.value)
    assert "FORBIDDEN" in msg
    assert "Resource not accessible by integration" in msg
    assert "PRRT_a" in msg
    assert exc.value.failed == ["PRRT_a"]
    assert exc.value.resolved == []
    assert exc.value.errors == [FORBIDDEN]


def test_partial_failure_names_only_the_failed_thread(monkeypatch):
    serve(monkeypatch, {
        "data": {"t0": {"thread": {"id": "PRRT_ok", "isResolved": True}}, "t1": None},
        "errors": [dict(FORBIDDEN, path=["t1"])],
    })
    with pytest.raises(ThreadResolutionError) as exc:
        resolve_threads_batch(["PRRT_ok", "PRRT_bad"], "tok")
    assert exc.value.failed == ["PRRT_bad"]
    assert exc.value.resolved == ["PRRT_ok"]
    assert "PRRT_bad" in str(exc.value)
    assert "PRRT_ok" not in str(exc.value)


def test_http_error_is_a_failure_not_a_silent_warning(monkeypatch):
    serve(monkeypatch, {}, status=502)
    with pytest.raises(ThreadResolutionError) as exc:
        resolve_threads_batch(["PRRT_a"], "tok")
    assert exc.value.failed == ["PRRT_a"]


def test_fully_successful_batch_returns_all_ids(monkeypatch):
    serve(monkeypatch, {"data": {
        "t0": {"thread": {"id": "A", "isResolved": True}},
        "t1": {"thread": {"id": "B", "isResolved": True}},
    }})
    assert resolve_threads_batch(["A", "B"], "tok") == ["A", "B"]


def test_empty_batch_makes_no_call(monkeypatch):
    calls = serve(monkeypatch, {"data": None})
    assert resolve_threads_batch([], "tok") == []
    assert calls == []


# ---------- fetch_review_threads ----------

def test_fetch_review_threads_null_data_raises_clear_error(monkeypatch):
    serve(monkeypatch, {"data": None, "errors": [FORBIDDEN]})
    with pytest.raises(ThreadResolutionError) as exc:
        fetch_review_threads("F2iLLC", "demo", 1, "tok")
    assert "FORBIDDEN" in str(exc.value)


def test_resolve_addressed_threads_propagates(monkeypatch):
    threads_body = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{
            "id": "PRRT_a", "isResolved": False,
            "comments": {"nodes": [{"id": 1, "body": "x VGL-abc12345", "path": "a.py",
                                    "line": 3, "author": {"login": "vigil"}}]},
        }],
    }}}}}
    serve(monkeypatch, threads_body, {"data": None, "errors": [FORBIDDEN]})
    with pytest.raises(ThreadResolutionError):
        resolve_addressed_threads("F2iLLC", "demo", 1, "tok", {"a.py": {3}})


# ---------- CLI ----------

def wire_cli(monkeypatch, resolver):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(cli, "get_pr_data", lambda *a: {"head_sha": "b" * 40, "diff": ""})
    monkeypatch.setattr(cli, "get_last_reviewed_sha", lambda *a: "a" * 40)
    monkeypatch.setattr(cli, "resolve_dismissed_threads", lambda *a: 0)
    monkeypatch.setattr(cli, "get_changed_files_between_commits", lambda *a: ["a.py"])
    monkeypatch.setattr(cli, "_history_diverged", lambda *a: False)
    monkeypatch.setattr(cli, "commentable_lines", lambda diff: {"a.py": {3}})
    monkeypatch.setattr(cli, "resolve_addressed_threads", resolver)


def invoke():
    return CliRunner().invoke(cli.app, ["resolve-addressed", PR_URL])


def test_cli_forbidden_exits_nonzero_and_names_error(monkeypatch):
    def boom(*a):
        raise ThreadResolutionError(resolved=[], failed=["PRRT_a"], errors=[FORBIDDEN])

    wire_cli(monkeypatch, boom)
    result = invoke()
    assert result.exit_code != 0
    assert "FORBIDDEN" in result.output
    assert "PRRT_a" in result.output
    assert "Auto-resolved 0" not in result.output


def test_cli_forbidden_end_to_end_through_graphql(monkeypatch):
    wire_cli(monkeypatch, resolve_addressed_threads)
    threads_body = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{
            "id": "PRRT_a", "isResolved": False,
            "comments": {"nodes": [{"id": 1, "body": "x VGL-abc12345", "path": "a.py",
                                    "line": 3, "author": {"login": "vigil"}}]},
        }],
    }}}}}
    serve(monkeypatch, threads_body, {"data": None, "errors": [FORBIDDEN]})
    result = invoke()
    assert result.exit_code != 0
    assert "FORBIDDEN" in result.output
    assert "Auto-resolved 0" not in result.output


def test_cli_success_exits_zero_with_count(monkeypatch):
    wire_cli(monkeypatch, lambda *a: 2)
    result = invoke()
    assert result.exit_code == 0
    assert "Auto-resolved 2 addressed thread(s)" in result.output


def test_cli_nothing_to_resolve_exits_zero(monkeypatch):
    wire_cli(monkeypatch, lambda *a: 0)
    monkeypatch.setattr(cli, "get_last_reviewed_sha", lambda *a: None)
    result = invoke()
    assert result.exit_code == 0


# ---------- review command: auto-resolve failure does not change the outcome ----------

def test_review_outcome_unchanged_when_auto_resolve_raises(monkeypatch, capsys):
    from tests.test_rereview_and_dismissal import (
        FULL_DIFF, SHA_A, run_review, vigil_review, wire_review,
    )

    rec = wire_review(
        monkeypatch,
        reviews=[vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")],
        changed_files=["file_b.py"],
        decision="REQUEST_CHANGES",
    )

    def boom(*a, **k):
        rec.addressed_calls.append(a)
        raise ThreadResolutionError(resolved=["X"], failed=["PRRT_a"], errors=[FORBIDDEN])

    monkeypatch.setattr(cli, "resolve_addressed_threads", boom)
    run_review()
    out = capsys.readouterr().out
    assert len(rec.addressed_calls) == 1
    assert rec.review_diffs == [FULL_DIFF]
    assert rec.posted == ["REQUEST_CHANGES"]
    assert "FORBIDDEN" in out
    assert "PRRT_a" in out


# ---------- fetch_review_threads: partial data with errors fails closed ----------

def _node(tid, line=3):
    return {
        "id": tid, "isResolved": False,
        "comments": {"nodes": [{"id": 1, "body": "x VGL-abc12345", "path": "a.py",
                                "line": line, "author": {"login": "vigil"}}]},
    }


def _page(nodes, has_next=False, cursor=None, errors=None):
    body = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }}}}}
    if errors is not None:
        body["errors"] = errors
    return body


FORBIDDEN_PARTIAL = {"type": "FORBIDDEN", "message": "Resource not accessible by integration"}


def test_fetch_partial_data_with_errors_raises(monkeypatch):
    serve(monkeypatch, _page([_node("PRRT_a")], errors=[FORBIDDEN_PARTIAL]))
    with pytest.raises(ThreadResolutionError) as exc:
        fetch_review_threads("F2iLLC", "demo", 1, "tok")
    assert "FORBIDDEN" in str(exc.value)
    assert "Could not fetch review threads" in str(exc.value)


def test_fetch_second_page_partial_with_errors_raises(monkeypatch):
    serve(
        monkeypatch,
        _page([_node("PRRT_a")], has_next=True, cursor="c1"),
        _page([_node("PRRT_b")], errors=[FORBIDDEN_PARTIAL]),
    )
    with pytest.raises(ThreadResolutionError):
        fetch_review_threads("F2iLLC", "demo", 1, "tok")


def test_fetch_null_pull_request_without_errors_raises(monkeypatch):
    serve(monkeypatch, {"data": {"repository": {"pullRequest": None}}})
    with pytest.raises(ThreadResolutionError) as exc:
        fetch_review_threads("F2iLLC", "demo", 1, "tok")
    assert "Could not fetch review threads" in str(exc.value)


def test_fetch_null_review_threads_without_errors_raises(monkeypatch):
    serve(monkeypatch, {"data": {"repository": {"pullRequest": {"reviewThreads": None}}}})
    with pytest.raises(ThreadResolutionError):
        fetch_review_threads("F2iLLC", "demo", 1, "tok")


def test_fetch_clean_two_pages_returns_all_threads(monkeypatch):
    calls = serve(
        monkeypatch,
        _page([_node("PRRT_a"), _node("PRRT_b")], has_next=True, cursor="c1"),
        _page([_node("PRRT_c")]),
    )
    threads = fetch_review_threads("F2iLLC", "demo", 1, "tok")
    assert [t["id"] for t in threads] == ["PRRT_a", "PRRT_b", "PRRT_c"]
    assert len(calls) == 2
    assert calls[1]["variables"]["cursor"] == "c1"


def test_cli_partial_thread_fetch_with_errors_exits_one(monkeypatch):
    wire_cli(monkeypatch, resolve_addressed_threads)
    serve(
        monkeypatch,
        _page([_node("PRRT_a")], errors=[FORBIDDEN_PARTIAL]),
        {"data": {"t0": {"thread": {"id": "PRRT_a", "isResolved": True}}}},
    )
    result = invoke()
    assert result.exit_code == 1
    assert "Auto-resolved" not in result.output
