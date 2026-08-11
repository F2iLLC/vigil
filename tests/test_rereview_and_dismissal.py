"""Tests for the re-review gate (#49) and Vigil self-dismissal (#48).

These two issues are one defect with two halves. #49 is the trigger: the
review short-circuits on an unchanged head, so a PR can reach a state it
cannot leave. #48 is the action it enables: Vigil can issue REQUEST_CHANGES
but has no way to withdraw one.

Two real incidents shape the tests, and they differ in the way that matters:

  * F2iLLC/bioqms-core#1472 - the block at head was left OUTSTANDING.
  * F2iLLC/praxislms#263    - the block at head was DISMISSED, and the gate
                              still could not be reopened because
                              get_last_reviewed_sha returns a dismissed
                              review's commit_id all the same.

A trigger keyed only on "an outstanding CHANGES_REQUESTED exists" misses the
second; one keyed only on "no live review at head" misses the first. Both
shapes are asserted here.

Mocking policy: these tests mock the GitHub API boundary (_paginate for reads,
_dismiss_review / httpx for writes) and let the real selection, gating and
dismissal logic run. The one place a collaborator is faked - post_review's
`outcome` contract at the CLI level - is verified separately and for real
against the actual HTTP fallback ladder in TestPostReviewOutcome.
"""

import re
from types import SimpleNamespace

import httpx
import pytest
import typer
from unittest.mock import MagicMock, patch

from vigil import cli, comment_manager
from vigil.comment_manager import (
    dismiss_stale_vigil_blocks,
    get_vigil_review_state,
    has_settled_vigil_verdict_at,
    select_outstanding_vigil_blocks,
    _dismiss_review,
)
from vigil.github_review import post_review
from vigil.models import ReviewResult


SHA_A = "5e2c671" + "a" * 33
SHA_B = "e0db6c1" + "b" * 33
SHA_C = "c8b06b5" + "c" * 33

PR_URL = "https://github.com/F2iLLC/demo/pull/1"

# A two-file base-to-head diff. Re-review must always see BOTH files: the
# full PR diff against the base, never just what moved since the last review.
FULL_DIFF = """diff --git a/file_a.py b/file_a.py
--- a/file_a.py
+++ b/file_a.py
@@ -1,2 +1,3 @@
 import os
+import sys

diff --git a/file_b.py b/file_b.py
--- a/file_b.py
+++ b/file_b.py
@@ -1,2 +1,3 @@
 import json
+import time

"""


def vigil_review(review_id: int, state: str, commit_id: str, submitted_at: str) -> dict:
    """A Vigil-authored review record as the GitHub reviews API returns it."""
    return {
        "id": review_id,
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
        "user": {"login": "vigil-reviewer"},
        "body": "Reviewed by [Vigil] - AI-powered PR review\n\nSummary here.",
    }


def foreign_review(review_id: int, state: str, commit_id: str) -> dict:
    """A review by somebody other than Vigil (must never be selected)."""
    return {
        "id": review_id,
        "state": state,
        "commit_id": commit_id,
        "submitted_at": "2026-08-01T00:00:00Z",
        "user": {"login": "F2iProject"},
        "body": "Looks good to me.",
    }


