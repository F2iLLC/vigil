"""Tests for the head-content validation guard (F2iLLC/vigil#74).

The defect: on F2iLLC/LunaOS#4528 Vigil emitted seven CRITICAL "Cannot find
namespace JSX" findings across three pushes, each stamped with that push's
correct head SHA, against files that already carried
`import type { JSX } from "react"` at every one of those commits. The findings
described pre-rebase content while citing a correct-looking post-rebase SHA,
and nothing anywhere checked the two against each other — Vigil never fetched
file content at all.

The guard's bias is the thing these tests exist to pin down, and it is
asymmetric on purpose: suppress only on positive evidence, keep on anything
ambiguous, and keep on *every* error. A finding kept in error costs one wrong
comment; a finding suppressed in error lets a real defect through a merge gate
five repositories depend on. So the fail-open cases below are asserted
explicitly rather than left to follow from the happy path, and the wiring
tests assert that a REQUEST_CHANGES which loses every one of its findings
still posts as REQUEST_CHANGES — the guard must never be able to manufacture
a green verdict (the standing rule from #51, #53 and #66).
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from vigil import finding_validation, github_review
from vigil.finding_validation import (
    STALE_FILE_ABSENT,
    STALE_FIX_ALREADY_PRESENT,
    SuppressedFinding,
    validate_findings_against_head,
)
from vigil.github_review import post_review
from vigil.models import Finding, PersonaVerdict, ReviewResult, Severity


SHA = "0f1e2d3" + "c" * 33
OTHER_SHA = "9a8b7c6" + "d" * 33

JSX_PATH = "apps/web/src/components/AdminUserList.tsx"

# The file as it actually stood at every SHA Vigil cited on LunaOS#4528.
JSX_FILE_AT_HEAD = '''import type { JSX } from "react";
import { useState } from "react";

export function AdminUserList(): JSX.Element {
  const [users, setUsers] = useState<User[]>([]);
  return <ul>{users.map((u) => <li key={u.id}>{u.name}</li>)}</ul>;
}
'''

AUTH_PATH = "src/auth.py"
AUTH_FILE_AT_HEAD = '''def login(user):
    query = "SELECT * FROM users WHERE name = " + user
    return run(query)
'''


# ---------- fixtures ----------

def jsx_finding(**overrides) -> Finding:
    """The #74 finding: a fix the head tree already contains."""
    fields = {
        "file": JSX_PATH,
        "line": 4,
        "severity": Severity.critical,
        "category": "type-safety",
        "message": (
            "Cannot find namespace 'JSX'. The component's return type is "
            "JSX.Element but the JSX namespace is never imported, which fails "
            "typecheck under React 19."
        ),
        "suggestion": 'Add `import type { JSX } from "react";` at the top of the file.',
    }
    fields.update(overrides)
    return Finding(**fields)


def sql_finding(**overrides) -> Finding:
    """A live finding: the defect is still there at head."""
    fields = {
        "file": AUTH_PATH,
        "line": 2,
        "severity": Severity.high,
        "category": "SQL Injection",
        "message": "Concatenated SQL built from user input.",
        "suggestion": (
            "Use a parameterized query: "
            '`cursor.execute("SELECT * FROM users WHERE name = ?", (user,))`'
        ),
    }
    fields.update(overrides)
    return Finding(**fields)


def fetcher(files: dict[str, str | None], calls: list | None = None):
    """A fake blob fetch. A path mapped to None is absent at that commit."""
    def _fetch(owner, repo, path, ref, token):
        if calls is not None:
            calls.append((path, ref))
        if path not in files:
            return None
        return files[path]
    return _fetch


def readable(answer: bool = True):
    return lambda owner, repo, sha, token: answer


def validate(findings, *, files=None, calls=None, head_sha=SHA,
             fetch=None, commit_readable=None):
    return validate_findings_against_head(
        findings, "F2iLLC", "demo", head_sha, "token",
        fetch_content=fetch or fetcher(files or {}, calls),
        commit_readable=commit_readable or readable(True),
    )


# ---------- the file is not there at all ----------

