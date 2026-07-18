"""Multi-persona review engine with parallel specialist dispatch."""

import json
import secrets
import time
from typing import Callable

from litellm import completion

from .alerts import send_alerts_for_verdicts
from .diff_parser import diff_summary, filter_hunks, is_documentation_only, parse_diff, reassemble_diff
from .models import Finding, PersonaVerdict, ReviewResult, Severity
from .personas import Persona, ReviewProfile

MAX_RETRIES = 5
INITIAL_BACKOFF = 5  # seconds


def _parse_json_response(text: str) -> dict:
    """Parse JSON from LLM response, handling code fences and trailing text."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```json"):
        text = text[len("```json"):]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try normal parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Handle trailing text after valid JSON (common with some models)
    # Find the last closing brace and try parsing up to there
    depth = 0
    end = -1
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end > 0:
        return json.loads(text[:end])

    raise json.JSONDecodeError("No valid JSON object found", text, 0)


def _parse_findings(raw_list: list[dict]) -> list[Finding]:
    """Parse findings with defensive handling for LLM output quirks."""
    results = []
    for f in raw_list:
        # Ensure 'file' is a string (LLMs sometimes return null or objects)
        if not isinstance(f.get("file"), str):
            f["file"] = str(f.get("file") or "unknown")
        # Coerce line to int or None
        if f.get("line") is not None:
            try:
                f["line"] = int(f["line"])
            except (ValueError, TypeError):
                f["line"] = None
        results.append(Finding(**f))
    return results


def _gen_session_id() -> str:
    """Generate a short agent session ID like VGL-a3f8b2."""
    return f"VGL-{secrets.token_hex(3)}"


def _is_transient_llm_error(e: Exception) -> bool:
    """Return True for errors indicating temporary LLM infrastructure unavailability."""
    err_str = str(e).lower()
    return _is_retryable_llm_error(err_str)


def _is_retryable_llm_error(err: Exception | str) -> bool:
    """Return True only for provider errors likely to recover with retry."""
    err_str = str(err).lower()
    non_retryable_429_markers = (
        "billing",
        "quota",
        "insufficient_quota",
        "exceeded your current quota",
        "hard limit",
        "payment",
    )
    if "429" in err_str and any(marker in err_str for marker in non_retryable_429_markers):
        return False
    return any(x in err_str for x in ("rate_limit", "429", "503", "service unavailable", "unavailable", "timeout", "timed out"))


def _call_llm_with_retry(messages: list[dict], model: str, **kwargs):
    """Call litellm completion with exponential backoff on rate limits and transient errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return completion(model=model, messages=messages, **kwargs)
        except Exception as e:
            if _is_retryable_llm_error(e):
                wait = INITIAL_BACKOFF * (2 ** attempt)
                time.sleep(wait)
            else:
                raise
    # Final attempt — let it raise
    return completion(model=model, messages=messages, **kwargs)


def _build_pr_context_block(diff: str, pr_context: dict, file_summary: str = "") -> str:
    """Format PR metadata + diff for inclusion in review prompts."""
    summary_section = ""
    if file_summary:
        summary_section = f"""
### All Changed Files (full PR)
```
{file_summary}
```
"""
    conversation_section = ""
    conversation = pr_context.get("conversation") or ""
    if conversation:
        conversation_section = f"""
### PR Conversation (comments, bot replies, prior reviews)
This is what has ALREADY BEEN SAID in this PR's thread — treat it as evidence,
not code. If the diff, description, or a doc/plan change asserts something as
fact that this conversation contradicts, that is a finding (category
"factual-accuracy") even if the code itself is otherwise correct.
```
{conversation}
```
"""
    return f"""## PR: {pr_context['title']}

**Author:** {pr_context['author']}
**Branch:** {pr_context['head']} -> {pr_context['base']}
**Stats:** +{pr_context['additions']} -{pr_context['deletions']} across {pr_context['changed_files']} files

### Description
{pr_context.get('body') or 'No description provided.'}
{summary_section}{conversation_section}
### Diff (files relevant to your domain)
```diff
{diff}
```"""


