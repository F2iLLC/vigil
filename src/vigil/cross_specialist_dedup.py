"""Cross-specialist finding deduplication — merge overlapping findings in same round.

When multiple specialists flag the same code issue at the same location,
Vigil merges them into a single comment showing which specialists flagged it.
This prevents review spam while showing consensus.
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple

from .context_manager import (
    FindingFingerprint,
    find_cross_specialist_duplicates,
    fingerprint_finding,
    normalize_line_range,
    stable_finding_key,
)
from .models import Finding, PersonaVerdict, Severity
from .utils import (
    sanitize_markdown,
    severity_emoji,
    validate_session_id,
    validate_specialist_name,
)

log = logging.getLogger(__name__)


@dataclass
class VerdictInfo:
    """Verdict info for a specialist on a merged finding."""

    specialist: str
    verdict: str  # APPROVE | REQUEST_CHANGES
    category: str  # The category label this specialist used for the finding
    session_id: str = ""


class MergedFinding(NamedTuple):
    """A finding that was flagged by multiple specialists, now merged."""

    finding: Finding  # Representative finding (highest severity)
    specialists: list[str]  # List of specialist names who flagged it
    count: int  # Number of specialists who flagged it (len(specialists))
    original_findings: list[Finding]  # All original findings before merge
    verdict_info: list[VerdictInfo] = []  # Verdict details for each specialist


def merge_specialist_findings(
    verdicts: list[PersonaVerdict],
) -> tuple[list[Finding], list[MergedFinding]]:
    """Merge findings from multiple specialists, grouping overlapping issues.

    When specialists flag the same affected component and defect predicate,
    they're merged even if wording, category, line, or anchor differs.

    Args:
        verdicts: List of PersonaVerdict objects from specialists

    Returns:
        (deduped_findings, merged_info) tuple:
        - deduped_findings: List of findings with cross-specialist duplicates merged
        - merged_info: List of MergedFinding info for each merged group
    """
    return _merge_specialist_items(
        [(v.persona, f, v) for v in verdicts for f in v.findings],
        kind="findings",
    )


def merge_specialist_observations(
    verdicts: list[PersonaVerdict],
) -> tuple[list[Finding], list[MergedFinding]]:
    """Merge observations from multiple specialists (F2iLLC/vigil#96).

    The observation path is the twin of :func:`merge_specialist_findings` and
    exists for the same reason: several specialists describing one defect in
    their own words are one defect, not several. Only the findings path was
    ever deduped across specialists, so a single defect that six specialists
    each raised as an observation became six near-identical auto-filed GitHub
    issues — the in-run guard in ``issue_manager`` keys on
    ``stable_finding_key``, and six different sentences produce six different
    keys once ``_canonical_predicate`` falls back to lexical content.

    Grouping is by :func:`_group_by_key_or_location`: the findings path's
    stable semantic identity, widened to also catch specialists that cited the
    same place. Identity alone did not reach it — see that function.

    Args:
        verdicts: List of PersonaVerdict objects from specialists

    Returns:
        (deduped_observations, merged_info) tuple, with the same semantics as
        :func:`merge_specialist_findings`.
    """
    return _merge_specialist_items(
        [(v.persona, o, v) for v in verdicts for o in v.observations],
        kind="observations",
        by_location=True,
    )


# A consensus persona string is embedded in a GitHub issue title, and GitHub
# caps a title at 256 characters. Six short specialist names are nowhere near
# that, but the specialist count is a profile setting, so bound it rather than
# assume it stays small.
_MAX_CONSENSUS_PERSONA_LEN = 120


def consensus_persona(specialists: list[str]) -> str:
    """Render the specialists behind one merged item as a single persona string.

    ``ReviewResult.observation_sources`` is ``list[tuple[str, Finding]]`` and
    ``issue_manager`` renders that string verbatim into the issue title and the
    body's ``**Reviewer:**`` line, so attribution for a merged observation has
    to fit in one string or be lost. Joining with " + " keeps every contributor
    visible in exactly the place a reader looks for the reviewer, and uses only
    characters that survive ``utils.validate_specialist_name`` intact apart
    from the separator itself (which degrades to a space, never to nothing).

    A specialist can contribute more than once to the same group (nothing stops
    one persona raising two observations that share an identity), so names are
    de-duplicated in first-seen order rather than rendered as
    ``Security + Security``.

    The full, untruncated list always stays available on
    ``MergedFinding.specialists``; only this rendering is bounded.
    """
    names = list(dict.fromkeys(name for name in specialists if name))
    if not names:
        return "Vigil"
    joined = " + ".join(names)
    if len(joined) <= _MAX_CONSENSUS_PERSONA_LEN:
        return joined
    return f"{names[0]} + {len(names) - 1} others"


# How many trailing path segments must agree for two cited paths to be treated
# as the same file. Two, not one: models paraphrase a path's leading segments
# (`backend/app/services/x.py` vs `backend/services/x.py` in the bioqms-core
# cluster below) but rarely its tail, while matching on the bare basename alone
# would fuse `src/a/index.ts` with `src/b/index.ts`.
_PATH_TAIL_SEGMENTS = 2

# How far apart two cited lines may be and still be "the same place".
#
# A heuristic, and labelled one. Specialists agree on the file and on the
# defect but not on the line — they pick different lines inside the block they
# are describing. Measured across the clusters in F2iLLC/vigil#96:
# relara#1032-1038 all cited line 73; relara#1110/1111 cited 135 and 136;
# relara#1106/1107/1108 cited 395, 415 and 423 for one duplicated-UPDATE loop.
# The widest adjacent gap that has to close is 20 lines, so a +/-10 window is
# the minimum that works; 25 is roughly a function body, which is the unit a
# model is actually pointing at when it says "the contact update loop", and it
# leaves margin without being file-wide.
#
# ``_normalize_line_range``'s own default of 2 is deliberately NOT changed:
# fingerprinting and cross-round dedup depend on it, and they are matching one
# reviewer against its own earlier self, not several reviewers against each
# other. The wider context is passed in explicitly, here, for this path only.
_OBSERVATION_LINE_PROXIMITY = 25


def _location_identity(finding: Finding) -> tuple[str, tuple[int, int] | None] | None:
    """The place a finding points at: a path bucket and a line range.

    The path collapses to its last :data:`_PATH_TAIL_SEGMENTS` segments, so two
    specialists that disagree about a file's leading directories still land in
    one bucket — the model wrote both ``backend/app/services/audit_outbox.py``
    and ``backend/services/audit_outbox.py`` for one defect. Two segments, not
    one: the bare basename would fuse ``src/a/index.ts`` with ``src/b/index.ts``.
    A path with fewer segments than that is used whole.

    The line is a range widened by :data:`_OBSERVATION_LINE_PROXIMITY`, or
    ``None`` when the model gave no line. ``None`` matches only ``None``: an
    unanchored observation must never absorb one that named a line, or the
    anchored defect's own line stops being what identified it.

    Returns ``None`` for a finding with no usable path at all, which then groups
    on semantic identity only.
    """
    path = finding.file.replace("\\", "/").strip("/").lower()
    if not path or path == "n/a":
        return None
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return None
    tail = "/".join(segments[-_PATH_TAIL_SEGMENTS:])

    if finding.line is None or finding.line <= 0:
        return tail, None
    return tail, normalize_line_range(finding.line, _OBSERVATION_LINE_PROXIMITY)


def _group_by_key_or_location(
    specialist_findings: list[tuple[str, Finding]],
) -> list[list[tuple[str, Finding]]]:
    """Group observations by semantic identity OR by the place they point at.

    Identity alone does not reach the defect this exists for. ``stable_finding_key``
    is ``sha256(component + predicate)``, and ``_canonical_predicate`` compares a
    supplied ``predicate`` as its content words in order, falling back to the
    message's content words when the model emits none. Several specialists
    describing one defect in their own sentences therefore produce several
    distinct keys, and the in-run guard in ``issue_manager`` — which keys on the
    same function — files one issue for each. Both real clusters behind
    F2iLLC/vigil#96 look like that:

    * relara#1032/1033/1034/1036/1037/1038 — six personas, six categories, six
      messages, six different ``vigil-finding-key`` markers, and one identical
      location: ``packages/api/src/middleware/rate-limit.ts:73``.
    * bioqms-core#1571/1574/1580/1581/1583 — five personas, five categories, one
      docstring defect, no line number at all, and the cited path itself split
      between ``backend/app/services/audit_outbox.py`` and
      ``backend/services/audit_outbox.py``.
    * relara#1106/1107/1108 — three personas, one duplicated-UPDATE loop, cited
      at lines 395, 415 and 423 of one file.
    * relara#1110/1111 — two personas, one backslash-escape defect, cited at
      lines 135 and 136.

    The last two are why the line test is proximity rather than equality: models
    agree on the file and the defect, then point at different lines inside the
    block they are describing. Transitivity does the rest and is intended, not
    incidental — 395 is within range of 415 and 415 of 423, so all three become
    one group even where the outermost pair would be a stretch on its own.

    Location was the only thing that identified either cluster as one defect,
    and it is what ``find_cross_specialist_duplicates``' own docstring always
    claimed to key on. No lexical threshold could substitute: measured over the
    fixtures in ``tests/test_observation_dedup.py``, the pairwise
    ``SequenceMatcher`` ratio between six wordings of ONE defect (0.17-0.47) and
    between genuinely different defects (0.18-0.37) overlap almost completely,
    so any cut-off loose enough to catch paraphrase drops unrelated defects.

    Grouping is transitive within the round: if A shares a key with B and B
    shares a location with C, all three are one defect. A single round is all
    this function ever sees.

    THE TRADE-OFF, STATED RATHER THAN HIDDEN: this will sometimes merge two
    genuinely different defects — most easily two unanchored observations in one
    file, since every observation without a line collapses to that file's
    bucket, and next most easily two concerns within the proximity window. The
    wider the window, the likelier that is. It is accepted, and accepted only
    because nothing is discarded: every contributing specialist's own message is carried into the
    filed issue via ``ObservationConsensus.also_reported_by``. An issue that
    contains two real concerns under one heading is strictly better than five
    issues that contain one concern between them, and a reader can split it;
    silently dropping a specialist's text would not be recoverable. That is the
    load-bearing condition of this whole change, not a nicety.

    This is the OBSERVATION path only. Findings post as inline comments where a
    per-specialist voice is cheap and the behaviour is shipped; they still group
    on identity alone.
    """
    parent = list(range(len(specialist_findings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Keep the earlier index as the root so groups come back in order of
            # first appearance, which keeps the output deterministic.
            parent[max(ra, rb)] = min(ra, rb)

    first_by_key: dict[str, int] = {}
    # Indices sharing one path bucket, and the line range each of them claims.
    by_path: dict[str, list[int]] = {}
    line_range: dict[int, tuple[int, int] | None] = {}

    for i, (_, finding) in enumerate(specialist_findings):
        key = stable_finding_key(finding)
        if key in first_by_key:
            union(first_by_key[key], i)
        else:
            first_by_key[key] = i

        location = _location_identity(finding)
        if location is not None:
            path_tail, rng = location
            by_path.setdefault(path_tail, []).append(i)
            line_range[i] = rng

    for indices in by_path.values():
        unlined = [i for i in indices if line_range[i] is None]
        lined = [i for i in indices if line_range[i] is not None]
        # Every observation in one file that named no line is one place.
        for later in unlined[1:]:
            union(unlined[0], later)
        # Lined ones join when their widened ranges overlap. Pairwise rather
        # than by dict key because overlap is not an equivalence relation;
        # union-find supplies the transitivity instead.
        for position, a in enumerate(lined):
            range_a = line_range[a]
            for b in lined[position + 1:]:
                range_b = line_range[b]
                if range_a[0] <= range_b[1] and range_b[0] <= range_a[1]:
                    union(a, b)

    grouped: dict[int, list[tuple[str, Finding]]] = {}
    for i, item in enumerate(specialist_findings):
        grouped.setdefault(find(i), []).append(item)
    return list(grouped.values())


def _merge_specialist_items(
    specialist_findings: list[tuple[str, Finding, PersonaVerdict]],
    kind: str = "findings",
    by_location: bool = False,
) -> tuple[list[Finding], list[MergedFinding]]:
    """Group (persona, finding, verdict) triples and merge each group.

    Shared by the findings and the observations path so the two agree on the
    merge mechanics — representative selection, attribution, logging — and
    differ only in how a group is formed.

    A group of one is kept as-is. A group of more than one keeps a single
    representative — the highest severity, with ties resolved in favour of the
    first encountered, which makes the output deterministic and stable under
    input order — and records every contributing specialist in a
    ``MergedFinding`` so attribution is preserved rather than dropped.

    ``by_location`` widens grouping to also catch specialists citing the same
    place. It is set for observations only: see :func:`_group_by_key_or_location`
    for why that path needs it and why the shipped findings path does not change.
    """
    if not specialist_findings:
        return [], []

    pairs = [(name, finding) for name, finding, _ in specialist_findings]
    if by_location:
        groups = _group_by_key_or_location(pairs)
    else:
        # Unchanged: the stable semantic identity shared with cross-round and
        # issue dedup.
        groups = list(find_cross_specialist_duplicates(pairs).values())

    deduped_findings: list[Finding] = []
    merged_info: list[MergedFinding] = []

    # Build a lookup from (persona, finding_id) to verdict info
    verdict_lookup: dict[tuple[str, int], tuple[PersonaVerdict, Finding]] = {}
    for name, finding, verdict in specialist_findings:
        verdict_lookup[(name, id(finding))] = (verdict, finding)

    for group in groups:
        if len(group) == 1:
            # Single specialist — keep as-is
            _, finding = group[0]
            deduped_findings.append(finding)
        else:
            # Multiple specialists — merge
            specialists = [name for name, _ in group]
            findings = [f for _, f in group]

            # Representative finding: pick highest severity
            rep_finding = max(findings, key=lambda f: _severity_rank(f.severity))

            # Build verdict info for each specialist
            verdict_infos: list[VerdictInfo] = []
            for spec_name, spec_finding in group:
                if (spec_name, id(spec_finding)) in verdict_lookup:
                    verdict, _ = verdict_lookup[(spec_name, id(spec_finding))]
                    verdict_infos.append(
                        VerdictInfo(
                            specialist=spec_name,
                            verdict=verdict.decision,
                            category=spec_finding.category,
                            session_id=verdict.session_id,
                        )
                    )

            # Preserve the representative but track the merge
            deduped_findings.append(rep_finding)
            merged_info.append(
                MergedFinding(
                    finding=rep_finding,
                    specialists=specialists,
                    count=len(specialists),
                    original_findings=findings,
                    verdict_info=verdict_infos,
                )
            )

            log.info(
                "Merged %d specialist %s: %s:%s [%s] — %s",
                len(specialists),
                kind,
                rep_finding.file,
                rep_finding.line,
                rep_finding.category,
                ", ".join(specialists),
            )

    return deduped_findings, merged_info


def _severity_rank(severity: Severity) -> int:
    """Map severity to a numeric rank for comparison. Higher = more severe."""
    rank_map = {
        Severity.critical: 4,
        Severity.high: 3,
        Severity.medium: 2,
        Severity.low: 1,
    }
    return rank_map.get(severity, 0)


def format_merged_finding_comment(
    finding: Finding,
    specialists: list[str],
    session_ids: dict[str, str] | None = None,
    verdict_info: list[VerdictInfo] | None = None,
    total_specialists: int | None = None,
) -> str:
    """Format a merged finding for inline comment display.

    Shows which specialists flagged the issue with consensus table for multiple flaggers.
    Sanitizes LLM-generated content (message, suggestion, category) to prevent XSS.

    Args:
        finding: The representative Finding
        specialists: List of specialist names who flagged it
        session_ids: Optional dict mapping specialist name -> session_id (deprecated, use verdict_info)
        verdict_info: List of VerdictInfo objects with verdict details (replaces session_ids approach)
        total_specialists: Total number of specialists in the review (for consensus count)

    Returns:
        Formatted markdown for the merged finding
    """
    icon = severity_emoji(finding.severity)
    session_ids = session_ids or {}
    verdict_info = verdict_info or []

    # Sanitize LLM-generated content to prevent XSS
    sanitized_message = sanitize_markdown(finding.message)
    sanitized_suggestion = (
        sanitize_markdown(finding.suggestion) if finding.suggestion else None
    )
    sanitized_category = sanitize_markdown(finding.category)

    # Build the main finding section
    suggestion = (
        f"\n\n**Suggestion:** {sanitized_suggestion}"
        if sanitized_suggestion
        else ""
    )

    main_body = (
        f"{icon} **[{finding.severity.value.upper()}]** [{sanitized_category}]\n\n"
        f"{sanitized_message}{suggestion}"
    )

    # If only one specialist or no consensus table requested, use simple format
    if len(specialists) <= 1 or total_specialists is None:
        # Fall back to simple format with validated specialist names and session IDs
        specialist_lines = []
        for spec in specialists:
            # Validate specialist name for safe embedding
            safe_name = validate_specialist_name(spec)

            sid = session_ids.get(spec)
            # Validate session ID
            safe_sid = validate_session_id(sid) if sid else ""

            if safe_sid:
                specialist_lines.append(f"**{safe_name}** `{safe_sid}`")
            else:
                specialist_lines.append(f"**{safe_name}**")
        specialist_text = ", ".join(specialist_lines)
        return (
            f"{icon} **[{finding.severity.value.upper()}]** [{sanitized_category}]\n"
            f"🔍 Flagged by: {specialist_text}\n\n"
            f"{sanitized_message}{suggestion}"
        )

    # Build consensus table for multiple specialists
    lines = [main_body]
    lines.append("\n---\n")
    lines.append(f"📊 **Consensus ({len(specialists)}/{total_specialists} specialists)**")
    lines.append("")

    # Build table header and separator
    lines.append("| Specialist | Verdict | Ref |")
    lines.append("|------------|---------|-----|")

    # Build table rows from verdict_info if available, otherwise from specialists
    if verdict_info:
        for info in verdict_info:
            # Validate specialist name and session ID
            safe_name = validate_specialist_name(info.specialist)
            safe_sid = validate_session_id(info.session_id)

            verdict_emoji = "✅" if info.verdict == "APPROVE" else "🚫"
            session_id_str = f" `{safe_sid}`" if safe_sid else ""
            # Sanitize category field in verdict info
            safe_category = sanitize_markdown(info.category)
            lines.append(
                f"| {safe_name}{session_id_str} | {verdict_emoji} {info.verdict} | {safe_category} |"
            )
    else:
        # Fallback: use specialists list without verdict details
        for spec in specialists:
            safe_name = validate_specialist_name(spec)
            sid = session_ids.get(spec)
            safe_sid = validate_session_id(sid) if sid else ""
            session_id_str = f" `{safe_sid}`" if safe_sid else ""
            lines.append(
                f"| {safe_name}{session_id_str} | — | {sanitized_category} |"
            )

    return "\n".join(lines)


def annotate_findings_with_specialist_context(
    findings: list[Finding],
    merged_info: list[MergedFinding],
) -> list[dict]:
    """Annotate findings with specialist context for later formatting.

    Attaches metadata about which specialists flagged each finding,
    enabling formatted output to show consensus.

    Args:
        findings: The deduped findings list
        merged_info: List of MergedFinding objects

    Returns:
        List of dicts with finding + specialist metadata
    """
    # Build a lookup from finding ID to merged info
    merged_lookup: dict[int, MergedFinding] = {
        id(info.finding): info for info in merged_info
    }

    result = []
    for f in findings:
        if id(f) in merged_lookup:
            info = merged_lookup[id(f)]
            result.append({
                "finding": f,
                "is_merged": True,
                "specialists": info.specialists,
                "count": info.count,
            })
        else:
            result.append({
                "finding": f,
                "is_merged": False,
                "specialists": [],
                "count": 0,
            })
    return result