class TestFileAbsentAtHead:

    def test_finding_on_a_file_absent_at_head_is_suppressed(self):
        f = sql_finding()
        supported, suppressed = validate([f], files={})

        assert supported == []
        assert [s.finding for s in suppressed] == [f]
        assert suppressed[0].reason == STALE_FILE_ABSENT

    def test_absence_is_not_believed_when_the_commit_is_unreadable(self):
        """A 404 from the contents API means either "no such path" or "no such
        repository, as far as your credentials go". Only the first is evidence
        about the file, so the second must keep the finding."""
        f = sql_finding()
        supported, suppressed = validate(
            [f], files={}, commit_readable=readable(False),
        )

        assert supported == [f]
        assert suppressed == []

    def test_absence_is_not_believed_when_the_probe_itself_fails(self):
        def boom(owner, repo, sha, token):
            raise httpx.ConnectError("DNS resolution failed")

        f = sql_finding()
        supported, suppressed = validate([f], files={}, commit_readable=boom)

        assert supported == [f]
        assert suppressed == []

    def test_the_probe_runs_at_most_once_per_review(self):
        probes: list[str] = []

        def counting_probe(owner, repo, sha, token):
            probes.append(sha)
            return True

        findings = [sql_finding(), jsx_finding(), sql_finding(line=3)]
        _, suppressed = validate(
            findings, files={}, commit_readable=counting_probe,
        )

        assert len(suppressed) == 3
        assert probes == [SHA]


# ---------- the defect is still there ----------

class TestLiveFindingsSurvive:

    def test_finding_whose_defect_is_still_at_head_is_kept(self):
        f = sql_finding()
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]
        assert suppressed == []

    def test_finding_with_no_suggestion_is_kept(self):
        """Nothing to look for is not evidence of anything."""
        f = sql_finding(suggestion=None)
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]

    def test_prose_suggestion_is_never_searched_for(self):
        """"Use parameterized queries" is advice, not the text a fixed file
        contains. A prose match would be an accident, and only accidents are
        dangerous here."""
        f = sql_finding(suggestion="Use parameterized queries instead")
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]

    def test_short_snippet_is_too_unspecific_to_conclude_from(self):
        f = sql_finding(suggestion="Add `x = 1;` here")
        supported, suppressed = validate(
            [f], files={AUTH_PATH: "def f():\n    x = 1;\n    return x\n"},
        )

        assert supported == [f]

    def test_message_code_spans_are_not_mined_for_the_remedy(self):
        """A message cites the code it is complaining *about*, which is present
        at head exactly when the defect is real. Mining it would invert the
        test and suppress live findings."""
        f = sql_finding(
            message='Concatenated SQL: `query = "SELECT * FROM users WHERE name = " + user`',
            suggestion=None,
        )
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]

    def test_binary_blob_leaves_the_finding_unvalidated(self):
        f = sql_finding(file="assets/logo.png")
        supported, suppressed = validate(
            [f], files={"assets/logo.png": "PNG\x00\x01\x02"},
        )

        assert supported == [f]


# ---------- the #74 scenario ----------

class TestFixAlreadyPresentAtHead:

    def test_the_lunaos_4528_shape_is_suppressed(self):
        """The reported defect, end to end: the finding demands an import the
        file has carried at every SHA it was ever stamped with."""
        f = jsx_finding()
        supported, suppressed = validate([f], files={JSX_PATH: JSX_FILE_AT_HEAD})

        assert supported == []
        assert suppressed[0].reason == STALE_FIX_ALREADY_PRESENT
        assert 'import type { JSX } from "react";' in suppressed[0].evidence

    def test_indentation_and_line_breaks_do_not_hide_the_fix(self):
        f = jsx_finding(
            suggestion=(
                "Add the namespace import:\n\n"
                '```tsx\nimport type { JSX }\n  from "react";\n```'
            ),
        )
        supported, suppressed = validate([f], files={JSX_PATH: JSX_FILE_AT_HEAD})

        assert supported == []
        assert suppressed[0].reason == STALE_FIX_ALREADY_PRESENT

    def test_a_fix_that_is_genuinely_missing_keeps_the_finding(self):
        without_import = JSX_FILE_AT_HEAD.split("\n", 1)[1]
        f = jsx_finding()
        supported, suppressed = validate([f], files={JSX_PATH: without_import})

        assert supported == [f]
        assert suppressed == []

    def test_a_differently_quoted_fix_is_not_assumed_present(self):
        """Conservative by construction: no match, no suppression."""
        f = jsx_finding(suggestion="Add `import type { JSX } from 'react';`")
        supported, suppressed = validate([f], files={JSX_PATH: JSX_FILE_AT_HEAD})

        assert supported == [f]


