# Project checklist — API Change Guardian

Tick items as they are done. Each phase ends with exit criteria; do not start the next
phase until they are met, or a descope decision is written in the affected spec.
Requirement IDs in brackets point to the specs in [`specs/`](specs/README.md).

This checklist follows PRD v3: the app acts when a provider ships a change, opens one
draft PR with the version bump and the fix together, covers API-level changes, and
fixes hard cases with an agent.

| Phase | Week | Goal | Spec |
|---|---|---|---|
| Pre | before week 1 | Plan is safe to build | — |
| 0 | days 1–2 | Skeleton, schema, interfaces, CI | 00 |
| 1 | 1 | Know the changes | 01, 03, 02 |
| 2 | 2 | Know the repo | 04 |
| 3 | 3 | Bump, fix and verify from the CLI | 05, 06 |
| 4 | 4 | Agent fixer | 11 |
| 5 | 5 | GitHub App and API-level changes | 07, 10 |
| 6 | 6 | Google Cloud | 09 |
| 7 | 7 | Evals, README, demo | 08 |

**The core of the project:** the scanner (Phase 2) decides whether the app speaks, and
the agent fixer behind the AST guard (Phases 3–4) is what makes it an agentic project.
If time is short, protect those two.

---

## Pre-phase — before writing code

- [ ] Read specs 00, 01, 03 and 02; edit what you disagree with; mark them `Approved`
- [x] Spike: diff two real `openai` version pairs with griffe; note load time — [report](docs/spikes/2026-11-13-pre-phase-spike.md)
- [x] Spike: list the real breaking changes in `openai` and `stripe` by hand; count fixable (renames, removals) vs report-only (response shape, behaviour)
- [x] Spike: fetch OpenAI's deprecations page with a plain HTTP client; check the text is stable enough to diff
- [ ] Decide: keep both providers, or swap one if fixable changes are too few — spike recommends keeping both (Stripe supplies the SDK renames)
- [x] Apply the spike's changes to specs 01, 02, 03, 04 and 10 before approving them
- [ ] Decide how the LLM is reached in weeks 1–5: Vertex AI from day one, or a direct API key until week 6
- [ ] Confirm the chosen model supports tool use and structured output
- [ ] Start a list of candidate real repos for the scanner eval (need 6–8, pinned just before a known break)
- [ ] Export PRD v3 into the repo so the public docs do not link to a private page

---

## Phase 0 — Foundation (days 1–2)

- [x] Package layout: `guardian/` with stage packages incl. `agent/` and `apilevel/`; `evals/`, `tests/`, `deploy/` [F0.1]
- [x] Config from environment; `.env.example` committed; `.env` in `.gitignore` [F0.2]
- [x] Alembic migrations create the nine tables [F0.3]
- [x] SQLAlchemy layer works on SQLite and Postgres with only `DATABASE_URL` changed [F0.4]
- [x] `pipeline.poll()` and `pipeline.run()` as plain functions; in-process `JobQueue` with `poll`, `run`, `event` jobs [F0.5]
- [x] Shared LLM client with structured output and tool use; scriptable stub; every call writes an `llm_calls` row [F0.6]
- [x] `BlobStore` interface with local-directory implementation [F0.7]
- [x] Structured JSON logging with a run ID [F0.8]
- [x] Retry-with-backoff HTTP helper [F0.9]
- [x] `guardian` CLI skeleton: `poll`, `run`, `ingest`, plus `migrate` [F0.10]
- [x] CI workflow written: ruff, mypy, pytest on SQLite (3.11, 3.12) and Postgres [F0.11]
- [x] Global pause flag; `deliver()` is the single gate for GitHub writes [F0.12]

**Exit**
- [x] One command sets up a local environment and `pytest` passes (`python scripts/dev.py`, checked on a fresh copy)
- [x] Migrations run twice with no error
- [x] CI is green (Python 3.11, 3.12 and Postgres)
- [x] Spec 00 marked `Verified`

---

## Phase 1 — Know the changes (week 1)

