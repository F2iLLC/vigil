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

    Grouping happens on the same stable semantic identity the findings path
    uses, so the two paths cannot drift apart.

    Args:
        verdicts: List of PersonaVerdict objects from specialists

    Returns:
        (deduped_observations, merged_info) tuple, with the same semantics as
        :func:`merge_specialist_findings`.
    """
    return _merge_specialist_items(
        [(v.persona, o, v) for v in verdicts for o in v.observations],
        kind="observations",
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

    The full, untruncated list always stays available on
    ``MergedFinding.specialists``; only this rendering is bounded.
    """
    names = [name for name in specialists if name]
    if not names:
        return "Vigil"
    joined = " + ".join(names)
    if len(joined) <= _MAX_CONSENSUS_PERSONA_LEN:
        return joined
    return f"{names[0]} + {len(names) - 1} others"


def _merge_specialist_items(
    specialist_findings: list[tuple[str, Finding, PersonaVerdict]],
    kind: str = "findings",
) -> tuple[list[Finding], list[MergedFinding]]:
    """Group (persona, finding, verdict) triples by stable identity and merge.

    Shared by the findings and the observations path so the two can never
    disagree about what "the same defect" means.

    A group of one is kept as-is. A group of more than one keeps a single
    representative — the highest severity, with ties resolved in favour of the
    first encountered, which makes the output deterministic and stable under
    input order — and records every contributing specialist in a
    ``MergedFinding`` so attribution is preserved rather than dropped.
    """
    if not specialist_findings:
        return [], []

    # Group by the stable semantic identity shared with cross-round/issue dedup.
    groups = find_cross_specialist_duplicates(
        [(name, finding) for name, finding, _ in specialist_findings]
    )

    deduped_findings: list[Finding] = []
    merged_info: list[MergedFinding] = []

    # Build a lookup from (persona, finding_id) to verdict info
    verdict_lookup: dict[tuple[str, int], tuple[PersonaVerdict, Finding]] = {}
    for name, finding, verdict in specialist_findings:
        verdict_lookup[(name, id(finding))] = (verdict, finding)

    for fp, group in groups.items():
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