# ---------- ambiguity keeps the finding ----------

class TestAmbiguityKeepsTheFinding:

    def test_finding_with_no_line_is_kept(self):
        f = sql_finding(line=None)
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]

    def test_line_past_end_of_file_is_not_grounds_for_suppression(self):
        """A misplaced citation is not a stale finding. Vigil already relocates
        findings to the nearest commentable line, and models produce off-by-N
        line numbers constantly — suppressing on this would drop real defects."""
        f = sql_finding(line=9999)
        supported, suppressed = validate([f], files={AUTH_PATH: AUTH_FILE_AT_HEAD})

        assert supported == [f]

    def test_empty_head_sha_disables_validation_entirely(self):
        calls: list = []
        findings = [sql_finding(), jsx_finding()]
        supported, suppressed = validate(findings, files={}, calls=calls, head_sha="")

        assert supported == findings
        assert suppressed == []
        assert calls == []

    def test_no_findings_is_a_no_op(self):
        calls: list = []
        assert validate([], files={}, calls=calls) == ([], [])
        assert calls == []


# ---------- every API failure keeps the finding ----------

class TestFailsOpenOnEveryApiError:

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("connection refused"),
            httpx.TimeoutException("timed out"),
            httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock()),
            httpx.HTTPStatusError("429", request=MagicMock(), response=MagicMock()),
            httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock()),
            RuntimeError("something nobody predicted"),
        ],
        ids=["network", "timeout", "forbidden", "rate-limited", "server", "unexpected"],
    )
    def test_fetch_failure_keeps_the_finding(self, error):
        def boom(owner, repo, path, ref, token):
            raise error

        findings = [jsx_finding(), sql_finding()]
        supported, suppressed = validate(findings, fetch=boom)

        assert supported == findings
        assert suppressed == []

    def test_a_missing_token_cannot_silently_empty_a_review(self):
        """The 401 shape. Everything survives; nothing is quietly dropped."""
        def unauthorized(owner, repo, path, ref, token):
            raise httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock())

        findings = [jsx_finding()]
        supported, suppressed = validate(findings, fetch=unauthorized)

        assert supported == findings


# ---------- caching ----------

class TestBlobCaching:

    def test_same_path_and_sha_is_fetched_once(self):
        calls: list = []
        findings = [
            sql_finding(line=2),
            sql_finding(line=3, category="Error Handling"),
            sql_finding(line=2, severity=Severity.medium),
        ]
        validate(findings, files={AUTH_PATH: AUTH_FILE_AT_HEAD}, calls=calls)

        assert calls == [(AUTH_PATH, SHA)]

    def test_each_distinct_path_is_fetched_once(self):
        calls: list = []
        findings = [sql_finding(), jsx_finding(), sql_finding(line=3)]
        validate(
            findings,
            files={AUTH_PATH: AUTH_FILE_AT_HEAD, JSX_PATH: JSX_FILE_AT_HEAD},
            calls=calls,
        )

        assert calls == [(AUTH_PATH, SHA), (JSX_PATH, SHA)]

    def test_a_failed_fetch_is_not_retried_per_finding(self):
        calls: list = []

        def boom(owner, repo, path, ref, token):
            calls.append((path, ref))
            raise httpx.ConnectError("connection refused")

        findings = [sql_finding(), sql_finding(line=3)]
        supported, _ = validate(findings, fetch=boom, calls=calls)

        assert supported == findings
        assert len(calls) == 1

    def test_the_fetch_is_pinned_to_the_reviewed_commit(self):
        calls: list = []
        validate([sql_finding()], files={AUTH_PATH: AUTH_FILE_AT_HEAD},
                 calls=calls, head_sha=OTHER_SHA)

        assert calls == [(AUTH_PATH, OTHER_SHA)]


# ---------- the suppression record ----------

