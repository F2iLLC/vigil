"""Tests for issue #81 — a block the specialist table does not support.

F2iLLC/LunaOS#5082 drew ``vigil-reviewer: CHANGES_REQUESTED`` on a verdict of
**BLOCK** while its own table read *1/7 specialists approved · 0 rejections*.
Every specialist that actually ran had approved; the block, and the single
finding under it, were the lead's alone. The finding was false — the document
it complained about already said, in four separate places, exactly what the
finding asked it to say — and because Vigil cannot dismiss its own review, that
one wrong call cost two manual human actions and stalled the PR until someone
took them.

Two independent defects put it there, and the tests below are grouped to match.

- **The lead was exempt from validation** (``TestLeadFindingsCarryEvidence``).
  ``VERDICT_SCHEMA`` requires four evidence fields of every specialist —
  ``component``, ``predicate``, ``evidence_source``, ``evidence_commit`` — and
  every provenance-based suppression in ``finding_validation`` is gated on
  them. Both lead prompts carried their own hand-written copy of the finding
  schema, and neither copy listed any of the four. Lead findings therefore
  always parsed as ``evidence_source="unknown"``: the legacy fail-open value.
  The one reviewer that can block a PR with no specialist agreement was the one
  reviewer whose findings walked straight through the whole #74/#77 control
  path. The schema is now spliced from a single source, and the splice raises
  rather than shipping a prompt that forgot it.

- **A block that cites nothing still gated** (``TestAnUnsubstantiatedBlockDoesNotGate``).
  A blocking verdict is expensive precisely because Vigil cannot withdraw it.
  That price buys something when there is a finding under it. When no finding
  survives to be posted and no specialist objected either, it buys nothing and
  costs a human two actions, so the review reports instead of gating.

Two boundaries get their own class, because crossing either would be a worse
bug than the one being fixed:

- ``TestSubstantiatedBlocksStillBlock`` — a specialist REQUEST_CHANGES is
  independent evidence, and #74 deliberately kept it blocking even with every
  finding withheld. That is untouched. A lead block with a surviving finding is
  untouched. And ``result.decision`` is never rewritten: this changes the
  submitted event and the body, exactly as ``deblocked_stale_only`` does, so
  #79's reviewer-layer boundary ("a blocking lead verdict is never downgraded")
  still holds where it was drawn.
- ``TestTheVerdictNamesItsSource`` — the verdict and the table may no longer
  disagree in silence. This is attribution, not gating: it changes no event.

Mocking policy matches ``TestPostReviewOutcome`` and
``tests/test_nonblocking_no_inline_threads``: only the HTTP boundary is mocked,
so real placement, the real body builder and the real 422 ladder all run.
"""

from unittest.mock import MagicMock, patch

import pytest

from vigil import github_review
from vigil.finding_validation import (
    STALE_HISTORICAL_EVIDENCE,
    validate_findings_against_head,
)
from vigil.github_review import post_review
from vigil.models import Finding, PersonaVerdict, ReviewResult, Severity
from vigil.personas import (
    DEFAULT_PROFILE,
    ENTERPRISE_PROFILE,
    FINDING_EVIDENCE_FIELDS,
    VERDICT_SCHEMA,
    _finalize_lead_prompt,
    _with_evidence_fields,
)


SHA = "32a0422" + "a" * 33

# commentable_lines(DIFF) == {"docs/URS.md": {94, 95, 96}}. The lines are
# genuinely commentable, which is what makes "no inline comments" below an
# assertion about suppression rather than an accident of placement.
DIFF = """diff --git a/docs/URS.md b/docs/URS.md
--- a/docs/URS.md
+++ b/docs/URS.md
@@ -94,2 +94,3 @@
 | Reviewer | Decision |
+| Product Owner | APPROVED — revision 0.1 only |
 | Product Owner | PENDING — revision 0.2 whole matrix |
"""

LEAD_MESSAGE = "The approval table presents 0.1 as APPROVED in a 0.2 document"


# ---------- fixtures ----------

def _finding(message=LEAD_MESSAGE, line=95, severity=Severity.high, **kw):
    return Finding(
        file="docs/URS.md",
        line=line,
        severity=severity,
        category="coherence",
        message=message,
        **kw,
    )


