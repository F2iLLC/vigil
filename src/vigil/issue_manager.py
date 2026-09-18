"""GitHub issue creation for non-blocking observations.

Automatically creates GitHub issues for observations and deduplicates
against all existing open issues using Vigil's body marker.
"""

import difflib
import logging
import re

import httpx

from .models import Finding, ReviewResult, Severity
from .context_manager import stable_finding_key
from .github import get_default_branch, get_file_content_at_commit
from .utils import extract_message_content, github_headers, severity_emoji

log = logging.getLogger(__name__)

_PRIORITY_LABELS: dict[Severity, tuple[str, str, str]] = {
    Severity.critical: ("Critical Priority", "b60205", "P0 review finding — fix in the surfacing PR; if too large, tracked here and escalated"),
    Severity.high: ("High Priority", "d93f0b", "P1 review finding — fix in the surfacing PR; if too large, tracked here"),
    Severity.medium: ("Medium Priority", "fbca04", "P2 review finding — filed from review, not fixed in the surfacing PR"),
    Severity.low: ("Low Priority", "0e8a16", "P3 review finding — filed from review, not fixed in the surfacing PR"),
}

# Marker in issue body to identify Vigil-created issues
_VIGIL_ISSUE_MARKER = "<!-- vigil-observation -->"
_FINDING_KEY_PATTERN = re.compile(r"<!--\s*vigil-finding-key:\s*([a-f0-9]{24})\s*-->")

# Marker for an issue whose cited path was absent from the default branch when
# it was filed. Machine-readable so a triage pass can select these without
# parsing prose, and stable independently of the wording around it.
_NOT_ON_DEFAULT_BRANCH_MARKER = "<!-- vigil-not-on-default-branch -->"

# Where ``missing_from_default_branch`` memoizes the resolved default branch
# inside its per-path cache. A NUL byte cannot appear in a path, so this can
# never collide with one.
_BRANCH_CACHE_KEY = "\x00default-branch"


def priority_label_for(finding: Finding) -> str:
    """Return the priority label that corresponds to a finding's severity."""
    return _PRIORITY_LABELS[finding.severity][0]


def ensure_priority_label(owner: str, repo: str, token: str, severity: Severity) -> bool:
    """Create the severity's priority label if needed. Returns True if created or exists."""
    name, color, description = _PRIORITY_LABELS[severity]
    url = f"https://api.github.com/repos/{owner}/{repo}/labels"
    try:
        resp = httpx.post(
            url,
            headers=github_headers(token),
            json={
                "name": name,
                "color": color,
                "description": description,
            },
            timeout=10,
        )
        if resp.status_code in (201, 200):
            return True
        if resp.status_code == 422:
            # Already exists
            return True
        log.warning("Failed to create label: %d %s", resp.status_code, resp.text)
        return False
    except Exception as e:
        log.warning("Failed to create label: %s", e)
        return False


def missing_from_default_branch(
    owner: str,
    repo: str,
    token: str,
    path: str,
    cache: dict[str, str],
) -> str:
    """Return the default branch's name when ``path`` is absent from it, else ``""``.

    Observations are filed from a PR branch, but they land in the repository's
    backlog, where nothing records which tree state they were anchored to. A
    file that exists only on an unmerged branch therefore produces an issue
    citing a path that ``git ls-tree origin/main`` cannot resolve, and the body
    gives no hint of that (F2iLLC/vigil#97 — eight such issues from one
    bioqms-core PR review). This is the probe that lets the body say so.

    Answers only on **positive** evidence of absence. An unreachable
    repository, an unnamed default branch, or any API failure other than a 404
    for the path itself returns ``""``, i.e. "say nothing". The asymmetry is
    the same one ``finding_validation`` is built on and for the same reason:
    a missing annotation costs a triage pass one ``git ls-tree``, whereas a
    wrong one tells a reviewer that shipped code does not exist.

    ``cache`` is caller-owned and maps a path to this function's answer for it,
    so one review pays at most one API call per distinct cited path. It also
    carries the resolved branch name under :data:`_BRANCH_CACHE_KEY`, so the
    repository lookup happens once — and, because probing is lazy, not at all
    when every observation deduplicates against an existing issue.
    """
    if not path:
        return ""
    if path in cache:
        return cache[path]

    if _BRANCH_CACHE_KEY not in cache:
        try:
            cache[_BRANCH_CACHE_KEY] = get_default_branch(owner, repo, token)
        except Exception as e:
            log.warning("Could not resolve default branch, skipping annotation: %s", e)
            cache[_BRANCH_CACHE_KEY] = ""
    branch = cache[_BRANCH_CACHE_KEY]
    if not branch:
        return ""

    try:
        # A 404 here is attributable to the path: the default-branch lookup
        # above already succeeded, which proves the token can see this repo.
        absent = get_file_content_at_commit(owner, repo, path, branch, token) is None
    except Exception as e:
        log.warning("Could not check %s against %s, skipping annotation: %s", path, branch, e)
        cache[path] = ""
        return ""

    cache[path] = branch if absent else ""
    return cache[path]