def wire_review(
    monkeypatch,
    *,
    reviews: list[dict],
    changed_files: list[str],
    decision: str = "APPROVE",
    head_sha: str = SHA_C,
    submitted_event: str | None = None,
    omit_outcome: bool = False,
    resolved_threads: int = 0,
    dismiss_ok: bool = True,
):
    """Wire cli.review against fake GitHub I/O, returning a call recorder.

    `reviews` is the live list the fake API serves; the fake dismissal endpoint
    mutates it in place the way GitHub would, so multi-run sequences stay
    faithful.
    """
    rec = SimpleNamespace(review_diffs=[], dismissals=[], posted=[], reviews=reviews)

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(cli, "parse_pr_url", lambda pr_url: ("F2iLLC", "demo", 1))
    monkeypatch.setattr(
        cli, "get_pr_data",
        lambda *a: {
            "title": "A pull request",
            "author": "someone",
            "additions": 2,
            "deletions": 0,
            "changed_files": 2,
            "head_sha": head_sha,
            "diff": FULL_DIFF,
            "url": PR_URL,
        },
    )

    # --- GitHub read boundary. The real fetch_vigil_reviews signature filter,
    # the real state selection and the real gate all run on top of this. ---
    def fake_paginate(url, headers, params=None):
        if url.endswith("/reviews"):
            return list(reviews)
        return []

    monkeypatch.setattr(comment_manager, "_paginate", fake_paginate)

    monkeypatch.setattr(cli, "resolve_dismissed_threads", lambda *a: resolved_threads)
    monkeypatch.setattr(cli, "resolve_addressed_threads", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "get_changed_files_between_commits", lambda *a: list(changed_files))
    monkeypatch.setattr(cli, "fetch_all_vigil_comments", lambda *a: [])
    monkeypatch.setattr(cli, "write_audit_entry", lambda *a, **k: "/tmp/audit.db")
    monkeypatch.setattr(cli, "react", lambda *a: None)
    monkeypatch.setattr(cli, "remove_reaction", lambda *a: None)

    result = ReviewResult(
        decision=decision,
        summary="summary",
        commit_sha=head_sha,
        specialist_verdicts=[],
        lead_findings=[],
        observations=[],
    )

    def fake_review_diff(diff, pr_context, **kwargs):
        rec.review_diffs.append(diff)
        return result

    monkeypatch.setattr(cli, "review_diff", fake_review_diff)

    default_event = {
        "APPROVE": "APPROVE",
        "REQUEST_CHANGES": "REQUEST_CHANGES",
        "BLOCK": "REQUEST_CHANGES",
    }.get(decision, "COMMENT")

    def fake_post_review(owner, repo, pr_number, res, token, **kwargs):
        outcome = kwargs.get("outcome")
        if outcome is not None and not omit_outcome:
            outcome["requested_event"] = default_event
            outcome["submitted_event"] = submitted_event or default_event
        rec.posted.append(res.decision)
        return PR_URL + "#pullrequestreview-99"

    monkeypatch.setattr(cli, "post_review", fake_post_review)

    # --- GitHub write boundary for dismissals. Mutates the served review list
    # exactly as GitHub does: the review comes back with state DISMISSED. ---
    def fake_dismiss(owner, repo, pr_number, review_id, message, token):
        rec.dismissals.append((review_id, message))
        if not dismiss_ok:
            return False
        for r in reviews:
            if r.get("id") == review_id:
                r["state"] = "DISMISSED"
        return True

    monkeypatch.setattr(comment_manager, "_dismiss_review", fake_dismiss)
    return rec


def run_review(*, force: bool = False, reason: str = ""):
    """Invoke the CLI command with every option supplied explicitly.

    Calling a typer command directly leaves un-passed parameters as OptionInfo
    sentinels rather than values, so all of them are passed here.
    """
    return cli.review(
        PR_URL,
        model="gemini/gemini-3.1-flash-lite",
        lead_model=None,
        profile="default",
        output_json=False,
        post=True,
        force=force,
        reason=reason,
    )


# ---------- pure selection helpers ----------

class TestSelectOutstandingVigilBlocks:

    def test_selects_live_changes_requested(self):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        assert [r["id"] for r in select_outstanding_vigil_blocks(reviews)] == [1]

    def test_excludes_dismissed_block(self):
        """The praxislms#263 shape: dismissed, so nothing is standing."""
        reviews = [vigil_review(1, "DISMISSED", SHA_C, "2026-08-01T00:00:00Z")]
        assert select_outstanding_vigil_blocks(reviews) == []

    def test_excludes_approved_and_commented(self):
        reviews = [
            vigil_review(1, "APPROVED", SHA_A, "2026-08-01T00:00:00Z"),
            vigil_review(2, "COMMENTED", SHA_A, "2026-08-01T00:01:00Z"),
        ]
        assert select_outstanding_vigil_blocks(reviews) == []

    def test_state_matching_is_case_insensitive_and_null_safe(self):
        reviews = [
            {"id": 1, "state": "changes_requested", "commit_id": SHA_A},
            {"id": 2, "state": None, "commit_id": SHA_A},
            {"id": 3, "commit_id": SHA_A},
        ]
        assert [r["id"] for r in select_outstanding_vigil_blocks(reviews)] == [1]