def _approving_panel(n=3):
    """A populated specialist panel where nobody objected.

    #81's own shape: one specialist ran and approved, the rest were skipped as
    having no files in scope. ``reviewed=False`` rows still carry
    ``decision="APPROVE"`` — that is #66's settled contract and is what keeps a
    skipped domain from blocking — so a guard that read the decision alone
    would see agreement here. It must not.
    """
    panel = [PersonaVerdict(
        persona="DX", session_id="VGL-ce4d9a", decision="APPROVE",
        checks={"docs": "PASS"}, findings=[], observations=[],
    )]
    for i in range(n - 1):
        panel.append(PersonaVerdict(
            persona=f"Skipped{i}", session_id=f"VGL-skip{i}", decision="APPROVE",
            checks={}, findings=[], observations=[],
            reviewed=False, skip_reason="no_files_in_scope",
        ))
    return panel


def _objecting_panel():
    return [
        PersonaVerdict(
            persona="Security", session_id="VGL-sec001", decision="REQUEST_CHANGES",
            checks={}, findings=[], observations=[],
        ),
        *_approving_panel(2),
    ]


def _result(decision="BLOCK", verdicts=None, lead_findings=(), commit_sha=SHA):
    return ReviewResult(
        decision=decision,
        summary="Reviewed.",
        commit_sha=commit_sha,
        specialist_verdicts=_approving_panel() if verdicts is None else verdicts,
        lead_findings=list(lead_findings),
        observations=[],
    )


def _ok():
    resp = MagicMock(status_code=200, text="")
    resp.json.return_value = {"html_url": "https://github.com/o/r/pull/1#review"}
    return resp


def _payload(mock_post, index=0):
    return mock_post.call_args_list[index].kwargs["json"]


def _suppress_everything(monkeypatch):
    """Head validation that withholds every finding it is given.

    Stands in for "no finding survived to be posted" without pinning this test
    to any one suppression reason — the guard's condition is that nothing
    survived, not why.
    """
    from vigil.finding_validation import SuppressedFinding, STALE_FILE_ABSENT

    monkeypatch.setattr(
        github_review, "validate_findings_against_head",
        lambda findings, *a, **k: (
            [], [SuppressedFinding(f, STALE_FILE_ABSENT, f.file) for f in findings]
        ),
    )


# ---------- the lead was exempt from validation ----------

class TestLeadFindingsCarryEvidence:
    """The lead asks for the same evidence every specialist has to supply."""

    @pytest.mark.parametrize("profile", [DEFAULT_PROFILE, ENTERPRISE_PROFILE])
    @pytest.mark.parametrize("field", FINDING_EVIDENCE_FIELDS)
    def test_every_lead_prompt_requests_every_evidence_field(self, profile, field):
        assert f'"{field}"' in profile.lead_prompt

    @pytest.mark.parametrize("field", FINDING_EVIDENCE_FIELDS)
    def test_the_specialist_schema_still_requests_them(self, field):
        """Guards the test above from passing by weakening the other side."""
        assert f'"{field}"' in VERDICT_SCHEMA

    @pytest.mark.parametrize("profile", [DEFAULT_PROFILE, ENTERPRISE_PROFILE])
    def test_lead_prompts_are_told_what_a_lone_block_costs(self, profile):
        assert "SUBSTANTIATING A VERDICT NO SPECIALIST SUPPORTS" in profile.lead_prompt
        assert "CANNOT withdraw" in profile.lead_prompt

    @pytest.mark.parametrize("profile", [DEFAULT_PROFILE, ENTERPRISE_PROFILE])
    def test_no_placeholder_survives_into_a_shipped_prompt(self, profile):
        assert "__FINDING_EVIDENCE_FIELDS__" not in profile.lead_prompt
        assert "__LEAD_BLOCK_SUBSTANTIATION__" not in profile.lead_prompt

    def test_a_schema_that_forgets_the_fields_raises_at_import_time(self):
        """The failure mode being fixed must not be reachable silently.

        A prompt that quietly shipped without the evidence fields produced
        findings that still parsed, still posted and still blocked — while
        bypassing every validation stage. That has to be a startup error, not a
        review that fails open.
        """
        with pytest.raises(ValueError, match="__FINDING_EVIDENCE_FIELDS__"):
            _with_evidence_fields('{"findings": [{"file": "string"}]}')

    def test_a_lead_prompt_that_forgets_the_substantiation_rules_raises(self):
        with pytest.raises(ValueError, match="__LEAD_BLOCK_SUBSTANTIATION__"):
            _finalize_lead_prompt('{"findings": [__FINDING_EVIDENCE_FIELDS__]}')

    def test_the_unknown_provenance_a_lead_finding_used_to_carry_is_never_checked(self):
        """Why the missing fields mattered, stated as behavior.

        ``unknown`` is the legacy fail-open value. A finding carrying it — which
        is what EVERY lead finding carried, because the lead was never asked for
        anything else — cannot be suppressed on provenance grounds no matter
        what it claims.
        """
        legacy = _finding()  # evidence_source defaults to "unknown"
        assert legacy.evidence_source == "unknown"

        supported, suppressed = validate_findings_against_head(
            [legacy], "F2iLLC", "demo", SHA, "token",
            fetch_content=lambda *a: "unrelated content",
            commit_readable=lambda *a: True,
            diff_files=("docs/URS.md",),
        )
        assert supported == [legacy]
        assert suppressed == []

    def test_the_same_finding_is_suppressed_once_it_declares_its_source(self):
        """Guards the test above from passing for the wrong reason.

        Same message, same file, same head. The only difference is that the
        lead now supplies the provenance the schema asks for — and that is the
        difference between a finding the guard can act on and one it cannot.
        """
        declared = _finding(
            predicate="urs approval table conflates 0.1 with 0.2",
            component="docs/URS.md",
            evidence_source="historical_conversation",
        )

        supported, suppressed = validate_findings_against_head(
            [declared], "F2iLLC", "demo", SHA, "token",
            fetch_content=lambda *a: "unrelated content",
            commit_readable=lambda *a: True,
            diff_files=("docs/URS.md",),
        )
        assert supported == []
        assert [s.reason for s in suppressed] == [STALE_HISTORICAL_EVIDENCE]


