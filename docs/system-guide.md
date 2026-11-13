# API Change Guardian — System Guide

> A GitHub App that makes SDK upgrade PRs mergeable: when Dependabot or Renovate bumps
> a Python SDK, it finds the call sites the upgrade breaks and opens a draft pull
> request with fixes tested against the new version.

| | |
|---|---|
| **Author** | Kavish |
| **Status** | Draft v2 (source: [PRD v2](https://claude.ai/artifact/8ud7FGK43L3XkVkWmto4xm)) |
| **Timeline** | 6 weeks |
| **Language** | Python only |
| **Providers** | openai-python, stripe-python |
| **Hosting** | Google Cloud |

---

## 1. What the system does (end-to-end flow)

1. **Trigger** — Dependabot or Renovate opens or updates a PR. GitHub sends a webhook;
   the app service checks its signature and queues a run.
2. **Discover** — The worker reads the old and new version of a watched package from
   the dependency-file diff, fetches the release notes for every version in between,
   and diffs the public API of the two versions with griffe.
3. **Extract** — An LLM converts each release note into structured change records.
   This happens once per note and is reused by every repo.
4. **Reconcile** — Records with a bad evidence quote, or about a symbol that never
   existed in the old version, are dropped. Records the API diff confirms are high
   confidence; the rest are medium.
5. **Scan** — The repo at the bump branch head is scanned with libcst for call sites
   matching the changed symbols. No findings ends the run with no output.
6. **Fix** — Simple cases are fixed by deterministic codemods. Other fixable cases go
   to an LLM, and its diff must pass the AST guard. Non-fixable changes are only
   reported.
7. **Verify** — A Docker sandbox installs dependencies at the new SDK version, then
   runs lint, type check and tests with the network off. The result is a level.
8. **Deliver** — L1 or higher → a draft PR based on the bump branch. Otherwise → one
   comment on the bump PR with the affected call sites.

---

## 2. Core components

| Component | Responsibility | Key detail |
|---|---|---|
| **App service** | Receive webhooks | FastAPI on Cloud Run; signature check; publishes one message per run; no database access |
| **Job queue** | Hand runs to the worker | Pub/Sub when deployed, in-process locally |
| **Change discovery** | Version range and release notes | Reads dependency files, not the PR title; cache keyed on package and version with revisions |
| **API differ** | Facts about the SDK | griffe, static load, old vs new version directly |
| **LLM extractor** | Release notes → change records | Strict schema; evidence-quote check; no tools |
| **Scanner** | Find affected call sites | libcst with name and scope resolution; ignore rules |
| **Fixer** | Produce a minimal patch | Codemods first; LLM diff behind the AST guard |
| **Sandbox runner** | Verify the patch | Two phases; levels L1 to L3; baseline comparison |
| **PR writer** | Deliver the result | Draft fix PR on its own branch, or one comment |

---

## 3. Pipeline stages in detail

### 3.1 Trigger and version range (FR1)
- Events: `pull_request` opened and synchronize, authored by `dependabot[bot]` or
  `renovate[bot]`.
- The old and new version come from the diff of `requirements*.txt`,
  `pyproject.toml`, `poetry.lock` or `uv.lock`. A lock file wins over a manifest.
- Only upgrades of a watched package proceed.
- The CLI runs the same pipeline: `guardian run --repo PATH --package openai --from 1.x --to 1.y`.

### 3.2 Change discovery (FR2)
- Release notes for every version in (old, new] are fetched from GitHub Releases on
  demand and cached. An edited note is stored as a new revision under the same key.
- The public API of the exact old and new versions is diffed with griffe. Package
  code is never executed.

### 3.3 Extraction and reconciliation (FR3)
- Schema: `provider`, `package`, `version`, `change_type`, `qualified_symbol`,
  `affected_parameter`, `replacement`, `fixable`, `evidence_quote`, `migration_hint`.
- Validation failure → one retry. Evidence quote not in the note (after normalising
  whitespace and Markdown) → record rejected.
- Per run: a record whose symbol is not in the old version's API is dropped.
- Confidence: **high** when the API diff confirms the change, **medium** when only the
  notes state it. Medium records are kept.

### 3.4 Repository scanning (FR4)
- Parses Python with libcst; resolves imports, aliases and scopes; tracks simple
  client assignments and resolves attribute chains.
- For parameter changes, a finding exists only when the call passes that parameter.
- Skips virtualenvs and `site-packages`, and paths in `.guardianignore`.
- **No findings → no output.**

### 3.5 Fix generation (FR5)
- Tier 1: libcst codemods for parameter renames, symbol renames and import path
  changes.
- Tier 2: LLM diff limited to the call site, given the change record and the
  enclosing function.
- **AST guard** on every LLM diff: changed statements must contain a finding or be an
  SDK import; no new imports of other modules; `.py` files with findings only.
- Response-shape and behavioural changes get no diff; they are listed with call sites.

### 3.6 Verification (FR6)
- Phase 1, install: PyPI-only network, no secrets, metadata server blocked.
- Phase 2, run: no network.
- **L1:** compiles and lints with no new errors. **L2:** no new type errors against
  the new SDK. **L3:** tests that passed before the bump still pass.
- Tests that already fail before the bump are "could not run", not failures.
- Up to 2 retries with the error output. No L1 → comment, not a PR.

### 3.7 GitHub integration (FR7)
- GitHub App with installation tokens.
- Permissions: Contents (read and write), Pull requests (read and write), Metadata
  (read). No Workflows, no Issues.
- One draft fix PR per bump PR, on branch `guardian/fix-<package>-<version>`, based on
  the bump branch. Updated when the bump PR moves.
- A closed fix PR is remembered by (repo, package, target version) and not reopened.
- Comment-only mode posts the diff as a comment and pushes nothing.

---

## 4. Data model

| Table | Purpose |
|---|---|
| `release_notes` | Raw notes per (package, version, revision) |
| `change_records` | Extracted changes, each tied to one note revision or to the API diff |
| `runs` | One row per (repo, package, target version, head SHA): range, outcome, verification level |
| `findings` | Call-site matches per run: file, line, symbol, tier, confidence, fix status |
| `pull_requests` | Fix PR and comment per (repo, package, target version); remembers closed PRs |
| `llm_calls` | Every LLM call: input, output, model, tokens, latency, cost |

---

## 5. Safety, security and reliability rules

| Area | Rule |
|---|---|
| Safety | Never push to the default branch or the bump branch; fix PRs are always drafts; a human merges |
| LLM inputs | Release notes, repo code and test output are untrusted data; no LLM call has tools |
| Generated code | Every LLM diff passes the AST guard; the app has no Workflows permission |
| Sandbox | No secrets; PyPI-only network for install, none for run; non-root; resource and time limits |
| Cloud | Metadata server blocked from sandbox containers; separate minimum-role service accounts; secrets only in Secret Manager |
| Reliability | Idempotent runs; Pub/Sub redelivery with backoff and a dead-letter topic; GitHub secondary rate limits handled |
| Observability | Every LLM call logged with cost; a per-run log of changes considered, matched and skipped |
| Performance | 500-file scan in under 30 seconds; whole run in under 10 minutes |
| Cost | Extraction cached per SDK version; LLM fix calls capped per run; budget alert on the project |

Residual risk: the AST guard narrows what an LLM diff can do but does not prove it
harmless. The fix branch runs in the repo's own CI, so repos with sensitive CI secrets
should use comment-only mode.

---

## 6. Limitations

- Python only; two SDKs.
- Acts only on SDK version bumps. Server-side changes that break pinned code are out
  of scope.
- Response-shape and behavioural changes are reported, not fixed.
- Wrapped clients and dynamically built calls are not detected.
- No auto-merging; not a multi-tenant SaaS.

---

## 7. Evaluation and success criteria

Each stage is evaluated separately (full plan in
[`../specs/08-evaluation.md`](../specs/08-evaluation.md)):

| Eval | Dataset | Target |
|---|---|---|
| Extraction | 30 hand-labeled release notes | Precision ≥ 0.85; recall ≥ 0.80; wrong-symbol rate < 5% |
| Scanner | 6–8 real repos, ~30 fixture cases, 5 negative repos | Recall ≥ 0.85; precision ≥ 0.90 on in-scope patterns |
| Fix | Findings from the scanner set | ≥ 60% reach L2 or higher; false-confidence < 10% |
| End-to-end | Replay of historical bump PRs | False-alert < 10%; median time < 10 minutes |

**Success =** end-to-end demo on at least 2 real repositories running on the GCP
deployment; published eval numbers whether or not they meet the targets; README with
architecture diagram, threat model, limitations and demo video; public repo with
tests, a one-command local setup and a scripted GCP deployment.

---

## 8. Document map

- [PRD v2](https://claude.ai/artifact/8ud7FGK43L3XkVkWmto4xm) — product requirements
- [`../specs/`](../specs/README.md) — one spec per feature, with acceptance criteria
- [`tech-stack.md`](./tech-stack.md) — every library and service and why it was chosen
- [`architecture.md`](./architecture.md) — diagrams: overview, sequence, data model, trust boundaries, deployment
- [`../PRD API Change Guardian (GitHub App).pdf`](../PRD%20API%20Change%20Guardian%20(GitHub%20App).pdf) — PRD v1, superseded