def _build_issue_title(finding: Finding, persona: str) -> str:
    """Build a concise issue title."""
    msg = finding.message
    # Truncate message for title
    if len(msg) > 60:
        msg = msg[:57] + "..."
    return f"[Vigil/{persona}] {finding.category}: {msg}"


def _build_issue_body(
    finding: Finding,
    persona: str,
    pr_url: str = "",
    commit_sha: str = "",
    also_reported_by: list[tuple[str, str, str]] | None = None,
    absent_from_branch: str = "",
) -> str:
    """Build the GitHub issue body with full finding details.

    ``absent_from_branch`` is the default branch's name when the cited path
    does not resolve there, and ``""`` otherwise (including when that could not
    be determined — see :func:`missing_from_default_branch`). When set, the
    body says so up front. Without it the issue is indistinguishable from one
    describing shipped code, and the only way to tell them apart is to run
    ``git ls-tree`` against the default branch per issue — which is what
    F2iLLC/vigil#97 was filed about.

    The notice goes above ``### Finding`` for two reasons: it is the first
    thing a triage pass needs, and :func:`_match_finding_to_issue` reads that
    section back out for cross-run matching, so nothing may be added inside it.

    ``also_reported_by`` carries ``(persona, file, message)`` for the other
    specialists whose observations merged into this one. Rendering them is not
    decoration: cross-specialist merging groups by cited location as well as by
    semantic identity, so a group's members are differently worded and may raise
    genuinely different concerns about one place. Dropping their text would make
    the merge silent data loss (F2iLLC/vigil#96).

    They go in their own ``###`` section after ``### Finding`` and
    ``### Suggestion``, never inside them: :func:`_match_finding_to_issue` reads
    the Finding section back out and requires 0.85 similarity against the
    representative's message, so diluting it would break cross-run matching for
    exactly the issues this merging creates.
    """
    emoji = severity_emoji(finding.severity)
    loc = finding.file
    if finding.line:
        loc += f":{finding.line}"

    sections = [
        _VIGIL_ISSUE_MARKER,
        f"<!-- vigil-finding-key: {stable_finding_key(finding)} -->",
        f"## {emoji} {finding.priority} · {finding.severity.value.upper()} — {finding.category}\n",
        f"**File:** `{loc}`",
        f"**Reviewer:** {persona}",
    ]

    if pr_url:
        sections.append(f"**PR:** {pr_url}")
    if commit_sha:
        sections.append(f"**Commit:** `{commit_sha[:7]}`")

    if absent_from_branch:
        pr_ref = pr_url or "the reviewed pull request"
        sections.append(
            f"\n{_NOT_ON_DEFAULT_BRANCH_MARKER}\n"
            f"> [!IMPORTANT]\n"
            f"> **Not on `{absent_from_branch}`.** `{finding.file}` did not exist on the "
            f"default branch (`{absent_from_branch}`) when this issue was filed — it is "
            f"only on the branch reviewed in {pr_ref}, and the line number above resolves "
            f"against the reviewed commit, not against `{absent_from_branch}`.\n"
            f">\n"
            f"> Triage this against that PR, not as backlog. If the PR never merges, this "
            f"issue describes code that will never exist and should be closed unread."
        )

    sections.append(f"\n### Finding\n\n{finding.message}")

    if finding.suggestion:
        sections.append(f"\n### Suggestion\n\n{finding.suggestion}")

    if also_reported_by:
        also = [
            "\n### Also reported by\n",
            "Other specialists flagged the same location in this review. Their "
            "wording is kept verbatim, so this issue may cover more than one "
            "concern — split it if so.\n",
        ]
        for other_persona, other_file, other_message in also_reported_by:
            # A path that differs from the representative's is shown in
            # backticks so a later round citing that spelling still matches
            # this issue on the path check.
            if other_file and other_file != finding.file:
                also.append(
                    f"**{other_persona}** (`{other_file}`) — {other_message}\n"
                )
            else:
                also.append(f"**{other_persona}** — {other_message}\n")
        sections.append("\n".join(also))

    sections.append(
        "\n---\n"
        "*This issue was auto-created by [Vigil](https://github.com/F2iProject/vigil) "
        "from a non-blocking observation. It does not block the PR but should be tracked.*"
    )

    return "\n".join(sections)