### Change watching (spec 01)
- [x] SDK sources configured: `openai`, `stripe` [F1.1]
- [x] `poll` with ETags; per-source state stored; idempotent [F1.2, F1.3]
- [x] New release bodies stored byte-for-byte; edits become revisions [F1.4, F1.5]
- [ ] Release gate: no breaking or deprecating record → no runs [F1.7] — the API-diff half is live (`gate: api-diff-only`); the release-note half needs spec 02
- [x] Fan-out: one run job per active repo [F1.8]
- [x] Parsers for `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock` [F1.10]
- [x] Old = pinned version (lock wins); new = latest release [F1.11]
- [x] Silent end reasons: `package-not-used`, `version-not-pinned`, `up-to-date` [F1.12]
- [x] Notes for (old, new] fetched on demand and cached [F1.13]
- [x] `CHANGELOG.md` fallback when a release body is empty or only a link; regression on Stripe 16.0.0 [F1.14]
- [x] Gaps recorded when neither source has the version [F1.17]

### API diff and reconciliation (spec 03)
- [x] Fetch wheels from PyPI and load them statically; nothing is executed [F3.1]
- [x] Public API surface per version [F3.2]; old vs new diff [F3.3]
- [x] Rename inferred from removal plus existing replacement [F3.4]
- [x] Surface and diff caches [F3.5]; diff JSON stored [F3.6]
- [x] Symbol-exists check [F3.7]; confidence high or medium [F3.8]
- [x] Diff-only records for unannounced removals [F3.9]
- [x] Gate diff: new release vs the one before it [F3.11]
- [x] `lookup(version, symbol)` for the agent [F3.12]
- [x] Own comparison on griffe's loader, not `find_breaking_changes`; aliases followed into private modules [F3.2, F3.3, F3.13]
- [x] `TypedDict` params keys compared [F3.14]
- [x] Rename candidates paired from removed + added names [F3.4]
- [x] Noise filter (placeholder defaults, class-to-alias in types modules), counted by rule [F3.15]
- [x] Regression: Stripe 15.6.2 → 16.0.0 finds the `Reversal` rename, `request` removals and positional-only params
- [ ] Measure the noise filter on consecutive single `openai` releases before trusting the gate

### Extraction (spec 02)
- [ ] Change-record schema and `change_type` enum [F2.1, F2.2]
- [ ] `fixable` forced false for behaviour and response-shape changes [F2.3]
- [ ] One retry on schema failure, then `extraction-failed` [F2.4]
- [ ] Evidence check with the documented normalisation [F2.5]
- [ ] No tools; note passed as delimited data [F2.6]
- [ ] Cached per (package, version, revision) [F2.7]
- [ ] All three note formats: headings, inline ⚠️, `CHANGELOG.md` sections [F2.11]
- [ ] ⚠️ on an addition yields no record; unmarked "Remove support for" does [F2.12]
- [ ] Dependency changes kept as `behavior-changed` (e.g. `openai` 3.0.0 `http_client`) [F2.13]
- [ ] Real-format fixtures stored: `openai` 2.0.0 and 3.0.0, `stripe` 9.0.0, 13.0.0, 16.0.0

### Evaluation started (spec 08)
- [ ] 30 release notes hand-labeled **before** running the model on them [F8.1]
- [ ] Injection-payload cases added
- [ ] `make eval-extraction` runs and writes a results file [F8.11]
- [ ] 6–8 real repos for the scanner eval chosen and recorded

**Exit**
- [ ] `guardian poll` detects a release, stores it, extracts records and applies the gate
- [ ] For a package and range, a list of change records with confidence is produced
- [ ] Extraction eval recorded (target: precision ≥ 0.85, recall ≥ 0.80, wrong-symbol < 5%)
- [ ] Model chosen and written into spec 02
- [ ] Specs 01, 03, 02 marked `Verified`

---

## Phase 2 — Know the repo (week 2) — core

