"""Validate findings against the content of the commit they cite (#74).

Every finding Vigil posts is stamped with the PR's head SHA, and that SHA is
written to GitHub as the review's ``commit_id``. Nothing checked that the file
and line it cites actually support the claim *at that SHA*. On
F2iLLC/LunaOS#4528 the gap produced seven CRITICAL "Cannot find namespace JSX"
findings across three pushes, each stamped with that push's correct head,
against files that already carried ``import type { JSX } from "react"`` at
every one of those commits — repo-wide CI typecheck passed the whole time. The
finding text described pre-rebase content; only the SHA was current, which is
the worst possible combination because the SHA is what makes it look verified.

This module is the guard. Its bias is asymmetric and deliberate:

  * A finding is suppressed ONLY on positive evidence of staleness.
  * Anything ambiguous keeps the finding — an unresolvable line, a message
    with no citable remedy, an unreadable blob, and *any* API failure at all.

The two error directions are not comparable, so they are not weighed equally.
A finding kept in error costs a reviewer one wrong comment, visible and
arguable. A finding suppressed in error lets a real defect through a merge
gate five repositories depend on, silently, leaving nothing behind to audit.
So this fails open on every path, and it never touches
``ReviewResult.decision``: a REQUEST_CHANGES that loses every one of its
findings still posts as REQUEST_CHANGES. Suppression changes what a review
*says*, never what it does — the same line #66 draws.
"""

import logging
import re
from dataclasses import dataclass
from typing import Callable

from .github import commit_is_readable, get_file_content_at_commit
from .models import Finding

log = logging.getLogger(__name__)

# Machine-stable suppression reasons. These are matched on and rendered, not
# read as prose, so they must stay stable even if the wording around them
# changes (same contract as the SKIP_* constants in models.py).
STALE_FILE_ABSENT = "file_absent_at_head"
STALE_FIX_ALREADY_PRESENT = "suggested_fix_already_present"

_REASON_TEXT: dict[str, str] = {
    STALE_FILE_ABSENT: "the cited file does not exist at the reviewed commit",
    STALE_FIX_ALREADY_PRESENT: (
        "the change this finding asks for is already present in the file at "
        "the reviewed commit"
    ),
}

# A remedy snippet shorter than this, or one with no code punctuation in it,
# is prose ("use parameterized queries") rather than the literal text a fixed
# file would contain. Searching for prose in source would either never match
# or match by accident, and only the accident is dangerous — so both are
# rejected before the search happens.
_MIN_SNIPPET_CHARS = 12