def _run_specialist(persona: Persona, pr_block: str, model: str, delay: float = 0) -> PersonaVerdict:
    """Run a single specialist review. Called in parallel."""
    if delay > 0:
        time.sleep(delay)

    response = _call_llm_with_retry(
        messages=[
            {"role": "system", "content": persona.system_prompt},
            {"role": "user", "content": pr_block},
        ],
        model=model,
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError(f"Empty response from model for {persona.name}")
    raw = _parse_json_response(content)

    findings = _parse_findings(raw.get("findings", []))
    observations = _parse_findings(raw.get("observations", []))
    checks = raw.get("checks", {})
    decision = raw.get("decision", "APPROVE")

    return PersonaVerdict(
        persona=persona.name,
        session_id=_gen_session_id(),
        decision=decision,
        checks=checks,
        findings=findings,
        observations=observations,
    )


def _run_lead_review(
    lead_prompt: str,
    pr_block: str,
    verdicts: list[PersonaVerdict],
    model: str,
) -> tuple[str, str, list[Finding]]:
    """Run the lead review after all specialists report."""
    verdicts_text = ""
    for v in verdicts:
        checks_str = ", ".join(f"{k}: {val}" for k, val in v.checks.items())
        findings_str = ""
        if v.findings:
            findings_str = "\n".join(
                f"  - [{f.severity.value}] {f.file}:{f.line or '?'} -- {f.message}"
                for f in v.findings
            )
        findings_header = "Findings:\n" + findings_str if findings_str else "No findings."
        observations_str = ""
        if v.observations:
            observations_str = "\n".join(
                f"  - [{f.severity.value}] {f.file}:{f.line or '?'} -- {f.message}"
                for f in v.observations
            )
        observations_header = "Observations:\n" + observations_str if observations_str else "No observations."
        verdicts_text += f"""
### {v.persona} [{v.session_id}]: {v.decision}
Checks: {checks_str}
{findings_header}
{observations_header}
"""

    user_message = f"""{pr_block}

---

## Specialist Verdicts
{verdicts_text}

Review this PR as the final gate. Consider the specialist verdicts above."""

    response = _call_llm_with_retry(
        messages=[
            {"role": "system", "content": lead_prompt},
            {"role": "user", "content": user_message},
        ],
        model=model,
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = _parse_json_response(response.choices[0].message.content)
    decision = raw.get("decision", "APPROVE")
    summary = raw.get("summary", "")
    findings = _parse_findings(raw.get("findings", []))
    return decision, summary, findings


def review_diff(
    diff: str,
    pr_context: dict,
    profile: ReviewProfile,
    model: str = "gemini/gemini-2.5-flash",
    lead_model: str | None = None,
    on_specialist_done: Callable[[PersonaVerdict], None] | None = None,
    repo_key: str = "",
) -> ReviewResult:
    """Run the full multi-persona review pipeline.

    1. Dispatch all specialists with staggered starts
    2. Collect verdicts and filter known decisions
    3. Send email alerts for alert-enabled personas
    4. Merge overlapping findings from multiple specialists into single comments
    5. Run lead reviewer with all verdicts
    6. Return aggregated result with merged findings in lead_findings

    Args:
        diff: Raw unified diff text.
        pr_context: Dict with PR metadata (title, author, head, base, etc.).
            An optional "conversation" key (str, from comment_manager.build_
            conversation_context) is included in every specialist and lead
            prompt so factual claims in the diff can be cross-checked against
            the PR's comment thread and prior reviews.
        profile: The ReviewProfile containing specialists and lead prompt.
        model: LLM model identifier for specialists (default: gemini/gemini-2.5-flash).
        lead_model: Optional separate model for the lead reviewer.
        on_specialist_done: Optional callback invoked after each specialist finishes.
        repo_key: Repository in "owner/repo" format. When provided, findings are
            checked against the decision log and previously-acknowledged patterns
            are suppressed before the lead review.

    Returns:
        ReviewResult with:
        - specialist_verdicts: verdicts with merged findings removed
        - lead_findings: aggregated lead findings + deduped merged findings
        - observations: all non-blocking observations from all specialists
    """
    lead_model = lead_model or model

    # --- Parse diff into per-file hunks for targeted routing ---
    all_hunks = parse_diff(diff)
    full_summary = diff_summary(all_hunks)

    if is_documentation_only(all_hunks):
        verdicts = [
            PersonaVerdict(
                persona=persona.name,
                session_id=_gen_session_id(),
                decision="APPROVE",
                checks={"documentation_only": "PASS"},
                findings=[],
                observations=[],
            )
            for persona in profile.specialists
        ]
        for verdict in verdicts:
            if on_specialist_done:
                on_specialist_done(verdict)

        return ReviewResult(
            decision="APPROVE",
            summary="Documentation-only changes detected; Vigil auto-approved without model review.",
            commit_sha=pr_context.get("head_sha", ""),
            pr_url=pr_context.get("url", ""),
            model=model,
            specialist_verdicts=verdicts,
            lead_findings=[],
            observations=[],
            observation_sources=[],
        )
    # --- Step 1: Sequential specialist reviews ---
    # Each specialist gets only the files matching their patterns
    verdicts: list[PersonaVerdict] = []

    for persona in profile.specialists:
        # Filter diff to this specialist's domain
        if persona.file_patterns:
            specialist_hunks = filter_hunks(all_hunks, persona.file_patterns)
            specialist_diff = reassemble_diff(specialist_hunks)
        else:
            specialist_hunks = all_hunks
            specialist_diff = diff

        # Build PR context with filtered diff + file summary
        pr_block = _build_pr_context_block(specialist_diff, pr_context, full_summary)

        if not specialist_diff.strip():
            # No relevant files for this specialist — auto-approve
            verdicts.append(
                PersonaVerdict(
                    persona=persona.name,
                    session_id=_gen_session_id(),
                    decision="APPROVE",
                    checks={},
                    findings=[],
                    observations=[],
                )
            )
            if on_specialist_done:
                on_specialist_done(verdicts[-1])
            continue

        try:
            verdict = _run_specialist(persona, pr_block, model)

            # Non-blocking personas: move findings to observations, force APPROVE
            if not persona.blocking and verdict.findings:
                verdict.observations = verdict.findings + verdict.observations
                verdict.findings = []
                verdict.decision = "APPROVE"

            verdicts.append(verdict)
            if on_specialist_done:
                on_specialist_done(verdict)
        except Exception as e:
            if _is_transient_llm_error(e):
                # Transient infra error (503/timeout) — specialist was unavailable, not a code issue.
                # Emit a non-blocking observation so the lead reviewer is informed but not forced to block.
                verdicts.append(
                    PersonaVerdict(
                        persona=persona.name,
                        session_id=_gen_session_id(),
                        decision="APPROVE",
                        checks={},
                        findings=[],
                        observations=[
                            Finding(
                                file="N/A",
                                severity=Severity.low,
                                category="reviewer_unavailable",
                                message=f"{persona.name} specialist was temporarily unavailable ({type(e).__name__}). Review skipped — not a code-quality signal.",
                            )
                        ],
                    )
                )
            else:
                verdicts.append(
                    PersonaVerdict(
                        persona=persona.name,
                        session_id=_gen_session_id(),
                        decision="ERROR",
                        checks={},
                        findings=[
                            Finding(
                                file="N/A",
                                severity=Severity.medium,
                                category="reviewer_error",
                                message=f"Specialist review failed: {e}",
                            )
                        ],
                        observations=[],
                    )
                )

    # --- Step 1.5: Filter known decisions ---
    if repo_key:
        try:
            from .decision_log import filter_known_findings
            for v in verdicts:
                orig_findings = len(v.findings)
                orig_obs = len(v.observations)
                v.findings = filter_known_findings(repo_key, v.findings)
                v.observations = filter_known_findings(repo_key, v.observations)
                suppressed = (orig_findings - len(v.findings)) + (orig_obs - len(v.observations))
                if suppressed:
                    import logging
                    logging.getLogger(__name__).info(
                        "Suppressed %d known finding(s) for %s", suppressed, v.persona
                    )
        except Exception:
            pass  # decision log is best-effort

    # --- Step 1.6: Send email alerts for alert-enabled personas ---
    alert_personas = {p.name for p in profile.specialists if p.alert}
    if alert_personas:
        try:
            sent = send_alerts_for_verdicts(
                verdicts, alert_personas,
                pr_url=pr_context.get("url", ""),
                pr_title=pr_context.get("title", ""),
            )
            if sent:
                import logging
                logging.getLogger(__name__).info("Sent %d alert email(s)", sent)
        except Exception:
            pass  # alerting is best-effort, never blocks the review

    # --- Step 2: Lead review (gets file summary + specialist verdicts, not full diff) ---
    lead_pr_block = _build_pr_context_block(diff, pr_context, full_summary)
    decision, summary, lead_findings = _run_lead_review(
        profile.lead_prompt, lead_pr_block, verdicts, lead_model
    )

    # --- Step 3.5: Cross-specialist deduplication ---
    # When multiple specialists flag the same issue at the same location,
    # merge them into a single finding with specialist attribution.
    # The deduped findings are stored in the ReviewResult and made available
    # to the posting pipeline via a new merged_findings field.
    merged_findings_info: list = []  # List of (deduped_finding, specialists, original_findings)
    try:
        from .cross_specialist_dedup import merge_specialist_findings
        deduped_findings, merged_info = merge_specialist_findings(verdicts)

        if merged_info:
            import logging
            logging_module = logging.getLogger(__name__)
            logging_module.info(
                "Deduped %d cross-specialist finding group(s)",
                len(merged_info),
            )

            # Collect all findings that are part of merged groups (to remove from individual verdicts)
            merged_finding_ids: set[int] = set()
            for info in merged_info:
                for orig_finding in info.original_findings:
                    merged_finding_ids.add(id(orig_finding))
                # Store metadata for later use in posting pipeline
                merged_findings_info.append({
                    "finding": info.finding,
                    "specialists": info.specialists,
                    "original_findings": info.original_findings,
                    "count": info.count,
                })

            # Update all specialist verdicts: remove findings that were merged
            for v in verdicts:
                old_count = len(v.findings)
                v.findings = [f for f in v.findings if id(f) not in merged_finding_ids]
                if len(v.findings) < old_count:
                    logging_module.debug(
                        "Removed %d merged findings from %s",
                        old_count - len(v.findings),
                        v.persona,
                    )

            # Add the deduped findings to lead_findings so they flow to the posting pipeline
            # This ensures merged findings are actually posted
            lead_findings.extend(deduped_findings)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("Cross-specialist dedup failed: %s", e)


    # --- Step 3: Aggregate observations with persona tracking ---
    all_observations: list[Finding] = []
    observation_sources: list[tuple[str, Finding]] = []
    for v in verdicts:
        for obs in v.observations:
            all_observations.append(obs)
            observation_sources.append((v.persona, obs))

    return ReviewResult(
        decision=decision,
        summary=summary,
        commit_sha=pr_context.get("head_sha", ""),
        pr_url=pr_context.get("url", ""),
        model=model,
        specialist_verdicts=verdicts,
        lead_findings=lead_findings,
        observations=all_observations,
        observation_sources=observation_sources,
    )