- [ ] Spec 04 marked `Approved`
- [ ] Scan pinned to the default-branch commit [F4.1]
- [ ] libcst with `QualifiedNameProvider` and `ScopeProvider`: imports, aliases [F4.2]
- [ ] Same-file client tracking and attribute-chain resolution [F4.3] — expect this to take most of the week
- [ ] One-hop cross-module client, tagged separately [F4.4]
- [ ] Parameter check: finding only when the keyword is passed [F4.5]
- [ ] `TypedDict` key matching: keyword arguments and dict-literal keys [F4.13]
- [ ] Default skips and `.guardianignore` [F4.6]
- [ ] Findings with file, line, symbol, tier, confidence [F4.7]
- [ ] No findings ends the run silently [F4.8]
- [ ] Parse errors skipped with a warning [F4.9]
- [ ] 500-file benchmark under 30 seconds [F4.10]
- [ ] About 30 fixture cases built: direct import, alias, client variable, cross-module, wrapper, dynamic
- [ ] 5 negative repos added
- [ ] grep and libcst-without-resolution baselines [F8.3]
- [ ] `make eval-scanner` runs; known-limit patterns reported separately [F8.4]

**Exit**
- [ ] Scanner eval recorded (target on in-scope patterns: recall ≥ 0.85, precision ≥ 0.90)
- [ ] Decision on diff-only records: keep, or restrict to removals
- [ ] Spec 04 marked `Verified`

---

## Phase 3 — Bump, fix and verify (week 3)

### Fix generation (spec 05)
- [ ] Specs 05 and 06 marked `Approved`
- [ ] Bump editor: rewrites only the pinned version [F5.1]
- [ ] Codemods: parameter rename, symbol rename, import path, literal replacement [F5.3]
- [ ] Single-shot fixer (one LLM call, no tools) [F5.4]
- [ ] Fixer mode switch: `agent` or `single-shot` [F5.5]
- [ ] Non-fixable findings passed through as report-only [F5.6]
- [ ] AST guard: changed statements must hold a finding or be an SDK import [F5.7]
- [ ] Guard rejects new non-SDK imports, files without findings, non-`.py` files [F5.8]
- [ ] Diff size cap; no partial application [F5.9, F5.10]
- [ ] Patch assembly: bump and fixes in one patch; `partial` marking [F5.12, F5.13]
- [ ] Adversarial diff test set for the guard written and passing

### Verification (spec 06)
- [ ] Install phase: PyPI-only network through an allow-listing proxy, no secrets [F6.1]
- [ ] Run phase: no network [F6.2]
- [ ] `base` (default branch, pinned SDK) and `fixed` (bumped, patched) environments
- [ ] L1, L2, L3 against the baseline [F6.3–F6.5]
- [ ] "Could not run" and `tests: none` handling [F6.6]
- [ ] Up to 2 retries; best attempt kept [F6.8]
- [ ] No L1 → outcome `unverified` [F6.9]
- [ ] Non-root, resource limits, timeout, cleanup [F6.10]
- [ ] Canaries: no network in run phase; only PyPI in install phase; metadata address blocked; no secrets in the container [F6.11]
- [ ] Lock regeneration in the install phase [F6.14]
- [ ] Partial-patch rule for remaining call sites [F6.16]

**Exit**
- [ ] `guardian run` on a local repo produces one patch with bump and fix, and a verification level
- [ ] All security canaries pass
- [ ] Specs 05 and 06 marked `Verified`

---

## Phase 4 — Agent fixer (week 4) — core

