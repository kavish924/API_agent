# API Change Guardian — System Guide

> "Dependabot for APIs": a GitHub App that watches what API providers ship and, when a
> change breaks code in a repository, opens one draft pull request with the version
> bump and the fix together, tested against the new version.

| | |
|---|---|
| **Author** | Kavish |
| **Status** | Draft v3 (source: [PRD v3](https://claude.ai/artifact/8ud7FGK43L3XkVkWmto4xm)) |
| **Source idea** | YC request for startups, ["Self-Maintaining APIs"](https://www.youtube.com/watch?v=c3TxAUir2R8) |
| **Timeline** | 7 weeks |
| **Language** | Python only |
| **Providers** | openai-python, stripe-python; OpenAI deprecation notices |
| **Hosting** | Google Cloud |

---

## 1. What the system does (end-to-end flow)

1. **Watch** — Once a day, a poll checks each provider source: GitHub Releases of the
   watched SDKs and one deprecation page. New material is stored exactly as fetched.
2. **Understand** — An LLM converts each release note or page entry into structured
   change records. This happens once per document and is reused by every repo.
3. **Gate** — A release with no breaking or deprecating change starts nothing.
4. **Fan out** — A release that passes the gate starts one run per installed repo.
5. **Range** — Each run reads the version the repo pins on its default branch. Old is
   that version; new is the latest release.
6. **Reconcile** — The public API of the two versions is diffed with griffe. Records
   about symbols that never existed are dropped; records the diff confirms are high
   confidence, the rest medium.
7. **Scan** — The repo is scanned with libcst for call sites matching the changes. No
   findings ends the run with no output.
8. **Fix** — A deterministic bump editor updates the dependency file and codemods
   handle simple cases. For the rest, an agent investigates and proposes a patch,
   which must pass the AST guard.
9. **Verify** — A Docker sandbox installs dependencies at the new version, then runs
   lint, type check and tests with the network off. The result is a level.
10. **Deliver** — L1 or higher → a draft PR with bump and fix. Otherwise → one issue
    with the affected call sites.

API-level changes (a retired model) follow the same path with no version bump: the
scanner looks for the retired identifier as a string argument, and the fix replaces it
with the provider's recommended replacement.

---

## 2. Core components

| Component | Responsibility | Key detail |
|---|---|---|
| **Scheduler** | Start the daily poll | Cloud Scheduler when deployed; `guardian poll` locally |
| **App service** | Receive installation webhooks | FastAPI on Cloud Run; signature check; publishes a job; no database access |
| **Job queue** | Carry `poll`, `run` and `event` jobs | Pub/Sub when deployed, in-process locally |
| **Watcher** | Detect new releases and page changes | ETags; cache keyed on source and version with revisions; release gate; fan-out |
| **API differ** | Facts about the SDK | griffe, static load; gate diff and run diff; symbol lookup |
| **LLM extractor** | Notes and pages → change records | Strict schema; evidence-quote check; **no tools** |
| **Scanner** | Find affected call sites | libcst with name and scope resolution; literal-argument matching; ignore rules |
| **Fixer** | Produce one minimal patch | Bump editor, codemods, then the agent; AST guard on model-written diffs |
| **Agent fixer** | Fix what codemods cannot | Seven fixed tools, budgets, every step logged |
| **Sandbox runner** | Verify the patch | Two phases; levels L1 to L3; baseline comparison; type-check service for the agent |
| **PR writer** | Deliver the result | Draft upgrade PR on its own branch, or one issue |

---

## 3. Pipeline stages in detail

### 3.1 Change watching and fan-out (FR1)
- Sources: GitHub Releases for `openai-python` and `stripe-python`, plus the API-level
  source in 3.3. Polled daily with conditional requests.
- A new release body is stored byte-for-byte. An edit becomes a new revision.
- **Release gate:** no breaking or deprecating record → no runs.
- **Fan-out:** one run per installed repo; installing the app starts a catch-up run.
- **Version range:** old = the repo's pinned version (a lock file wins over a
  manifest), new = the latest release. A repo that does not use or pin the package
  ends the run silently.
- CLI: `guardian poll`, `guardian run --repo PATH --package openai`.

### 3.2 SDK change records (FR2)
- Schema: `provider`, `package`, `version`, `change_type`, `qualified_symbol`,
  `affected_parameter`, `replacement`, `fixable`, `evidence_quote`, `migration_hint`.
- Validation failure → one retry. Evidence quote not in the note (after normalising
  whitespace and Markdown) → record rejected.
- The public API of the exact old and new versions is diffed with griffe. Package code
  is never executed.
- Per run: a record whose symbol is not in the old version's API is dropped.
- Confidence: **high** when the API diff confirms the change, **medium** when only the
  notes state it. Medium records are kept.

### 3.3 API-level changes (FR3)
- One source in this version: OpenAI's deprecations page.
- Each poll snapshots the page if it changed and extracts only new or changed entries:
  kind, identifier, replacement, shutdown date, evidence quote.
- Scan: the identifier passed as a string literal to a resolved SDK call, including a
  module-level constant one step away.
- Fix: replace the literal with the recommended replacement. No version bump.
- Every such PR is labelled a **behaviour change**: a different model can change
  output, cost and limits, and no check here verifies that.
- Matches in non-Python files are listed as possible usages, never edited.

### 3.4 Repository scanning (FR4)
- Scans the default branch at one pinned commit.
- Parses Python with libcst; resolves imports, aliases and scopes; tracks simple
  client assignments and resolves attribute chains.
- For parameter changes, a finding exists only when the call passes that parameter.
- Skips virtualenvs and `site-packages`, and paths in `.guardianignore`.
- Deterministic on purpose: this stage decides whether the app speaks.
- **No findings → no output.**

### 3.5 Fix generation (FR5)
- **Bump editor:** rewrites the pinned version; lock files are regenerated by the
  repo's own tool inside the sandbox.
- **Tier 1, codemods:** parameter renames, symbol renames, import path changes,
  literal replacements.
- **Tier 2, agent:** one session per remaining fixable finding, with tools
  `read_file`, `search_repo`, `lookup_api`, `get_change`, `run_typecheck`,
  `propose_patch`, `cannot_fix`. No shell, network, GitHub or write access. Step,
  proposal, token and time budgets; every step logged.
- **AST guard** on every model-written diff: changed statements must contain a finding
  or be an SDK import; no new imports of other modules; `.py` files with findings only.
  A rejection goes back to the agent as a tool result.
- A single-shot mode (one call, no tools) is kept as the eval baseline.
- Response-shape and behavioural changes get no diff; they are listed with call sites.

### 3.6 Verification (FR6)
- Phase 1, install: PyPI-only network, no secrets, metadata server blocked.
- Phase 2, run: no network.
- Two environments: `base` (default branch, pinned SDK) and `fixed` (bumped, patched).
- **L1:** compiles and lints with no new errors. **L2:** no type errors in `fixed`
  that are absent from `base`. **L3:** tests that passed in `base` pass in `fixed`.
- Tests that fail in `base` are "could not run", not failures.
- Up to 2 retries with the error output. No L1 → issue, not a PR.

### 3.7 GitHub integration (FR7)
- GitHub App with installation tokens.
- Permissions: Contents, Pull requests and Issues (read and write), Metadata (read).
  No Workflows.
- One draft upgrade PR per repo and package, on branch `guardian/upgrade-<package>`,
  with bump and fix in one commit. A later release updates the same PR.
- A PR that leaves some call sites unfixed is marked partial and lists them.
- A closed PR is remembered by the changes it covered; the same changes are not
  proposed again.
- Issue fallback: one issue per repo and package, edited in place.
- Config in `.github/guardian.yml`, read from the default branch: `mode`, `packages`,
  `api_changes`. Issue-only mode pushes nothing.

---

## 4. Data model

| Table | Purpose |
|---|---|
| `repos` | Installed repositories and whether they are active |
| `sources` | Each watched source and its poll state |
| `source_documents` | Raw notes and page snapshots per (source, version or snapshot, revision) |
| `change_records` | Extracted changes, SDK-level or API-level, each tied to a document or to the API diff |
| `runs` | One row per (repo, target, head commit): range, outcome, verification level |
| `findings` | Call-site matches per run: file, line, symbol, tier, confidence, fix tier, fix status |
| `pull_requests` | PR or issue per (repo, package or source); covered and declined change IDs |
| `llm_calls` | Every LLM call: input, output, model, tokens, latency, cost |
| `agent_steps` | Every agent step: tool, arguments, result summary, tokens, latency |

---

## 5. Safety, security and reliability rules

| Area | Rule |
|---|---|
| Safety | Never push to the default branch; PRs are always drafts; a human merges |
| Versions | A fix never ships without its version: bump and fix are one commit |
| LLM inputs | Release notes, provider pages, repo code and tool or test output are untrusted data |
| Extraction | No tools; schema-only output; evidence and symbol checks |
| Agent | Fixed tool list; no shell, network, GitHub or write access; path confinement; secret-file deny list; budgets |
| Generated code | Every model-written diff passes the AST guard; the app has no Workflows permission |
| Sandbox | No secrets; PyPI-only network for install, none for run; non-root; resource and time limits |
| Cloud | Metadata server blocked from sandbox containers; separate minimum-role service accounts; secrets only in Secret Manager |
| Blast radius | Release gate; drafts only; a global pause switch that stops all deliveries without a deploy |
| Reliability | Idempotent runs; Pub/Sub redelivery with backoff and a dead-letter topic; GitHub secondary rate limits handled |
| Observability | Every LLM call and agent step logged with cost; a per-run log of changes considered, matched and skipped |
| Performance | 500-file scan in under 30 seconds; a run in under 15 minutes |
| Cost | Extraction cached per document; the gate stops fan-out for harmless releases; agent budget per finding; budget alert on the project |

The defence against prompt injection is structural: it rests on what a model's output
is allowed to do, not on what the prompt asks.

Residual risk: the AST guard narrows what a model-written diff can do but does not
prove it harmless. The PR branch runs in the repo's own CI, so repos with sensitive CI
secrets should use issue-only mode.

---

## 6. Limitations

- Python only; two providers; one API-level source.
- Response-shape and behavioural SDK changes are reported, not fixed.
- A model swap changes behaviour; such PRs are labelled and not verified for output.
- Wrapped clients, dynamically built calls, and values from environment or config at
  runtime are not detected.
- Repos are re-checked when the next breaking release arrives, not on every push.
- New-feature suggestions are not in this version.
- No auto-merging; not a multi-tenant SaaS.

---

## 7. Evaluation and success criteria

Each stage is evaluated separately (full plan in
[`../specs/08-evaluation.md`](../specs/08-evaluation.md)):

| Eval | Dataset | Target |
|---|---|---|
| Extraction | 30 hand-labeled release notes + deprecation entries | Precision ≥ 0.85; recall ≥ 0.80; wrong-symbol rate < 5% |
| Scanner | 6–8 real repos, ~30 fixture cases, 5 negative repos | Recall ≥ 0.85; precision ≥ 0.90 on in-scope patterns |
| Fix | Findings from the scanner set | ≥ 60% reach L2 or higher; false-confidence < 10% |
| Agent vs single-shot | Same findings, both fixers | Fix rate, cost per fix and steps published; no target |
| Agent safety | Fixtures with injected instructions | Zero harmful patches accepted |
| End-to-end | Replay of historical releases | False-alert < 10%; result within 24 hours of release |

**Success =** a real SDK release produces a draft upgrade PR on at least 2 real
repositories through the GCP deployment; one API-level change produces a PR or issue
end to end; published eval numbers including the agent comparison, whether or not they
meet the targets; README with architecture diagram, threat model, limitations and demo
video; public repo with tests, a one-command local setup and a scripted GCP deployment.

---

## 8. Document map

- [PRD v3](https://claude.ai/artifact/8ud7FGK43L3XkVkWmto4xm) — product requirements
- [`../CHECKLIST.md`](../CHECKLIST.md) — phase-by-phase build checklist
- [`../specs/`](../specs/README.md) — one spec per feature, with acceptance criteria
- [`tech-stack.md`](./tech-stack.md) — every library and service and why it was chosen
- [`architecture.md`](./architecture.md) — diagrams: overview, sequence, agent loop, data model, trust boundaries, deployment
- [`../PRD API Change Guardian (GitHub App).pdf`](../PRD%20API%20Change%20Guardian%20(GitHub%20App).pdf) — PRD v1, superseded