# ---------- a block that cites nothing does not gate ----------

class TestAnUnsubstantiatedBlockDoesNotGate:

    @patch("vigil.github_review.httpx.post")
    def test_a_lead_block_with_no_findings_posts_as_a_comment(self, mock_post):
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1, _result("BLOCK"), "tok", diff=DIFF, outcome=outcome,
        )

        assert outcome["requested_event"] == "COMMENT"
        assert outcome["submitted_event"] == "COMMENT"
        assert "comments" not in _payload(mock_post)

    @patch("vigil.github_review.httpx.post")
    def test_request_changes_is_treated_the_same_way(self, mock_post):
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1, _result("REQUEST_CHANGES"), "tok", diff=DIFF,
            outcome=outcome,
        )

        assert outcome["submitted_event"] == "COMMENT"

    @patch("vigil.github_review.httpx.post")
    def test_a_lead_block_whose_findings_were_all_withheld_posts_as_a_comment(
        self, mock_post, monkeypatch,
    ):
        _suppress_everything(monkeypatch)
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1, _result("BLOCK", lead_findings=[_finding()]), "tok",
            diff=DIFF, outcome=outcome,
        )

        assert outcome["submitted_event"] == "COMMENT"
        assert "comments" not in _payload(mock_post)

    @patch("vigil.github_review.httpx.post")
    def test_the_withheld_verdict_is_named_in_the_body(self, mock_post):
        """Nothing is hidden. The review still says what it wanted to say."""
        mock_post.return_value = _ok()

        post_review("o", "r", 1, _result("BLOCK"), "tok", diff=DIFF)

        body = _payload(mock_post)["body"]
        assert "**BLOCK** verdict was withheld" in body
        assert "no specialist returned a blocking verdict" in body
        assert "Reviewed." in body  # the lead's own summary, unedited

    @patch("vigil.github_review.httpx.post")
    def test_a_withheld_verdict_opens_no_review_thread(self, mock_post, monkeypatch):
        """The half of the cost that a dismissal does not undo.

        Under ``required_review_thread_resolution`` an inline comment is a
        second manual action on top of dismissing the review. A verdict that
        does not gate must not leave one behind.
        """
        _suppress_everything(monkeypatch)
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1, _result("BLOCK", lead_findings=[_finding()]), "tok",
            diff=DIFF,
        )

        for payload in (c.kwargs.get("json", {}) for c in mock_post.call_args_list):
            assert not payload.get("comments")


# ---------- the boundaries ----------