- [ ] Spec 11 marked `Approved`
- [ ] Agent loop on the shared LLM client with native tool use [F11.1–F11.3]
- [ ] Tool `read_file` [F11.4]
- [ ] Tool `search_repo` [F11.5]
- [ ] Tool `lookup_api` over the new SDK surface [F11.6]
- [ ] Tool `get_change` [F11.7]
- [ ] Tool `run_typecheck` backed by the sandbox service [F11.8, F6.13]
- [ ] Tool `propose_patch` through the AST guard; rejection returned as a tool result [F11.9]
- [ ] Tool `cannot_fix` [F11.10]
- [ ] No shell, network, GitHub or write tools [F11.11]
- [ ] Path confinement: escapes, absolute paths and outward symlinks refused [F11.12]
- [ ] Secret-file deny list [F11.13]; tool result size caps [F11.14]
- [ ] Budgets: steps, proposals, tokens, wall clock; per-run cost cap [F11.15, F11.16]
- [ ] Every step written to `agent_steps`; a session can be replayed [F11.17]
- [ ] Agent skipped where a codemod applies [F11.20]
- [ ] Tests with the scripted stub: happy path, guard rejection and retry, budget exhaustion, `cannot_fix`
- [ ] Adversarial fixtures: injected instructions in code, comments, docstrings and type-checker output [F8.7]
- [ ] `make eval-fix` runs codemod, single-shot and agent on the same findings [F8.5]
- [ ] Agent comparison table: fix rate, cost per fix, steps, guard rejections [F8.6]

**Exit**
- [ ] The agent fixes at least one real finding that single-shot gets wrong, or the eval shows it does not and that result is recorded
- [ ] Zero harmful patches accepted on the safety set
- [ ] Fix eval recorded (target: ≥ 60% reach L2 or higher, false-confidence < 10%)
- [ ] Model and default budget written into spec 11
- [ ] Spec 11 marked `Verified`

---

## Phase 5 — GitHub App and API-level changes (week 5)

### GitHub App (spec 07)
- [ ] Spec 07 marked `Approved`
- [ ] App registered; manifest committed; permissions exactly Contents RW, Pull requests RW, Issues RW, Metadata R [F7.1, F7.2]
- [ ] Webhook signature check; 401 on failure [F7.3]
- [ ] Installation events register and deactivate repos [F7.4, F7.20]
- [ ] Catch-up run on install [F7.5]
- [ ] Branch `guardian/upgrade-<package>`; one commit with bump and fix; draft PR against the default branch [F7.6]
- [ ] Nothing is ever pushed to the default branch [F7.7]
- [ ] PR description rendered [F7.8]; partial PRs marked [F7.9]
- [ ] Same PR updated by a later run [F7.10]
- [ ] Closed-PR memory by declined change IDs [F7.11]
- [ ] Issue fallback, edited in place [F7.12]
- [ ] Issue-only mode [F7.13]
- [ ] Link to an open Dependabot or Renovate PR for the same package [F7.14]
- [ ] `.github/guardian.yml` read from the default branch [F7.16]
- [ ] Pause flag honoured [F7.17]; duplicate delivery produces one PR [F7.19]
- [ ] Live test on a real test repo behind a webhook tunnel

### API-level changes (spec 10)
- [ ] Spec 10 marked `Approved`
- [ ] `api-page` source: fetch the `.md` version with conditional requests; snapshot revisions [F10.1, F10.2]
- [ ] Table parser in plain code; only new or changed rows go further [F10.3]
- [ ] Record schema with `api_family`, `replacement_kind`, shutdown date; LLM only for prose cells [F10.4, F10.5]
- [ ] API-family prefix table as reviewed config [F10.15]
- [ ] Fixable only for a single same-family replacement; everything else report-only [F10.16]
- [ ] Literal-argument matching with the method's API family, incl. module-level constants [F4.12, F10.8]
- [ ] Real snapshot fixture (2026-11-13): 34 rows, 52 IDs; the five worked cases in spec 10
- [ ] Demo: `gpt-5` retires 2026-12-11, before this phase; use a later retirement or replay the stored snapshot
- [ ] Possible usages in non-Python files listed, never edited [F10.9]
- [ ] Literal-replacement fix; no version bump [F10.10]
- [ ] Behaviour-change label and shutdown date on every such PR [F10.12, F10.13]
- [ ] `api_changes` config: `pr`, `issue`, `off` [F10.13]
- [ ] Manual path `guardian ingest --file` [F10.14]