class TestHasSettledVigilVerdictAt:

    def test_approved_at_head_is_settled(self):
        reviews = [vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")]
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is True

    def test_commented_at_head_is_settled(self):
        """A repo with no VIGIL_REVIEW_TOKEN only ever gets COMMENT reviews.

        Treating those as unsettled would re-review on every PR event forever,
        which is the cost regression this gate must not cause.
        """
        reviews = [vigil_review(1, "COMMENTED", SHA_C, "2026-08-01T00:00:00Z")]
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is True

    def test_dismissed_at_head_is_not_settled(self):
        reviews = [vigil_review(1, "DISMISSED", SHA_C, "2026-08-01T00:00:00Z")]
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is False

    def test_changes_requested_at_head_is_not_settled(self):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_C, "2026-08-01T00:00:00Z")]
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is False

    def test_approval_on_a_different_commit_does_not_settle_head(self):
        reviews = [vigil_review(1, "APPROVED", SHA_B, "2026-08-01T00:00:00Z")]
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is False

    def test_missing_head_sha_is_treated_as_settled(self):
        """Conservative on unknown data: skip rather than spend a review."""
        assert has_settled_vigil_verdict_at([], None) is True
        assert has_settled_vigil_verdict_at([], "") is True


class TestGetVigilReviewState:

    @patch("vigil.comment_manager._paginate")
    def test_ignores_non_vigil_reviews(self, mock_paginate):
        mock_paginate.return_value = [
            foreign_review(9, "CHANGES_REQUESTED", SHA_C),
            vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z"),
        ]
        state = get_vigil_review_state("o", "r", 1, "t", SHA_C)
        assert state.outstanding_blocks == []
        assert state.settled_verdict_at_head is True
        assert state.last_reviewed_sha == SHA_C

    @patch("vigil.comment_manager._paginate")
    def test_last_reviewed_sha_still_comes_from_a_dismissed_review(self, mock_paginate):
        """The exact root cause: state is collapsed away by SHA alone.

        get_vigil_review_state reports the same SHA, but carries the state
        that lets the caller tell the difference.
        """
        mock_paginate.return_value = [
            vigil_review(1, "DISMISSED", SHA_C, "2026-08-01T00:00:00Z"),
        ]
        state = get_vigil_review_state("o", "r", 1, "t", SHA_C)
        assert state.last_reviewed_sha == SHA_C
        assert state.settled_verdict_at_head is False

    @patch("vigil.comment_manager._paginate")
    def test_no_reviews_is_empty_state(self, mock_paginate):
        mock_paginate.return_value = []
        state = get_vigil_review_state("o", "r", 1, "t", SHA_C)
        assert state.last_reviewed_sha is None
        assert state.outstanding_blocks == []
        assert state.settled_verdict_at_head is True


# ---------- the pure gate ----------

class TestRereviewReasons:

    def _reasons(self, **kw):
        base = dict(
            forced=False,
            reason="",
            outstanding_blocks=[],
            settled_verdict_at_head=True,
            resolved_threads=0,
        )
        base.update(kw)
        return cli._rereview_reasons(**base)

    def test_ordinary_case_has_no_reasons(self):
        assert self._reasons() == []

    def test_forced(self):
        assert self._reasons(forced=True) != []

    def test_forced_records_the_reason_text(self):
        assert "on-demand" in self._reasons(forced=True, reason="on-demand")[0]

    def test_outstanding_block(self):
        assert self._reasons(outstanding_blocks=[{"id": 1}]) != []

    def test_no_settled_verdict_at_head(self):
        assert self._reasons(settled_verdict_at_head=False) != []

    def test_resolved_threads(self):
        assert self._reasons(resolved_threads=2) != []


# ---------- issue #49: the re-review gate, end to end through cli.review ----------