class TestSuppressionIsAuditable:

    def test_the_record_carries_the_finding_reason_and_evidence(self):
        f = jsx_finding()
        _, suppressed = validate([f], files={JSX_PATH: JSX_FILE_AT_HEAD})
        item = suppressed[0]

        assert isinstance(item, SuppressedFinding)
        assert item.finding is f
        assert item.reason == STALE_FIX_ALREADY_PRESENT
        assert item.evidence
        assert "already present" in item.reason_text

    def test_an_unknown_reason_degrades_to_the_raw_constant(self):
        item = SuppressedFinding(sql_finding(), "some_future_reason")
        assert item.reason_text == "some_future_reason"

    def test_supported_findings_keep_their_order_and_identity(self):
        a, b, c = sql_finding(), jsx_finding(), sql_finding(line=3)
        supported, _ = validate(
            [a, b, c], files={AUTH_PATH: AUTH_FILE_AT_HEAD, JSX_PATH: JSX_FILE_AT_HEAD},
        )

        assert [id(f) for f in supported] == [id(a), id(c)]


# ---------- wiring into post_review ----------

DIFF = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,2 +1,3 @@
 def login(user):
+    query = "SELECT * FROM users WHERE name = " + user
     return run(query)
"""


def _result(decision, findings=(), lead_findings=(), commit_sha=SHA):
    verdict = PersonaVerdict(
        persona="Security",
        session_id="VGL-abc123",
        decision="APPROVE" if decision == "APPROVE" else "REQUEST_CHANGES",
        checks={},
        findings=list(findings),
        observations=[],
    )
    return ReviewResult(
        decision=decision,
        summary="Reviewed.",
        commit_sha=commit_sha,
        specialist_verdicts=[verdict],
        lead_findings=list(lead_findings),
        observations=[],
    )


def _ok():
    resp = MagicMock(status_code=200, text="")
    resp.json.return_value = {"html_url": "https://github.com/o/r/pull/1#review"}
    return resp


@pytest.fixture
def real_guard(monkeypatch):
    """Undo conftest's stub so post_review runs the real guard.

    Only the blob fetch and the readability probe are faked, so the wiring,
    the verdict rebuild, the body composition and the fallback ladder are all
    the production code paths.
    """
    monkeypatch.setattr(
        github_review, "validate_findings_against_head",
        validate_findings_against_head,
    )

    def _install(files: dict[str, str], commit_readable: bool = True):
        monkeypatch.setattr(
            finding_validation, "get_file_content_at_commit", fetcher(files),
        )
        monkeypatch.setattr(
            finding_validation, "commit_is_readable", readable(commit_readable),
        )

    return _install


class TestPostReviewAppliesTheGuard:

    @patch("vigil.github_review.httpx.post")
    def test_a_stale_finding_is_not_posted_inline(self, mock_post, real_guard):
        real_guard({JSX_PATH: JSX_FILE_AT_HEAD})
        mock_post.return_value = _ok()
        result = _result("REQUEST_CHANGES", findings=[jsx_finding()])

        post_review("o", "r", 1, result, "tok", diff=DIFF)

        payload = mock_post.call_args_list[0].kwargs["json"]
        assert "comments" not in payload
        assert result.specialist_verdicts[0].findings == []

    @patch("vigil.github_review.httpx.post")
    def test_a_suppressed_finding_is_reported_not_deleted(self, mock_post, real_guard):
        real_guard({JSX_PATH: JSX_FILE_AT_HEAD})
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1, _result("REQUEST_CHANGES", findings=[jsx_finding()]),
            "tok", diff=DIFF,
        )

        body = mock_post.call_args_list[0].kwargs["json"]["body"]
        assert "Suppressed Findings (1 not supported" in body
        assert JSX_PATH in body
        assert "already present" in body

    @patch("vigil.github_review.httpx.post")
    def test_a_live_finding_still_lands_inline(self, mock_post, real_guard):
        real_guard({AUTH_PATH: AUTH_FILE_AT_HEAD})
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1, _result("REQUEST_CHANGES", findings=[sql_finding()]),
            "tok", diff=DIFF,
        )

        payload = mock_post.call_args_list[0].kwargs["json"]
        assert len(payload["comments"]) == 1
        assert "Suppressed Findings" not in payload["body"]

    @patch("vigil.github_review.httpx.post")
    def test_lead_findings_go_through_the_same_guard(self, mock_post, real_guard):
        real_guard({JSX_PATH: JSX_FILE_AT_HEAD})
        mock_post.return_value = _ok()
        result = _result("REQUEST_CHANGES", lead_findings=[jsx_finding()])

        post_review("o", "r", 1, result, "tok", diff=DIFF)

        assert result.lead_findings == []

    @patch("vigil.github_review.httpx.post")
    def test_the_guard_is_pinned_to_the_reviews_own_commit(self, mock_post, monkeypatch):
        seen: list[str] = []

        def recording(findings, owner, repo, head_sha, token, *a, **k):
            seen.append(head_sha)
            return list(findings), []

        monkeypatch.setattr(
            github_review, "validate_findings_against_head", recording,
        )
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1,
            _result("REQUEST_CHANGES", findings=[sql_finding()], commit_sha=OTHER_SHA),
            "tok", diff=DIFF,
        )

        assert seen == [OTHER_SHA]

    @patch("vigil.github_review.httpx.post")
    def test_a_guard_that_raises_posts_every_finding(self, mock_post, monkeypatch):
        """Fail open at the wiring level too, not only inside the guard."""
        def boom(*a, **k):
            raise RuntimeError("validation exploded")

        monkeypatch.setattr(github_review, "validate_findings_against_head", boom)
        mock_post.return_value = _ok()
        result = _result("REQUEST_CHANGES", findings=[sql_finding()])

        post_review("o", "r", 1, result, "tok", diff=DIFF)

        payload = mock_post.call_args_list[0].kwargs["json"]
        assert len(payload["comments"]) == 1
        assert result.specialist_verdicts[0].findings != []

    @patch("vigil.github_review.httpx.post")
    def test_no_commit_sha_means_no_validation(self, mock_post, monkeypatch):
        called: list = []

        def recording(findings, *a, **k):
            called.append(1)
            return list(findings), []

        monkeypatch.setattr(
            github_review, "validate_findings_against_head", recording,
        )
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1,
            _result("REQUEST_CHANGES", findings=[sql_finding()], commit_sha=""),
            "tok", diff=DIFF,
        )

        assert called == []


class TestTheGuardNeverManufacturesAGreenVerdict:
    """The constraint that outranks the fix itself.

    `cli.py` withdraws Vigil's own standing blocks (#48) and resolves its own
    open threads (#61) on exactly three conditions: the replacement review
    posted, this run's verdict is APPROVE, and GitHub accepted it as
    `event=APPROVE`. Suppressing findings must not reach any of those. It
    changes what a review *says*, never what it does — the same boundary #66
    draws for skipped specialists.
    """

    @patch("vigil.github_review.httpx.post")
    def test_losing_every_finding_leaves_the_verdict_blocking(self, mock_post, real_guard):
        real_guard({JSX_PATH: JSX_FILE_AT_HEAD})
        mock_post.return_value = _ok()
        result = _result("REQUEST_CHANGES", findings=[jsx_finding()])
        outcome: dict = {}

        post_review("o", "r", 1, result, "tok", diff=DIFF, outcome=outcome)

        payload = mock_post.call_args_list[0].kwargs["json"]
        assert payload["event"] == "REQUEST_CHANGES"
        assert outcome["requested_event"] == "REQUEST_CHANGES"
        # The two values cli.py's dismissal guards read. Neither may move.
        assert outcome["submitted_event"] == "REQUEST_CHANGES"
        assert result.decision == "REQUEST_CHANGES"

    @patch("vigil.github_review.httpx.post")
    def test_a_block_verdict_survives_total_suppression(self, mock_post, real_guard):
        real_guard({JSX_PATH: JSX_FILE_AT_HEAD})
        mock_post.return_value = _ok()
        result = _result("BLOCK", findings=[jsx_finding()])
        outcome: dict = {}

        post_review("o", "r", 1, result, "tok", diff=DIFF, outcome=outcome)

        assert result.decision == "BLOCK"
        assert outcome["submitted_event"] == "REQUEST_CHANGES"

    @patch("vigil.github_review.httpx.post")
    def test_an_unreachable_github_suppresses_nothing(self, mock_post, monkeypatch):
        """A validation outage must not empty a blocking review's findings."""
        monkeypatch.setattr(
            github_review, "validate_findings_against_head",
            validate_findings_against_head,
        )

        def offline(owner, repo, path, ref, token):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(finding_validation, "get_file_content_at_commit", offline)
        mock_post.return_value = _ok()
        result = _result("REQUEST_CHANGES", findings=[sql_finding()])

        post_review("o", "r", 1, result, "tok", diff=DIFF)

        payload = mock_post.call_args_list[0].kwargs["json"]
        assert len(payload["comments"]) == 1
        assert "Suppressed Findings" not in payload["body"]