_FENCED_CODE = re.compile(r"```[\w+.-]*\r?\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_CODE_SHAPE = re.compile(r"[=(){}\[\];:<>.]")
_WHITESPACE = re.compile(r"\s+")

# Sentinel for "this blob could not be read", kept distinct from ``None``,
# which this module reads as the much stronger claim "GitHub says there is no
# such file at this commit".
_UNREADABLE = object()


@dataclass(frozen=True)
class SuppressedFinding:
    """A finding withheld from the review, with the evidence that withheld it.

    Carried out of ``validate_findings_against_head`` rather than logged and
    forgotten: a guard that silently deletes findings from a merge gate is
    indistinguishable from the bug it fixes. Every instance of this is
    rendered into the posted review body and logged.
    """

    finding: Finding
    reason: str
    evidence: str = ""

    @property
    def reason_text(self) -> str:
        """Human-readable reason, degrading to the raw constant if unknown."""
        return _REASON_TEXT.get(self.reason, self.reason)


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces, for content matching.

    Both sides of every comparison go through this, so indentation, line
    breaks and trailing whitespace cannot make a present line look absent.
    """
    return _WHITESPACE.sub(" ", text).strip()


def _is_code_like(snippet: str) -> bool:
    """True when a snippet is specific enough to search source for."""
    return len(snippet) >= _MIN_SNIPPET_CHARS and bool(_CODE_SHAPE.search(snippet))


def _remedy_snippets(finding: Finding) -> list[str]:
    """The code this finding asks for, from the one field that means that.

    Only ``suggestion`` is read, and only the code spans inside it — fenced
    blocks and backticked spans. That field is, by construction, what the file
    should contain once the finding is addressed, so finding it *already*
    there is direct evidence the finding describes content older than the
    commit it cites.

    The message is deliberately not mined the same way. A message cites the
    code it is complaining *about* ("missing null check on ``user.email``"),
    which is present at head precisely when the defect is real — mining it
    would invert the test and suppress live findings.
    """
    text = finding.suggestion or ""
    if not text.strip():
        return []

    fenced = _FENCED_CODE.findall(text)
    inline = _INLINE_CODE.findall(_FENCED_CODE.sub(" ", text))
    snippets = [_normalize(s) for s in (*fenced, *inline)]
    return [s for s in snippets if _is_code_like(s)]


def _already_applied_snippet(finding: Finding, content: str) -> str:
    """Return the remedy snippet already present at head, or "".

    Whole-file rather than near-the-cited-line, because the #74 shape is
    exactly a fix that lands somewhere other than where the finding points:
    the import goes at the top of the file, the finding cites the usage two
    hundred lines down.

    Residual false-suppression risk, stated rather than hidden: a finding that
    asks for the same change at several call sites is suppressed once any one
    of them has it. ``_MIN_SNIPPET_CHARS`` and the code-shape filter keep that
    to snippets specific enough to be meaningful, and the suppression is
    reported in the review body, so it is visible rather than silent.
    """
    haystack = _normalize(content)
    for snippet in _remedy_snippets(finding):
        if snippet in haystack:
            return snippet
    return ""


def validate_findings_against_head(
    findings: list[Finding],
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    fetch_content: Callable[[str, str, str, str, str], str | None] | None = None,
    commit_readable: Callable[[str, str, str, str], bool] | None = None,
) -> tuple[list[Finding], list[SuppressedFinding]]:
    """Split ``findings`` into (supported, suppressed) against head tree content.

    Two things count as positive evidence of staleness, and nothing else does:

    1. ``STALE_FILE_ABSENT`` — GitHub reports no such path at ``head_sha``,
       *and* a probe confirms the commit itself is readable with this token
       (the contents API returns the same 404 for a missing path and for a
       repository the credentials cannot see).
    2. ``STALE_FIX_ALREADY_PRESENT`` — the code the finding's own
       ``suggestion`` asks for is already in the file at ``head_sha``.

    Everything else keeps the finding, including several things that look like
    evidence and are not. A cited line past end-of-file is **not** grounds for
    suppression: it says the citation is misplaced, which Vigil already
    tolerates and repairs (``_place_finding_inline`` relocates findings to the
    nearest commentable line), and says nothing about whether the defect
    exists somewhere else in the file. Suppressing on it would drop real
    findings over an off-by-N line number, which models produce constantly.

    Args:
        findings: The findings about to be posted. Not mutated.
        owner: Repository owner.
        repo: Repository name.
        head_sha: The exact commit the findings are stamped with. An empty
            SHA disables validation entirely — there is nothing to check
            against, and guessing a ref would defeat the point.
        token: GitHub API token.
        fetch_content: Seam for the blob fetch, defaulting to
            ``github.get_file_content_at_commit``. Returns the file's text, or
            None when GitHub reports the path absent at that ref.
        commit_readable: Seam for the corroborating probe, defaulting to
            ``github.commit_is_readable``.

    Returns:
        ``(supported, suppressed)``. ``supported`` preserves input order and
        contains the same objects (identity is what the caller rebuilds
        verdicts by). ``suppressed`` carries each withheld finding with the
        reason and the evidence for it.
    """
    if not findings or not head_sha:
        return list(findings), []

    fetch = fetch_content or get_file_content_at_commit
    readable = commit_readable or commit_is_readable

    # Per (path, sha), so a review with a dozen findings in one file costs one
    # blob fetch. Deliberately call-scoped, not module-level: a cache that
    # outlived the call would serve one PR's head content to another run.
    cache: dict[tuple[str, str], object] = {}
    head_readable: bool | None = None

    supported: list[Finding] = []
    suppressed: list[SuppressedFinding] = []

    for finding in findings:
        key = (finding.file, head_sha)
        if key not in cache:
            try:
                cache[key] = fetch(owner, repo, finding.file, head_sha, token)
            except Exception as e:  # noqa: BLE001 — never fail a review over validation
                # Network failure, 403, rate limit, revoked token: all of them
                # mean "unverified", and unverified keeps the finding. The
                # cache holds the failure too, so an outage costs one call per
                # file rather than one per finding.
                log.warning(
                    "Could not read %s at %s (%s: %s) — keeping the finding "
                    "unvalidated",
                    finding.file, head_sha[:7], type(e).__name__, e,
                )
                cache[key] = _UNREADABLE

        content = cache[key]

        if content is _UNREADABLE:
            supported.append(finding)
            continue

        if content is None:
            if head_readable is None:
                try:
                    head_readable = readable(owner, repo, head_sha, token)
                except Exception as e:  # noqa: BLE001 — same fail-open contract
                    log.warning(
                        "Could not confirm %s is readable (%s: %s) — treating "
                        "every absent-file result as unverified",
                        head_sha[:7], type(e).__name__, e,
                    )
                    head_readable = False
            if not head_readable:
                supported.append(finding)
                continue
            suppressed.append(
                SuppressedFinding(finding, STALE_FILE_ABSENT, finding.file)
            )
            continue

        if not isinstance(content, str) or "\x00" in content:
            # A binary blob is not something a line-and-content claim can be
            # checked against. Unverified, so kept.
            supported.append(finding)
            continue

        snippet = _already_applied_snippet(finding, content)
        if snippet:
            suppressed.append(
                SuppressedFinding(finding, STALE_FIX_ALREADY_PRESENT, snippet)
            )
            continue

        supported.append(finding)

    for item in suppressed:
        log.info(
            "Suppressed finding at %s:%s [%s] — %s (evidence: %s)",
            item.finding.file, item.finding.line, item.finding.category,
            item.reason, item.evidence,
        )

    return supported, suppressed