**Exit**
- [ ] Installing the App on a test repo starts a catch-up run
- [ ] A real release that affects the repo produces a draft upgrade PR, running locally
- [ ] One deprecation notice produces a PR or issue end to end
- [ ] Specs 07 and 10 marked `Verified`

---

## Phase 6 — Google Cloud (week 6)

- [ ] Spec 09 marked `Approved`
- [ ] GCP project created; billing linked; **budget alert set first** [F9.15]
- [ ] Region chosen for Vertex AI model availability
- [ ] Decision: Cloud SQL or Firestore
- [ ] Two service accounts with minimum roles [F9.10]
- [ ] Secret Manager holds the App private key and webhook secret [F9.8]
- [ ] Pub/Sub topic, pull subscription, dead-letter topic [F9.2]
- [ ] Pub/Sub `JobQueue` implementation; lease extension during a run [F9.3, F9.4]
- [ ] Cloud Scheduler publishes the daily `poll` job [F9.17]
- [ ] Worker-side fan-out publishes `run` jobs [F9.18]
- [ ] Cloud Storage `BlobStore` implementation [F9.7]
- [ ] Cloud SQL instance; migrations applied; worker connects [F9.6]
- [ ] Vertex AI LLM client implementation, incl. tool use [F9.9]
- [ ] Webhook service deployed to Cloud Run; max instances capped [F9.1]
- [ ] Worker VM with Docker; worker as a systemd service [F9.3, F9.16]
- [ ] Firewall rule blocks sandbox networks from 169.254.169.254 [F9.11]
- [ ] No public inbound ports on the VM [F9.12]
- [ ] Pause switch that flips without a deploy [F9.19]
- [ ] Deploy and teardown scripts in `deploy/` [F9.13]
- [ ] Logs in Cloud Logging; alert on dead-lettered messages [F9.14]
- [ ] GitHub App webhook URL switched to the Cloud Run URL

**Exit**
- [ ] Deploy script works on an empty project
- [ ] A scheduled poll leads to a draft upgrade PR on a test repo with no manual step
- [ ] Metadata canary passes on the deployed VM
- [ ] Duplicate delivery, poison message, VM restart and pause tests pass
- [ ] Cloud Run service account cannot read the App key or reach the database
- [ ] Spec 09 marked `Verified`

---

## Phase 7 — Prove it (week 7)

- [ ] Spec 08 marked `Approved`
- [ ] `make eval-e2e`: replay of historical releases through the deployed app [F8.8]
- [ ] Manual review of every L2 and L3 fix for false confidence
- [ ] All results files record run date, model, dataset version and budgets
- [ ] README results table generated from results files [F8.12]
- [ ] Charts in the README: grep vs libcst; single-shot vs agent
- [ ] Results published whether or not targets are met [F8.13]
- [ ] Run log query works: considered, matched, skipped, with reasons and agent steps [F8.14]
- [ ] One agent session written up step by step
- [ ] Limitations updated with measured miss rates
- [ ] Threat model and residual risk in the README
- [ ] Demo video, 2–3 minutes: a real release turns into a draft upgrade PR
- [ ] One-command local setup checked on a clean clone
- [ ] All specs marked `Verified`; open questions closed or recorded as decisions

**Exit**
- [ ] A real SDK release produces a draft upgrade PR on at least 2 real repositories on the GCP deployment
- [ ] One API-level change produces a PR or issue end to end
- [ ] Public repo has tests, eval data, fixture repos, agent trajectories and the deploy script

---

## If time runs short

Cut in this order, and record each cut in the affected spec. The scanner, the AST
guard and the agent fixer are not on this list.

1. [ ] Drop `stripe`; ship with `openai` only
2. [ ] Drop L3 test verification; report L1 and L2 only
3. [ ] API-level changes report by issue only; no auto-fix
4. [ ] Replace Cloud SQL with SQLite on the worker VM's disk
5. [ ] Drop issue-only mode

## Stretch goals

- [ ] Suggest adoption of new features
- [ ] Stack fixes on an existing Dependabot or Renovate PR
- [ ] Type-aware scanning with Pyright
- [ ] Stripe dated API versions