class TestRereviewGate:

    def test_ordinary_case_still_short_circuits(self, monkeypatch):
        """Cost guard. No new commits, no block, nobody asked -> skip."""
        rec = wire_review(
            monkeypatch,
            reviews=[vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")],
            changed_files=[],
        )
        with pytest.raises(typer.Exit) as exc:
            run_review()
        assert exc.value.exit_code == 0
        assert rec.review_diffs == [], "no review should have been run"
        assert rec.posted == []

    def test_comment_only_repo_still_short_circuits(self, monkeypatch):
        """Cost guard for repos with no VIGIL_REVIEW_TOKEN (COMMENT reviews)."""
        rec = wire_review(
            monkeypatch,
            reviews=[vigil_review(1, "COMMENTED", SHA_C, "2026-08-01T00:00:00Z")],
            changed_files=[],
        )
        with pytest.raises(typer.Exit) as exc:
            run_review()
        assert exc.value.exit_code == 0
        assert rec.review_diffs == []

    def test_explicit_request_reviews_an_unchanged_head(self, monkeypatch):
        """/vigil review produces a fresh verdict even at a settled head."""
        rec = wire_review(
            monkeypatch,
            reviews=[vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")],
            changed_files=[],
        )
        run_review(force=True, reason="on-demand /vigil review comment")
        assert rec.review_diffs == [FULL_DIFF]
        assert rec.posted == ["APPROVE"]

    def test_outstanding_block_reviews_without_new_commits(self, monkeypatch):
        """The bioqms-core#1472 shape: live CHANGES_REQUESTED at head."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_C, "2026-08-01T00:00:00Z")]
        rec = wire_review(monkeypatch, reviews=reviews, changed_files=[])
        run_review()
        assert rec.review_diffs == [FULL_DIFF]

    def test_dismissed_verdict_at_head_reviews_without_new_commits(self, monkeypatch):
        """The praxislms#263 shape, which a naive implementation gets wrong.

        Nothing is outstanding here - the block was dismissed - so a trigger
        keyed on "an outstanding CHANGES_REQUESTED exists" would not fire.
        The assertion below proves that premise before asserting the fix.
        """
        reviews = [vigil_review(1, "DISMISSED", SHA_C, "2026-08-01T00:00:00Z")]
        assert select_outstanding_vigil_blocks(reviews) == [], (
            "premise: the naive outstanding-block trigger would NOT fire here"
        )
        rec = wire_review(monkeypatch, reviews=reviews, changed_files=[])
        run_review()
        assert rec.review_diffs == [FULL_DIFF]

    def test_resolved_threads_since_last_review_trigger_rereview(self, monkeypatch):
        rec = wire_review(
            monkeypatch,
            reviews=[vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")],
            changed_files=[],
            resolved_threads=3,
        )
        run_review()
        assert rec.review_diffs == [FULL_DIFF]

    @pytest.mark.parametrize(
        "reviews,force,resolved",
        [
            ([vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")], True, 0),
            ([vigil_review(1, "CHANGES_REQUESTED", SHA_C, "2026-08-01T00:00:00Z")], False, 0),
            ([vigil_review(1, "DISMISSED", SHA_C, "2026-08-01T00:00:00Z")], False, 0),
            ([vigil_review(1, "APPROVED", SHA_C, "2026-08-01T00:00:00Z")], False, 2),
        ],
        ids=["forced", "outstanding-block", "dismissed-head", "threads-resolved"],
    )
    def test_every_rereview_path_uses_the_full_base_to_head_diff(
        self, monkeypatch, reviews, force, resolved,
    ):
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=[], resolved_threads=resolved,
        )
        run_review(force=force)
        assert len(rec.review_diffs) == 1
        reviewed = rec.review_diffs[0]
        assert reviewed == FULL_DIFF
        # Both files from the base-to-head diff, not just what moved last.
        assert "file_a.py" in reviewed
        assert "file_b.py" in reviewed

    def test_no_empty_commit_is_required_to_clear_a_stale_block(self, monkeypatch):
        """Regression for the forbidden workaround.

        The head SHA is identical across the blocking review and the re-review,
        and the changed-file set is empty - i.e. nothing was pushed - yet a
        fresh verdict lands and the block is withdrawn.
        """
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_C, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=[], head_sha=SHA_C,
            decision="APPROVE",
        )
        run_review()
        assert rec.review_diffs == [FULL_DIFF]
        assert rec.posted == ["APPROVE"]
        assert [d[0] for d in rec.dismissals] == [1]
        assert select_outstanding_vigil_blocks(reviews) == []


# ---------- issue #48: withdrawing Vigil's own block ----------

class TestApproveDismissesStaleBlocks:

    def test_approve_dismisses_prior_vigil_blocks(self, monkeypatch):
        reviews = [
            vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z"),
            vigil_review(2, "CHANGES_REQUESTED", SHA_B, "2026-08-01T01:00:00Z"),
        ]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"], decision="APPROVE",
        )
        run_review()
        assert [d[0] for d in rec.dismissals] == [1, 2]

    def test_dismissal_message_names_the_head_sha(self, monkeypatch):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="APPROVE", head_sha=SHA_C,
        )
        run_review()
        assert SHA_C in rec.dismissals[0][1]

    def test_request_changes_leaves_prior_verdicts_untouched(self, monkeypatch):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="REQUEST_CHANGES",
        )
        run_review()
        assert rec.dismissals == []
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_block_verdict_leaves_prior_verdicts_untouched(self, monkeypatch):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"], decision="BLOCK",
        )
        run_review()
        assert rec.dismissals == []

    def test_no_dismissal_when_review_degraded_to_comment(self, monkeypatch):
        """The most dangerous failure mode: a COMMENT review clears no block,
        so dismissing the old one would leave the PR completely unguarded."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="APPROVE", submitted_event="COMMENT",
        )
        run_review()
        assert rec.dismissals == []
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_no_dismissal_when_review_fell_back_to_an_issue_comment(self, monkeypatch):
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="APPROVE", submitted_event="ISSUE_COMMENT",
        )
        run_review()
        assert rec.dismissals == []

    def test_no_dismissal_when_the_outcome_is_unknown(self, monkeypatch):
        """Fails closed: an unpopulated outcome must not authorize a dismissal."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="APPROVE", omit_outcome=True,
        )
        run_review()
        assert rec.dismissals == []

    def test_no_dismissal_when_posting_the_replacement_raised(self, monkeypatch):
        """Never withdraw a block without a replacement verdict on record."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"], decision="APPROVE",
        )

        def boom(*a, **k):
            raise httpx.HTTPError("review submission failed")

        monkeypatch.setattr(cli, "post_review", boom)
        run_review()
        assert rec.dismissals == []
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_verdict_guard_holds_independently_of_the_event_guard(self, monkeypatch):
        """Defense in depth, guard 2 of 3.

        In production the verdict and the submitted event agree, so the event
        guard alone would mask a missing verdict guard. This forces the
        pathological pairing to prove the verdict check is load-bearing on its
        own: a REQUEST_CHANGES verdict must never dismiss a block, whatever
        GitHub reports about the event.
        """
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="REQUEST_CHANGES", submitted_event="APPROVE",
        )
        run_review()
        assert rec.dismissals == []
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_posted_guard_holds_independently_of_the_event_guard(self, monkeypatch):
        """Defense in depth, guard 1 of 3.

        A replacement verdict that did not land must not authorize withdrawing
        the old block, even if the outcome dict was already populated before
        the failure.
        """
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"], decision="APPROVE",
        )

        def populate_then_fail(owner, repo, pr_number, res, token, **kwargs):
            outcome = kwargs.get("outcome")
            if outcome is not None:
                outcome["requested_event"] = "APPROVE"
                outcome["submitted_event"] = "APPROVE"
            raise httpx.HTTPError("connection reset after submit")

        monkeypatch.setattr(cli, "post_review", populate_then_fail)
        run_review()
        assert rec.dismissals == []
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_rejected_dismissal_does_not_fail_the_review(self, monkeypatch):
        """Degrade safely: a 403 costs a stale block, never the review."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"],
            decision="APPROVE", dismiss_ok=False,
        )
        run_review()  # must not raise
        assert [d[0] for d in rec.dismissals] == [1]
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    def test_unexpected_dismissal_error_does_not_fail_the_review(self, monkeypatch):
        """Even an exception escaping the helper must not fail a posted review."""
        reviews = [vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z")]
        rec = wire_review(
            monkeypatch, reviews=reviews, changed_files=["file_a.py"], decision="APPROVE",
        )

        def boom(*a, **k):
            raise httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())

        monkeypatch.setattr(cli, "dismiss_stale_vigil_blocks", boom)
        run_review()  # must not raise
        assert rec.posted == ["APPROVE"]


class TestStaleDismissalResurrection:
    """Issue #48's headline sequence: block at A -> approve at B -> push C.

    Before the fix, the branch rule `dismiss_stale_reviews_on_push` dropped the
    APPROVE from B on any push and left the block from A untouched, so the
    block became the latest live review again and re-blocked the PR on a
    verdict Vigil had already retracted.
    """

    def test_block_cannot_resurrect_after_an_approval_is_stale_dismissed(self, monkeypatch):
        reviews: list[dict] = []

        # --- Round 1: head A. Vigil blocks. ---
        wire_review(monkeypatch, reviews=reviews, changed_files=["file_a.py"],
                    decision="REQUEST_CHANGES", head_sha=SHA_A)
        run_review()
        reviews.append(vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z"))
        assert [r["id"] for r in select_outstanding_vigil_blocks(reviews)] == [1]

        # --- Round 2: author pushes B. Vigil re-reviews and approves. ---
        rec2 = wire_review(monkeypatch, reviews=reviews, changed_files=["file_a.py"],
                           decision="APPROVE", head_sha=SHA_B)
        run_review()
        # The approval landed, and the block from A was withdrawn by its author.
        assert [d[0] for d in rec2.dismissals] == [1]
        assert reviews[0]["state"] == "DISMISSED"
        reviews.append(vigil_review(2, "APPROVED", SHA_B, "2026-08-01T02:00:00Z"))

        # --- Round 3: anything pushes C. GitHub stale-dismisses APPROVALS
        #     only - exactly the behavior that used to resurrect the block. ---
        for r in reviews:
            if r["state"] == "APPROVED":
                r["state"] = "DISMISSED"

        assert select_outstanding_vigil_blocks(reviews) == [], (
            "the retracted block must not come back as the latest live review"
        )

        # And the PR is not left stranded either: with no settled verdict at C,
        # the gate reopens instead of skipping.
        assert has_settled_vigil_verdict_at(reviews, SHA_C) is False
        rec3 = wire_review(monkeypatch, reviews=reviews, changed_files=[],
                           decision="APPROVE", head_sha=SHA_C)
        run_review()
        assert rec3.review_diffs == [FULL_DIFF]


# ---------- the dismissal seam itself ----------

def _resp(status_code=200, text=""):
    """A mock response that raises from raise_for_status on 4xx/5xx.

    Matching real httpx behavior matters here: a mock whose raise_for_status
    silently succeeds would hide an implementation that calls it instead of
    inspecting status_code, which is exactly the not-degrading-safely bug
    these tests exist to catch.
    """
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status_code), request=MagicMock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestDismissReviewSeam:

    @patch("vigil.comment_manager.httpx.put")
    def test_calls_the_dismissals_endpoint(self, mock_put):
        mock_put.return_value = _resp(200)
        assert _dismiss_review("o", "r", 7, 4242, "superseded", "tok") is True
        url = mock_put.call_args[0][0]
        assert url == (
            "https://api.github.com/repos/o/r/pulls/7/reviews/4242/dismissals"
        )
        assert mock_put.call_args.kwargs["json"] == {
            "message": "superseded", "event": "DISMISS",
        }

    @patch("vigil.comment_manager.httpx.put")
    def test_403_returns_false_without_raising(self, mock_put):
        """Self-dismissal permission is UNVERIFIED; a refusal must be survivable."""
        mock_put.return_value = _resp(403, "Forbidden")
        assert _dismiss_review("o", "r", 7, 1, "m", "tok") is False

    @patch("vigil.comment_manager.httpx.put")
    def test_422_returns_false_without_raising(self, mock_put):
        mock_put.return_value = _resp(422, "Unprocessable")
        assert _dismiss_review("o", "r", 7, 1, "m", "tok") is False

    @patch("vigil.comment_manager.httpx.put")
    def test_network_error_returns_false_without_raising(self, mock_put):
        mock_put.side_effect = httpx.ConnectError("no route to host")
        assert _dismiss_review("o", "r", 7, 1, "m", "tok") is False

    @patch("vigil.comment_manager.httpx.put")
    def test_does_not_retry(self, mock_put):
        mock_put.return_value = _resp(403)
        _dismiss_review("o", "r", 7, 1, "m", "tok")
        assert mock_put.call_count == 1


class TestDismissStaleVigilBlocks:

    @patch("vigil.comment_manager._dismiss_review")
    def test_selects_only_outstanding_blocks(self, mock_dismiss):
        mock_dismiss.return_value = True
        reviews = [
            vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z"),
            vigil_review(2, "APPROVED", SHA_B, "2026-08-01T01:00:00Z"),
            vigil_review(3, "DISMISSED", SHA_A, "2026-08-01T02:00:00Z"),
            vigil_review(4, "COMMENTED", SHA_B, "2026-08-01T03:00:00Z"),
        ]
        assert dismiss_stale_vigil_blocks("o", "r", 1, "t", "msg", reviews=reviews) == [1]

    @patch("vigil.comment_manager._dismiss_review")
    def test_continues_past_a_rejected_dismissal(self, mock_dismiss):
        mock_dismiss.side_effect = [False, True]
        reviews = [
            vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z"),
            vigil_review(2, "CHANGES_REQUESTED", SHA_B, "2026-08-01T01:00:00Z"),
        ]
        assert dismiss_stale_vigil_blocks("o", "r", 1, "t", "msg", reviews=reviews) == [2]
        assert mock_dismiss.call_count == 2

    @patch("vigil.comment_manager.fetch_vigil_reviews")
    def test_fetch_failure_degrades_to_no_op(self, mock_fetch):
        mock_fetch.side_effect = httpx.HTTPError("boom")
        assert dismiss_stale_vigil_blocks("o", "r", 1, "t", "msg") == []

    @patch("vigil.comment_manager._paginate")
    @patch("vigil.comment_manager._dismiss_review")
    def test_ignores_non_vigil_blocks(self, mock_dismiss, mock_paginate):
        mock_dismiss.return_value = True
        mock_paginate.return_value = [
            foreign_review(9, "CHANGES_REQUESTED", SHA_C),
            vigil_review(1, "CHANGES_REQUESTED", SHA_A, "2026-08-01T00:00:00Z"),
        ]
        assert dismiss_stale_vigil_blocks("o", "r", 1, "t", "msg") == [1]

    @patch("vigil.comment_manager._dismiss_review")
    def test_skips_records_without_an_id(self, mock_dismiss):
        mock_dismiss.return_value = True
        assert dismiss_stale_vigil_blocks(
            "o", "r", 1, "t", "msg", reviews=[{"state": "CHANGES_REQUESTED"}],
        ) == []
        assert mock_dismiss.call_count == 0


# ---------- post_review's outcome contract, against the real fallback ladder ----------

class TestPostReviewOutcome:
    """Verifies for real what the CLI-level tests take on contract.

    Only the HTTP boundary is mocked; the actual 422 fallback ladder runs.
    """

    def _result(self, decision="APPROVE"):
        return ReviewResult(
            decision=decision, summary="s", commit_sha=SHA_C,
            specialist_verdicts=[], lead_findings=[], observations=[],
        )

    @patch("vigil.github_review.httpx.post")
    def test_accepted_approve_records_approve(self, mock_post):
        ok = MagicMock(status_code=200, text="")
        ok.json.return_value = {"html_url": "u"}
        mock_post.return_value = ok

        outcome: dict = {}
        post_review("o", "r", 1, self._result(), "tok", outcome=outcome)
        assert outcome["requested_event"] == "APPROVE"
        assert outcome["submitted_event"] == "APPROVE"

    @patch("vigil.github_review.httpx.post")
    def test_degradation_to_comment_is_recorded(self, mock_post):
        """422 on APPROVE (no write access) -> retried as COMMENT."""
        rejected = MagicMock(status_code=422, text="not permitted")
        ok = MagicMock(status_code=200, text="")
        ok.json.return_value = {"html_url": "u"}
        mock_post.side_effect = [rejected, ok]

        outcome: dict = {}
        post_review("o", "r", 1, self._result(), "tok", outcome=outcome)
        assert outcome["requested_event"] == "APPROVE"
        assert outcome["submitted_event"] == "COMMENT"
        # The second attempt really did go out as a COMMENT review.
        assert mock_post.call_args_list[1].kwargs["json"]["event"] == "COMMENT"

    @patch("vigil.github_review.httpx.post")
    def test_issue_comment_fallback_is_recorded(self, mock_post):
        rejected = MagicMock(status_code=422, text="nope")
        ok = MagicMock(status_code=200, text="")
        ok.json.return_value = {"html_url": "u"}
        mock_post.side_effect = [rejected, rejected, ok]

        outcome: dict = {}
        post_review("o", "r", 1, self._result(), "tok", outcome=outcome)
        assert outcome["submitted_event"] == "ISSUE_COMMENT"
        assert "/issues/1/comments" in mock_post.call_args_list[2].args[0]

    @patch("vigil.github_review.httpx.post")
    def test_request_changes_is_recorded(self, mock_post):
        ok = MagicMock(status_code=200, text="")
        ok.json.return_value = {"html_url": "u"}
        mock_post.return_value = ok

        outcome: dict = {}
        post_review("o", "r", 1, self._result("REQUEST_CHANGES"), "tok", outcome=outcome)
        assert outcome["submitted_event"] == "REQUEST_CHANGES"

    @patch("vigil.github_review.httpx.post")
    def test_outcome_is_optional(self, mock_post):
        ok = MagicMock(status_code=200, text="")
        ok.json.return_value = {"html_url": "u"}
        mock_post.return_value = ok
        assert post_review("o", "r", 1, self._result(), "tok") == "u"


# ---------- workflow plumbing for the explicit-request signal ----------

class TestExplicitRequestPlumbing:
    """The --force signal is useless unless it reaches the CLI (issue #49)."""

    def _read(self, name):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / name).read_text()

    def test_action_declares_force_and_reason_inputs(self):
        action = self._read("action.yml")
        assert re.search(r"^  force:", action, re.MULTILINE)
        assert re.search(r"^  reason:", action, re.MULTILINE)

    def test_action_forwards_force_and_reason_to_the_cli(self):
        action = self._read("action.yml")
        assert "ARGS+=(--force)" in action
        assert 'ARGS+=(--reason "$VIGIL_REASON")' in action
        # Passed via env, not interpolated into the shell script body.
        assert "VIGIL_FORCE: ${{ inputs.force }}" in action
        assert "VIGIL_REASON: ${{ inputs.reason }}" in action

    def test_reusable_workflow_forces_on_the_issue_comment_trigger(self):
        wf = self._read(".github/workflows/reusable-vigil.yml")
        assert "force=true" in wf
        assert "force=false" in wf
        assert "force: ${{ steps.pr.outputs.force }}" in wf
        assert "reason: ${{ steps.pr.outputs.reason }}" in wf


