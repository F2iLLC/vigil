# Vigil

AI-powered, model-agnostic pull-request review with domain-specialist teams.

Vigil routes a PR diff to focused reviewers, asks a lead reviewer to synthesize their verdicts, posts actionable findings on the relevant diff lines, and can track non-blocking follow-up work as GitHub issues.

> [!IMPORTANT]
> This README documents `main`. The published `v1.0.0` release and `v1` action tag date from March 2026 and do not include the current review lifecycle, model default, or action hardening. The reusable workflow below follows the reviewed implementation centrally. If you call the composite action directly, use the pinned commit shown here instead of `@v1`.

## How it works

```text
PR diff + description + conversation history
                    |
       domain-routed specialists
  Logic | Security | Architecture
  Testing | Performance | DX
                    |
        lead reviewer synthesis
                    |
   APPROVE / REQUEST_CHANGES / BLOCK
        |                       |
 inline findings       actionable observations
                        tracked as GitHub issues
```

Each specialist receives only the files relevant to its domain. Security skips documentation, Testing sees tests and related source, and data/GxP reviewers activate only for relevant files in the enterprise profile.

## Features

- **Model-agnostic review** through [LiteLLM](https://github.com/BerriAI/litellm).
- **Six default specialists plus a lead**, with a seven-specialist enterprise profile for regulated systems.
- **Inline findings** relocated to a valid changed line when the model cites an un-commentable location.
- **Actionable observation gate** that rejects model-generated praise or notes without a concrete follow-up action.
- **Automatic issue tracking** for non-blocking observations, with severity-matched priority labels and open-issue deduplication.
- **PR conversation context** so specialists and the lead can check claims against top-level comments and prior review bodies.
- **Documentation-only fast path** that approves recognized docs-only diffs without calling a model.
- **Cross-specialist consensus** that merges duplicate findings into one comment with specialist attribution.
- **Cross-round deduplication** against active and resolved Vigil comments.
- **Defensive output handling** that validates structured model responses and sanitizes model-generated Markdown before posting.
- **Decision log** that suppresses acknowledged, accepted, wontfix, or false-positive patterns.
- **Review lifecycle commands** for dismissing resolved findings and auto-resolving threads whose code changed.
- **Transient-provider handling** that retries recoverable rate-limit, timeout, and service-unavailable errors without treating infrastructure noise as a code defect.
- **Alert delivery** through SMTP and an optional LunaOS escalation webhook.
- **CLI, composite action, reusable workflow, and webhook server** integration options.

## Installation

Vigil requires Python 3.10 or newer.

For local development:

```bash
git clone https://github.com/F2iLLC/vigil.git
cd vigil
python -m venv .venv
python -m pip install -e .
```

Install the webhook dependencies when using `vigil serve`:

```bash
python -m pip install -e ".[webhook]"
```

For a pinned CLI installation without cloning:

```bash
python -m pip install "git+https://github.com/F2iLLC/vigil.git@fd918eb1d2dbaa16cbecc424aa17ba23002e6685"
```

## Quick start

```bash
export GITHUB_TOKEN="ghp_..."
export GEMINI_API_KEY="..."

vigil review https://github.com/owner/repo/pull/123 --post
```

`GITHUB_TOKEN` must be able to read the PR. Posting reviews and issues also requires pull-request and issue write access.

## CLI

### Review a PR

```text
vigil review <PR_URL> [OPTIONS]

Options:
  -m, --model TEXT      Specialist model
                        Default: gemini/gemini-3.1-flash-lite
  --lead-model TEXT     Optional separate lead-reviewer model
  -p, --profile TEXT    default or enterprise
  --json                Print the structured result
  --post                Post the result to GitHub
```

Examples:

```bash
# Default Gemini model
vigil review https://github.com/org/repo/pull/123 --post

# Gemini specialists with a Claude lead
vigil review https://github.com/org/repo/pull/123 \
  --model gemini/gemini-3.1-flash-lite \
  --lead-model claude-sonnet-4-6 \
  --post

# Regulated-system profile
vigil review https://github.com/org/repo/pull/123 --profile enterprise --post

# Machine-readable output without posting
vigil review https://github.com/org/repo/pull/123 --json
```

### Resolve acknowledged findings

```bash
vigil dismiss-resolved https://github.com/owner/repo/pull/123
```

This resolves Vigil threads that received a resolution reply and records the decision for future suppression. Recognized replies include `resolved`, `fixed`, `addressed`, `done`, and issue-link replies that cover the finding.

### Resolve findings addressed by code changes

```bash
vigil resolve-addressed https://github.com/owner/repo/pull/123
```

This compares the current head with the last Vigil-reviewed commit and resolves Vigil threads whose cited file and line changed. In a pull-request GitHub Actions event, the composite action can auto-detect the PR URL.

### Browse the decision log

```bash
vigil decisions owner/repo
vigil decisions owner/repo --file src/auth.py
vigil decisions owner/repo --category security
vigil decisions owner/repo --remove 5
vigil decisions owner/repo --clear
```

### List profiles

```bash
vigil profiles
```

### Run the webhook server

```bash
vigil serve \
  --host 0.0.0.0 \
  --port 8000 \
  --model gemini/gemini-3.1-flash-lite \
  --profile default
```

## Review lifecycle

Vigil reviews the full PR diff against the base branch. On a posted re-review it also:

1. Locates the most recent Vigil review commit.
2. Resolves threads with accepted resolution replies.
3. Checks whether the PR head changed; if no files changed, it skips the duplicate review.
4. Resolves threads whose cited code changed.
5. Re-reviews the **full PR diff**, not only the latest commit or changed-file subset.
6. Filters findings already covered by active or resolved Vigil comments.

This distinction matters: incremental state controls skipping, thread resolution, and deduplication, while the model still receives the complete PR change.

### Documentation-only PRs

If every changed file is recognized as documentation—Markdown, RST, text, common README/CHANGELOG/LICENSE files, `docs/`, `documentation/`, or GitHub templates—Vigil returns an approval without calling the specialist or lead models.

### PR conversation evidence

Vigil fetches top-level PR comments and prior review bodies and supplies a bounded version of that conversation to every specialist and the lead. Reviewers are instructed to flag factual claims in the diff or description when the existing thread contradicts them.

### Cross-specialist consensus

When multiple specialists report the same file/category/message concern at overlapping lines, Vigil emits one finding and includes a consensus table showing which specialists raised it and their verdicts.

## Observations and automatic issues

With `--post`, non-blocking observations can become GitHub issues:

1. Model-generated observations must include a concrete, non-null suggestion. Compliments, descriptions, and no-action notes are discarded.
2. Vigil ensures a priority label exists for the observation severity: `Critical Priority`, `High Priority`, `Medium Priority`, or `Low Priority`.
3. Open Vigil-created issues are checked for the same file and a sufficiently similar message before a new issue is created.
4. The final review links each tracked observation to its issue.

Security is non-blocking in both built-in profiles. Its findings become observations and do not change the overall review decision, but they can still be tracked and alerted.

## Decision log

Vigil stores acknowledged finding patterns in `~/.vigil/decisions.db`.

- Matching uses repository, file, category, and fuzzy message similarity.
- Resolution replies record the reply author and reason.
- Replies containing `false positive` are recorded as `false_positive`.
- Replies containing `wontfix` or `acceptable` are recorded as `wontfix`.
- Other accepted resolution replies are recorded as `accepted`.
- `--remove` re-enables one pattern; `--clear` removes all stored decisions for a repository.

## GitHub Actions

### Approval credential

The workflow token and the review identity are separate concerns:

- `github.token` can read the PR and usually post comments.
- GitHub may reject `APPROVE` or `REQUEST_CHANGES` events from `github.token`, depending on repository/organization settings and identity rules.
- When that happens, Vigil falls back to a `COMMENT` review. The content is preserved, but it cannot satisfy a required-approval branch rule.
- Configure `VIGIL_REVIEW_TOKEN` as a repository or organization secret when Vigil must submit a real review decision. Use a fine-grained PAT or GitHub App token for a dedicated reviewer identity with **Pull requests: Read and write** access to the target repositories.

The reusable workflow emits a visible warning when `VIGIL_REVIEW_TOKEN` is absent.

### Central reusable workflow

Do not symlink workflow files across repositories. A Git symlink to a path outside the repository breaks on GitHub runners and on other clones.

Use the reusable workflow instead. Each repository keeps this small caller:

```yaml
# .github/workflows/vigil.yml
name: Vigil PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  pull_request_review_comment:
    types: [created]
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  vigil:
    uses: F2iLLC/vigil/.github/workflows/reusable-vigil.yml@main
    with:
      # Omit this input to use ubuntu-latest.
      runner-json: '["self-hosted","linux","x64","ci-light"]'
    secrets: inherit
```

The caller follows `main` so reviewed workflow fixes propagate to every consumer without copying the full YAML. If immutable workflow provenance is more important than automatic propagation, replace `@main` with a full commit SHA and update that pin intentionally.

The centralized workflow provides:

- reviews on PR open, synchronize, reopen, and ready-for-review;
- `/vigil review` on-demand reviews;
- resolution-reply handling;
- `resolve-addressed` on new commits;
- per-PR concurrency cancellation;
- `SKIP_VIGIL=true`, `skip-vigil`, and `[skip vigil]` controls;
- model-aware provider-key checks;
- approval-token warnings; and
- an advisory mode that keeps provider outages from turning into red CI by default.

Set these repository or organization secrets:

| Secret | Required when | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | Using `gemini/*` | Specialist and lead model calls |
| `ANTHROPIC_API_KEY` | Using `claude-*` or `anthropic/*` | Specialist or lead model calls |
| `OPENAI_API_KEY` | Using OpenAI models | Specialist or lead model calls |
| `VIGIL_REVIEW_TOKEN` | Real approval events are required | Submit APPROVE/REQUEST_CHANGES as the reviewer identity |

Repository secrets are not exposed to untrusted fork pull requests under the normal `pull_request` event. Choose a fork-review policy deliberately; do not switch to `pull_request_target` without reviewing the security implications.

### Direct composite-action use

If the centralized lifecycle is unnecessary, call the composite action directly:

```yaml
- uses: F2iLLC/vigil@fd918eb1d2dbaa16cbecc424aa17ba23002e6685
  with:
    model: gemini/gemini-3.1-flash-lite
    profile: default
    gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
    github-token: ${{ secrets.VIGIL_REVIEW_TOKEN || github.token }}
```

Available inputs:

| Input | Default | Notes |
| --- | --- | --- |
| `pr-url` | Event PR URL | Required for comment-triggered workflows |
| `command` | `review` | `review`, `dismiss-resolved`, or `resolve-addressed` |
| `model` | `gemini/gemini-3.1-flash-lite` | LiteLLM model identifier |
| `lead-model` | Same as `model` | Optional separate lead model |
| `profile` | `default` | `default` or `enterprise` |
| `github-token` | `github.token` | Use `VIGIL_REVIEW_TOKEN` for real approval events |
| `gemini-api-key` | Empty | Gemini provider credential |
| `anthropic-api-key` | Empty | Anthropic provider credential |
| `openai-api-key` | Empty | OpenAI provider credential |

The action uses a suitable system Python 3.10+ when available and falls back to `actions/setup-python`. It installs Vigil into an isolated virtual environment under the runner temporary directory.

## Webhook server

Configure a GitHub webhook to send events to:

```text
https://your-host.example/webhook
```

The server handles:

- `pull_request` opened, reopened, and ready-for-review events;
- `/vigil review` top-level PR comments; and
- top-level PR resolution comments.

It skips drafts and bot-authored PRs. The standalone webhook server does not implement the GitHub Actions `synchronize`/`resolve-addressed` lifecycle.

Set `WEBHOOK_SECRET` to verify `X-Hub-Signature-256` signatures. A health endpoint is available at `/health`.

## Alerts

Alert-enabled personas can send the same non-blocking findings through email and the optional LunaOS escalation endpoint.

```bash
# Email
VIGIL_ALERT_EMAIL=dev-team@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=vigil@example.com
SMTP_PASSWORD=app-specific-password

# Optional LunaOS escalation delivery
LUNAOS_ESCALATION_URL=https://hetzner-api.lunaos.io/api/escalations
ESCALATION_INGEST_TOKEN=shared-secret-token
```

Both delivery paths are best-effort and additive. Leave their configuration unset to disable them.

## Profiles

### `default`

| Specialist | Focus | Blocking |
| --- | --- | --- |
| Logic | Correctness, edge cases, concurrency | Yes |
| Security | Validation, injection, secrets, auth | No |
| Architecture | Boundaries, coupling, API design | Yes |
| Testing | Coverage, assertions, error paths | Yes |
| Performance | Queries, memory, rendering, bundle cost | Yes |
| DX | API contracts, documentation, errors, migrations | Yes |

### `enterprise`

The enterprise profile is a separate seven-specialist team, not the default team plus two appended reviewers:

| Specialist | Focus | Blocking |
| --- | --- | --- |
| Architecture | Boundaries, lifecycle, observability, configuration | Yes |
| Security | Validation, auth, secrets, tenant isolation | No |
| Test Strategy | Coverage architecture and assertion quality | Yes |
| Data Architecture | Schemas, migrations, indexes, ownership | Yes |
| Performance | Queries, memory, rendering, bounded work | Yes |
| DX | Public contracts, migrations, documentation | Yes |
| GxP Compliance | Audit trails, ALCOA+, Part 11, immutability | Yes |

## Review decisions

- **APPROVE**: no blocking specialist or lead finding remains.
- **REQUEST_CHANGES**: a specialist or lead found a critical/high issue.
- **BLOCK**: the lead found a fundamental scope, architecture, or coherence problem. GitHub receives this as `REQUEST_CHANGES` because GitHub has no `BLOCK` review event.

Specialists operate under domain sovereignty: they state the constraint in their domain and leave cross-domain implementation choices to the lead reviewer.

## Supported models

Use any provider supported by LiteLLM and set the corresponding environment variable.

```bash
# Google
vigil review "$PR" --model gemini/gemini-3.1-flash-lite
vigil review "$PR" --model gemini/gemini-3.1-pro

# Anthropic
vigil review "$PR" --model claude-sonnet-4-6

# OpenAI
vigil review "$PR" --model gpt-4o
vigil review "$PR" --model o3-mini

# Local
vigil review "$PR" --model ollama/llama3
```

## Architecture

```text
src/vigil/
|-- cli.py                     Typer CLI and review orchestration
|-- reviewer.py                Specialist dispatch and lead synthesis
|-- personas.py                Profiles, prompts, and routing patterns
|-- models.py                  Pydantic review models
|-- diff_parser.py             Diff parsing and docs-only classification
|-- github.py                  PR data and GitHub API access
|-- github_review.py           Review and inline-comment posting
|-- comment_manager.py         Conversation, thread, and resolution lifecycle
|-- context_manager.py         Cross-round fingerprints and filtering
|-- cross_specialist_dedup.py  Consensus merging and formatting
|-- issue_manager.py           Observation issue creation and deduplication
|-- decision_log.py            SQLite-backed decision memory
|-- alerts.py                  SMTP and LunaOS escalation delivery
|-- webhook.py                 FastAPI webhook server
|-- audit.py                   SQLite review audit trail
`-- utils.py                   Sanitization and shared helpers
```

The current pipeline:

1. Fetch PR metadata, full diff, top-level comments, and prior reviews.
2. Locate previous Vigil state for posted re-reviews.
3. Resolve acknowledged or code-addressed threads.
4. Parse the full diff and take the documentation-only fast path when eligible.
5. Route relevant hunks to each specialist sequentially.
6. Filter known decisions and send optional specialist alerts.
7. Run the lead reviewer with specialist results and conversation evidence.
8. Merge duplicate cross-specialist findings into consensus findings.
9. Create deduplicated issues for non-blocking observations.
10. Filter cross-round duplicates and post inline findings with fallbacks.

See [CROSS_ROUND_CONTEXT.md](CROSS_ROUND_CONTEXT.md) for fingerprinting and consensus details.

## License

[MIT](LICENSE)