class TestSubstantiatedBlocksStillBlock:
    """Four ways this fix could become a worse bug than the one it fixes."""

    @patch("vigil.github_review.httpx.post")
    def test_a_lead_block_with_a_surviving_finding_still_blocks(self, mock_post):
        """The lead reads the full diff and may object alone. It just has to
        point at something."""
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1, _result("BLOCK", lead_findings=[_finding()]), "tok",
            diff=DIFF, outcome=outcome,
        )

        assert outcome["submitted_event"] == "REQUEST_CHANGES"
        assert len(_payload(mock_post)["comments"]) == 1

    @patch("vigil.github_review.httpx.post")
    def test_a_specialist_objection_still_blocks_with_every_finding_withheld(
        self, mock_post, monkeypatch,
    ):
        """#74 settled this deliberately, and it stays settled.

        A specialist REQUEST_CHANGES is an independent verdict, not a restating
        of the findings under it. Losing the findings does not withdraw it.
        """
        _suppress_everything(monkeypatch)
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1,
            _result("BLOCK", verdicts=_objecting_panel(), lead_findings=[_finding()]),
            "tok", diff=DIFF, outcome=outcome,
        )

        assert outcome["submitted_event"] == "REQUEST_CHANGES"

    @patch("vigil.github_review.httpx.post")
    def test_a_profile_with_no_specialists_is_left_alone(self, mock_post):
        """"No specialist objected" says nothing when there was none to object.

        #79 settled this configuration on exactly that reasoning. #81's shape is
        a populated panel that disagreed with the verdict, and that is all this
        guard claims.
        """
        mock_post.return_value = _ok()
        outcome: dict = {}

        post_review(
            "o", "r", 1, _result("BLOCK", verdicts=[]), "tok", diff=DIFF,
            outcome=outcome,
        )

        assert outcome["submitted_event"] == "REQUEST_CHANGES"

    @patch("vigil.github_review.httpx.post")
    def test_the_verdict_itself_is_never_rewritten(self, mock_post):
        """The gate moves; the verdict does not.

        ``cli.py`` withdraws Vigil's own standing blocks (#48) and resolves its
        own threads (#61) only on an APPROVE that GitHub accepted as
        ``event=APPROVE``. Nothing here may reach those paths, so nothing here
        may turn a block into an approval — it can only decline to gate on one.
        """
        mock_post.return_value = _ok()
        result = _result("BLOCK")
        outcome: dict = {}

        post_review("o", "r", 1, result, "tok", diff=DIFF, outcome=outcome)

        assert result.decision == "BLOCK"
        assert outcome["submitted_event"] != "APPROVE"


# ---------- the verdict and the table may not disagree in silence ----------

class TestTheVerdictNamesItsSource:

    @patch("vigil.github_review.httpx.post")
    def test_a_block_no_specialist_supports_says_so(self, mock_post):
        """The reader of #5082 had a BLOCK header, a table of approvals and a
        footer reading "1/7 specialists approved", with nothing connecting
        them. Following this review's own documented advice produced the wrong
        conclusion."""
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1, _result("BLOCK", lead_findings=[_finding()]), "tok",
            diff=DIFF,
        )

        body = _payload(mock_post)["body"]
        assert "This verdict is the lead reviewer's alone" in body
        assert "1 of 3 ran" in body

    @patch("vigil.github_review.httpx.post")
    def test_a_block_a_specialist_supports_names_the_specialist(self, mock_post):
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1,
            _result("BLOCK", verdicts=_objecting_panel(), lead_findings=[_finding()]),
            "tok", diff=DIFF,
        )

        body = _payload(mock_post)["body"]
        assert "**Blocking specialists:** Security" in body
        assert "lead reviewer's alone" not in body

    @patch("vigil.github_review.httpx.post")
    def test_a_skipped_specialist_is_not_counted_as_an_objector(self, mock_post):
        """A skipped row carries ``decision="APPROVE"`` by #66's contract, and
        it is not agreement either way. It is counted as neither."""
        mock_post.return_value = _ok()

        post_review(
            "o", "r", 1, _result("BLOCK", lead_findings=[_finding()]), "tok",
            diff=DIFF,
        )

        body = _payload(mock_post)["body"]
        assert "Blocking specialists:" not in body

    @patch("vigil.github_review.httpx.post")
    def test_a_non_blocking_review_gets_no_attribution_line(self, mock_post):
        """Attribution answers "whose block is this?". There is no block."""
        mock_post.return_value = _ok()

        post_review("o", "r", 1, _result("APPROVE"), "tok", diff=DIFF)

        body = _payload(mock_post)["body"]
        assert "lead reviewer's alone" not in body
        assert "Blocking specialists:" not in body