def _fetch_all_issues(
    owner: str, repo: str, token: str,
) -> list[dict]:
    """Fetch all open issues, paginating through all pages.

    Returns a list of issue dicts from the GitHub API.
    """
    url: str | None = f"https://api.github.com/repos/{owner}/{repo}/issues"
    params: dict | None = {"state": "open", "per_page": "100"}
    all_issues: list[dict] = []

    try:
        with httpx.Client() as client:
            while url:
                resp = client.get(url, headers=github_headers(token), params=params, timeout=15)
                resp.raise_for_status()
                all_issues.extend(resp.json())
                # Follow Link: <url>; rel="next" for pagination
                link = resp.headers.get("Link", "")
                url = None
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                params = None  # params baked into Link URL on subsequent pages
    except Exception as e:
        log.warning("Failed to fetch existing issues: %s", e)

    return all_issues


def _match_finding_to_issue(
    finding: Finding,
    issues: list[dict],
) -> str | None:
    """Check if any existing issue matches a finding.

    Matches by:
    1. Vigil issue marker present in body
    2. File path appearing in the body
    3. Message similarity >= 0.85

    Returns the issue HTML URL if found, None otherwise.
    """
    finding_text = extract_message_content(finding.message)
    if not finding_text:
        return None
    finding_key = stable_finding_key(finding)

    for issue in issues:
        body = issue.get("body") or ""
        # Must be a Vigil-created issue
        if _VIGIL_ISSUE_MARKER not in body:
            continue
        key_match = _FINDING_KEY_PATTERN.search(body)
        if key_match and key_match.group(1) == finding_key:
            return issue.get("html_url")
        # Check file path match
        if f"`{finding.file}" not in body:
            continue
        # Extract and compare message content
        # The finding message is in the "### Finding" section
        finding_match = re.search(r"### Finding\s*\n\n(.+?)(?:\n###|\n---|$)", body, re.DOTALL)
        if not finding_match:
            continue
        existing_text = extract_message_content(finding_match.group(1))
        if not existing_text:
            continue
        ratio = difflib.SequenceMatcher(None, finding_text, existing_text).ratio()
        if ratio >= 0.85:
            return issue.get("html_url")

    return None


def find_existing_issue(
    owner: str,
    repo: str,
    token: str,
    finding: Finding,
    persona: str,
    existing_issues: list[dict] | None = None,
) -> str | None:
    """Check if an open Vigil-created issue already exists for this finding.

    Searches all open issues for the Vigil body marker, then matches by:
    1. File path appearing in the body
    2. Message similarity >= 0.85

    Args:
        owner: Repository owner.
        repo: Repository name.
        token: GitHub token.
        finding: The finding to check for duplicates.
        persona: Specialist persona name (unused in matching, kept for API compat).
        existing_issues: Pre-fetched list of open Vigil issues. If None, fetches them.

    Returns the issue HTML URL if found, None otherwise.
    """
    if existing_issues is None:
        existing_issues = _fetch_all_issues(owner, repo, token)
    return _match_finding_to_issue(finding, existing_issues)