class TestWebhookForcesOnDemandReviews:
    """The webhook is the second entry point that serves '/vigil review'."""

    def _cmd(self, **kwargs):
        from vigil import webhook
        captured = {}

        class FakeCompleted:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return FakeCompleted()

        with patch("subprocess.run", fake_run):
            webhook._run_review("https://x/pull/1", "m", None, "default", **kwargs)
        return captured["cmd"]

    def test_forwards_force_and_reason(self):
        cmd = self._cmd(force=True, reason="on-demand /vigil review comment")
        assert "--force" in cmd
        assert cmd[cmd.index("--reason") + 1] == "on-demand /vigil review comment"

    def test_omits_force_for_ordinary_events(self):
        assert "--force" not in self._cmd()

    def _dispatch(self, event: str, payload: dict):
        """POST a webhook event and return the args _run_review was given."""
        from fastapi.testclient import TestClient
        from vigil import webhook

        captured = {}

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                captured["target"] = target
                captured["args"] = args

            def start(self):
                pass

        with patch.object(webhook.threading, "Thread", FakeThread):
            client = TestClient(webhook.create_app(webhook_secret=""))
            client.post("/webhook", json=payload, headers={"X-GitHub-Event": event})
        return captured.get("args")

    def test_slash_command_comment_dispatches_a_forced_review(self):
        args = self._dispatch("issue_comment", {
            "action": "created",
            "comment": {"body": "/vigil review"},
            "issue": {
                "number": 1,
                "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
                "html_url": "https://github.com/o/r/issues/1",
            },
        })
        assert args is not None, "no review was dispatched"
        assert args[4] is True, "the on-demand request must be forced"

    def test_pull_request_event_dispatches_an_unforced_review(self):
        args = self._dispatch("pull_request", {
            "action": "opened",
            "pull_request": {
                "draft": False,
                "user": {"type": "User"},
                "html_url": "https://github.com/o/r/pull/1",
            },
        })
        assert args is not None, "no review was dispatched"
        assert args[4] is False, "an ordinary PR event must not be forced"
