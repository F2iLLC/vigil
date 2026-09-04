# Cross-Round Context & Cross-Specialist Deduplication

This document describes the implementation of Issue #7 (cross-round context) and Issue #6 (cross-specialist deduplication) in Vigil.

## Overview

Vigil now prevents two common problems in multi-round reviews:

1. **Cross-Round Re-flagging** — Vigil no longer re-posts findings that were already flagged in previous review rounds, even if the PR author didn't explicitly dismiss them
2. **Cross-Specialist Spam** — When multiple specialists flag the same issue at the same location, Vigil merges them into a single comment showing which specialists flagged it. This applies to both paths: blocking *findings* become one comment, and non-blocking *observations* become one auto-filed issue (F2iLLC/vigil#96)

## Architecture

### New Modules

#### `src/vigil/context_manager.py`
Handles cross-round context and finding fingerprinting.

**Key Concepts:**
- **Finding Fingerprint**: A unique identifier for a finding pattern
  - Combines: file path, category, message content hash, line range
  - Accounts for slight line shifts (e.g., code added above the original issue)
  - Enables fuzzy matching across multiple review rounds

**Key Functions:**
- `fingerprint_finding(finding)` — Generate a fingerprint for any Finding
- `fingerprints_match(fp1, fp2, exact_line=False)` — Check if two fingerprints match
  - `exact_line=False`: Fuzzy matching (allows line range overlap)
  - `exact_line=True`: Strict matching (requires identical ranges)
- `extract_finding_from_comment(body, path, line)` — Parse Finding from a Vigil comment body
- `filter_cross_round_duplicates(new_findings, existing_comments)` — Main API: filters findings against previous comments

**Example Usage:**
```python
from vigil.context_manager import filter_cross_round_duplicates

# New findings from current review
new_findings = [f1, f2, f3]

# Existing comments from GitHub (from previous rounds + resolved threads)
existing_comments = github.fetch_all_vigil_comments(...)

# Filter out findings that match previous comments
filtered = filter_cross_round_duplicates(new_findings, existing_comments)
# filtered has fewer entries if duplicates were found
```

#### `src/vigil/cross_specialist_dedup.py`
Handles merging findings — and observations — flagged by multiple specialists.

**Key Concepts:**
- **Merged Finding**: When multiple specialists flag the same issue, combine them
- **Representative Finding**: The highest-severity version of merged findings
- **Specialist Attribution**: Tracks which specialists flagged the merged issue

**Key Functions:**
- `merge_specialist_findings(verdicts)` — Main API: groups findings by fingerprint
  - Returns: `(deduped_findings, merged_info)`
  - `deduped_findings`: List of findings with cross-specialist duplicates removed
  - `merged_info`: List of MergedFinding objects with specialist attribution
- `merge_specialist_observations(verdicts)` — The same API for the observation path
  (F2iLLC/vigil#96). Same grouping, same representative rule, same return shape;
  observations are what Vigil auto-files GitHub issues from, so a defect that
  merges as a finding must merge as an observation too or one defect becomes one
  issue per specialist.
- `consensus_persona(specialists)` — Render the contributors of a merged item as the
  single persona string `observation_sources` and the issue body both carry
- `format_merged_finding_comment(finding, specialists, session_ids)` — Format for display
- `find_cross_specialist_duplicates(specialist_findings)` — Group by fingerprint

**Example Usage:**
```python
from vigil.cross_specialist_dedup import merge_specialist_findings

# verdicts from all specialists
verdicts = [security_verdict, logic_verdict, testing_verdict, ...]

deduped, merged = merge_specialist_findings(verdicts)

if merged:
    print(f"Merged {len(merged)} finding group(s)")
    for info in merged:
        print(f"  {info.specialists} both flagged {info.finding.file}:{info.finding.line}")
```

### Modified Modules

#### `src/vigil/reviewer.py`
**Change:** Added Step 3.5 and Step 3.6 before creating ReviewResult

After all specialists complete and lead review runs, but before building the final ReviewResult:
```python
# --- Step 3.5: Cross-specialist deduplication (findings) ---
merge_specialist_findings(verdicts)  # modifies verdicts in-place

# --- Step 3.6: Cross-specialist deduplication (observations) ---
merge_specialist_observations(verdicts)  # rebuilds observations/observation_sources
```

This ensures the ReviewResult contains deduplicated findings *and* deduplicated
observations from the start. Step 3.6 also logs
`Deduped N cross-specialist observation group(s)`, mirroring the findings log, so
"deduplicated" is evidenced in the run record rather than merely asserted.

`ReviewResult.observation_sources` keeps exactly one `(persona, observation)`
entry per surviving observation — `issue_manager` builds its persona map from
`id(observation)`, so a duplicate or missing entry would misattribute the filed
issue. For a merged observation the persona is the `consensus_persona` string
naming every contributor, which `issue_manager` renders verbatim into the issue
title and the body's `**Reviewer:**` line; the untruncated list stays on
`MergedFinding.specialists`.

#### `src/vigil/github_review.py`
**Change:** Added Step 0 at the start of `post_review()`

Before placing findings as inline comments:
```python
# --- Step 0: Cross-round context filtering ---
filter_cross_round_duplicates(all_findings, existing_comments)
```

This removes findings that match previous rounds before they're posted.

**Flow:**
1. Fetch all new findings from specialists + lead
2. Filter against existing comments (from GitHub API)
3. Update verdicts to remove filtered findings
4. Proceed with posting remaining findings

#### `src/vigil/comment_manager.py`
**Change:** Added `filter_against_existing_findings()` function

Optional utility for filtering inline comments dict objects (used internally but available for testing).

## How It Works

### Cross-Round Context (Issue #7)

When a user opens a PR and Vigil reviews it multiple times:

**Round 1:**
- Security specialist finds SQL injection at `src/auth.py:42`
- Posts comment with finding

**Round 2 (code changes in other part of file):**
- Security specialist finds same SQL injection at `src/auth.py:42` (still there!)
- Comment fingerprint matches existing comment from Round 1
- Comment is filtered out and NOT reposted

**Matching Logic:**
```
new_finding = Finding(file="src/auth.py", line=43, ...)  # line shifted by 1
existing_comment.finding = Finding(file="src/auth.py", line=42, ...)  # original finding

# Fingerprints:
new_fp = fingerprint_finding(new_finding)
existing_fp = fingerprint_finding(existing_finding)

# Match?
fingerprints_match(new_fp, existing_fp, exact_line=False) == True
# Because: file matches, category matches, message matches,
# line ranges [41-45] and [40-44] overlap
```

### Cross-Specialist Deduplication (Issue #6)

When multiple specialists flag the same issue in the same round:

**Review:**
- Security specialist finds SQL injection at `src/auth.py:42`
  - Finding: `Finding(file="src/auth.py", line=42, category="SQL Injection", message="Dangerous SQL")`
- Logic specialist also finds the same SQL issue at same location
  - Finding: `Finding(file="src/auth.py", line=42, category="SQL Injection", message="Dangerous SQL")`

**Deduplication:**
```
merge_specialist_findings([security_verdict, logic_verdict])
# Returns:
#   deduped_findings = [Finding(...)]  # one copy
#   merged_info = [MergedFinding(finding=..., specialists=["Security", "Logic"], count=2)]
```

**Posted Comment:**
```
🔴 **[HIGH]** [SQL Injection]
🔍 Flagged by: **Security** `VGL-abc123`, **Logic** `VGL-def456`

Dangerous SQL concatenation — use parameterized queries
```

### Cross-Specialist Observation Deduplication (Issue #96)

Observations take the same route, and it matters more there: Vigil auto-files a
GitHub issue per surviving observation. Before #96 only the findings path was
merged, so six specialists describing one defect in six different sentences
produced six different `stable_finding_key` values (the key falls back to the
message's content words when the model emits no structured `predicate`), and the
in-run guard in `issue_manager` filed six near-identical issues for one defect.

```
merge_specialist_observations([security_verdict, logic_verdict, ...])
# Returns:
#   deduped_observations = [Finding(...)]  # one copy, the highest-severity one
#   merged_info = [MergedFinding(finding=..., specialists=["Security", "Logic", ...], count=6)]
```

**Filed Issue:**
```
[Vigil/Security + Logic + Performance] duplication: ...

**Reviewer:** Security + Logic + Performance
```

Grouping uses the same `stable_finding_key` identity as the findings path, so
two genuinely different defects are never collapsed — only what would already
have merged as a finding merges as an observation.

## Finding Fingerprint Details

A finding fingerprint combines four components:

### 1. File Path
```python
fingerprint.file = finding.file  # "src/auth.py"
```
No fuzzy matching — files must match exactly.

### 2. Category
```python
fingerprint.category = finding.category  # "SQL Injection"
```
No fuzzy matching — categories must match exactly.

### 3. Message Hash
```python
# Extract normalized text from finding message
message_text = extract_message_content(finding.message)
# Generate stable hash (MD5 of lowercased, whitespace-normalized text)
fingerprint.message_hash = content_fingerprint(message_text)
```

This hash is consistent even if the message contains:
- Different capitalization
- Extra whitespace
- Markdown formatting
- Session IDs or other metadata

### 4. Line Range
```python
# Convert single line number to a range (accounts for code shifts)
line = finding.line  # e.g., 42
context_lines = 2  # configurable
fingerprint.line_range = (max(0, line - context_lines), line + context_lines)
# Result: (40, 44)
```

**Why ranges?**
When code is modified, the issue might move from line 42 to line 44. With ranges:
- Line 42 finding → range [40, 44]
- Line 44 finding → range [42, 46]
- Ranges overlap → match!

**Why 2 context lines?**
- A small code addition (1-2 lines) above the issue shouldn't trigger a re-post
- Larger changes (3+ lines) indicate substantial refactoring, so repost is justified
- Configurable via `_normalize_line_range(line, context_lines=2)`

## Matching Modes

### Fuzzy Mode (Cross-Round)
```python
fingerprints_match(fp1, fp2, exact_line=False)
```

Matches if:
- Same file AND category AND message_hash
- Line ranges **overlap** (not exactly equal)

Use case: Cross-round dedup — ignore small line shifts

### Exact Mode (Same-Round)
```python
fingerprints_match(fp1, fp2, exact_line=True)
```

Matches if:
- Same file AND category AND message_hash
- Line ranges **equal exactly**

Use case: Cross-specialist dedup — multiple specialists at exact same location

## Integration Points

### 1. Reviewer Loop (`src/vigil/reviewer.py:320`)
```python
# After all specialists complete, after lead review
# BEFORE building ReviewResult

try:
    from .cross_specialist_dedup import merge_specialist_findings
    deduped_findings, merged_info = merge_specialist_findings(verdicts)
    
    if merged_info:
        # Update verdicts to reflect deduplication
        for v in verdicts:
            merged_ids = {id(info.finding) for info in merged_info}
            v.findings = [f for f in v.findings if id(f) not in merged_ids]
except Exception:
    pass  # Best-effort, never blocks
```

### 2. Comment Posting (`src/vigil/github_review.py:320`)
```python
# At start of post_review(), before placing findings inline

try:
    from .context_manager import filter_cross_round_duplicates
    all_new_findings = [f for v in verdicts for f in v.findings] + lead_findings
    
    filtered = filter_cross_round_duplicates(all_new_findings, existing_comments)
    
    # Rebuild verdicts with filtered findings
    removed_ids = {id(f) for f in all_new_findings} - {id(f) for f in filtered}
    for v in verdicts:
        v.findings = [f for f in v.findings if id(f) not in removed_ids]
except Exception:
    pass  # Best-effort, never blocks
```

### 3. CLI Integration (`src/vigil/cli.py`)
The CLI already fetches existing comments:
```python
existing_comments = fetch_all_vigil_comments(owner, repo, pr_number, token)
# Includes both active comments AND resolved threads
# Passed to post_review() for cross-round filtering
```

## Testing

Comprehensive tests are provided in two new test files:

### `tests/test_context_manager.py`
Tests for fingerprinting and cross-round matching:
- Line range normalization
- Range overlap detection
- Fingerprint generation and matching
- Finding extraction from comment bodies
- Cross-round duplicate filtering

### `tests/test_cross_specialist_dedup.py`
Tests for cross-specialist merging:
- Severity ranking
- Finding merging logic
- Formatted comment output
- MergedFinding data structure

### `tests/test_observation_dedup.py`
Tests for cross-specialist merging on the observation path (#96):
- Six specialists, one defect, one surviving observation and one issue
- Genuinely different defects are never collapsed
- The representative is the highest-severity member of the group
- Every contributing specialist stays recoverable, in the source and in the issue

**Run tests:**
```bash
pytest tests/test_context_manager.py -v
pytest tests/test_cross_specialist_dedup.py -v
pytest tests/test_observation_dedup.py -v
```

## Performance Considerations

### Fingerprint Generation
- O(1) per finding (hash of normalized message)
- Called once per new finding

### Cross-Round Filtering
- O(N*M) where N = new findings, M = existing comments
- Mitigated by lazy extraction (only extract finding if needed)
- Acceptable because M is usually small (10-50 comments per PR)

### Cross-Specialist Deduplication
- O(N log N) via grouping by fingerprint
- Called once per review (not per specialist)
- Negligible cost

## Error Handling

Both features are **best-effort** — failures never block reviews:

```python
try:
    # Feature code
    result = feature(input)
except Exception as e:
    log.debug("Feature failed: %s", e)
    # Continue with fallback behavior
```

If fingerprinting fails, findings are treated as independent (no dedup). If extraction fails, comments are treated as unknown (no match).

## Configuration

No explicit configuration needed. Key parameters:

| Parameter | Location | Default | Meaning |
|-----------|----------|---------|---------|
| `context_lines` | `context_manager.py` | 2 | Lines to expand around finding for range |
| `similarity_threshold` | `comment_manager.py` | 0.85 | Fuzzy message match threshold (85%) |
| `exact_line` | fingerprint matching | False (cross-round) | Exact or fuzzy line matching |

## Future Enhancements

1. **Configurable Context Lines** — Allow users to adjust how much line shift is tolerated
2. **Decision Confidence** — Track confidence in finding matches; low confidence → always repost
3. **User Feedback** — Learn from user dismissals to improve fingerprint accuracy
4. **Cross-Repo Patterns** — Share fingerprints across repos to detect platform-wide issues

## Example: Full Flow

```
PR opened with 10 files changed

Round 1 Review:
  Security finds SQL injection at src/auth.py:42 → Posted as comment
  Logic finds design issue at src/api.py:10 → Posted as comment

[Author makes changes]

Round 2 Review (2 files changed):
  - src/auth.py:44 (code shifted by 2 lines due to additions above)
    Security finds SQL injection (same fingerprint as Round 1)
    → FILTERED OUT (cross-round duplicate)
  
  - src/api.py:10 (unchanged)
    Logic finds design issue (same as Round 1)
    → FILTERED OUT (cross-round duplicate)
  
  - src/utils.py:100 (new finding)
    Security finds input validation issue
    → POSTED (new finding)
    
  - src/utils.py:100 (same location)
    DX specialist also finds input validation issue
    → MERGED with Security finding
    → POSTED as single comment showing both specialists flagged it

Final Result:
  Round 1: 2 comments
  Round 2: 1 comment (merged from 2 specialists, 2 filtered as duplicates)
  Total on PR: 3 comments (not 5!)
```