def create_issue(
    owner: str,
    repo: str,
    token: str,
    finding: Finding,
    persona: str,
    pr_url: str = "",
    commit_sha: str = "",
    also_reported_by: list[tuple[str, str, str]] | None = None,
    absent_from_branch: str = "",
) -> str | None:
    """Create a GitHub issue for a finding. Returns the issue HTML URL or None on failure."""
    title = _build_issue_title(finding, persona)
    body = _build_issue_body(
        finding, persona, pr_url, commit_sha, also_reported_by,
        absent_from_branch=absent_from_branch,
    )

    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    try:
        resp = httpx.post(
            url,
            headers=github_headers(token),
            json={
                "title": title,
                "body": body,
                "labels": [priority_label_for(finding)],
            },
            timeout=15,
        )
        resp.raise_for_status()
        issue_url = resp.json().get("html_url", "")
        log.info("Created issue: %s", issue_url)
        return issue_url
    except Exception as e:
        log.warning("Failed to create issue: %s", e)
        return None


def create_issues_for_observations(
    owner: str,
    repo: str,
    token: str,
    result: ReviewResult,
    pr_url: str = "",
) -> list[tuple[Finding, str]]:
    """Create GitHub issues for all observations in the review result.

    Pre-fetches all existing open issues once to avoid N+1 API calls,
    then deduplicates each observation against the cache before creating.
    Groups observations with their source persona from specialist verdicts.

    An observation whose cited path does not resolve on the repository's
    default branch is filed with that fact stated in its body, so a triage
    pass can see in one read that the issue describes code which has not
    shipped (F2iLLC/vigil#97). See :func:`missing_from_default_branch` for
    when that check answers and when it stays silent.

    Args:
        owner: Repository owner.
        repo: Repository name.
        token: GitHub token.
        result: The full review result containing observations.
        pr_url: PR URL for context in the issue body.

    Returns list of (finding, issue_url) tuples.
    """
    if not result.observations:
        return []

    # Ensure every priority label that will be used exists before creating issues.
    for severity in {obs.severity for obs in result.observations}:
        ensure_priority_label(owner, repo, token, severity)

    # Pre-fetch all open issues once (avoids N+1 API calls)
    existing_issues = _fetch_all_issues(owner, repo, token)

    # Build persona lookup from observation_sources if available
    persona_map: dict[int, str] = {}
    if result.observation_sources:
        for persona_name, obs in result.observation_sources:
            persona_map[id(obs)] = persona_name

    # Fallback: map observations to personas from verdicts
    if not persona_map:
        for v in result.specialist_verdicts:
            for obs in v.observations:
                persona_map[id(obs)] = v.persona

    # What the other specialists in a merged group said, keyed on object
    # identity like persona_map above. Absent for an unmerged observation.
    also_reported_map: dict[int, list[tuple[str, str, str]]] = {
        id(consensus.observation): consensus.also_reported_by
        for consensus in result.observation_consensus
        if consensus.also_reported_by
    }

    # Default-branch answers for the paths this run actually files, memoized
    # across observations. Populated lazily, so a run whose observations all
    # deduplicate makes no extra API calls at all.
    default_branch_cache: dict[str, str] = {}

    issues: list[tuple[Finding, str]] = []
    created_by_key: dict[str, str] = {}
    for obs in result.observations:
        persona = persona_map.get(id(obs), "Vigil")
        finding_key = stable_finding_key(obs)

        if finding_key in created_by_key:
            issues.append((obs, created_by_key[finding_key]))
            continue

        # Check for existing issue using pre-fetched cache
        existing_url = _match_finding_to_issue(obs, existing_issues)
        if existing_url:
            log.info("Observation already tracked: %s", existing_url)
            issues.append((obs, existing_url))
            created_by_key[finding_key] = existing_url
            continue

        # Create new issue
        issue_url = create_issue(
            owner, repo, token, obs, persona,
            pr_url=pr_url,
            commit_sha=result.commit_sha,
            also_reported_by=also_reported_map.get(id(obs)),
            absent_from_branch=missing_from_default_branch(
                owner, repo, token, obs.file, default_branch_cache,
            ),
        )
        if issue_url:
            issues.append((obs, issue_url))
            created_by_key[finding_key] = issue_url

    return issues
