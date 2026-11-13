# Project checklist — API Change Guardian

Tick items as they are done. Each phase ends with exit criteria; do not start the next
phase until they are met, or a descope decision is written in the affected spec.
Requirement IDs in brackets point to the specs in [`specs/`](specs/README.md).

| Phase | Week | Goal | Spec |
|---|---|---|---|
| Pre | before week 1 | Plan is safe to build | — |
| 0 | days 1–2 | Skeleton, schema, interfaces, CI | 00 |
| 1 | 1 | Know the changes | 01, 03, 02 |
| 2 | 2 | Know the repo | 04 |
| 3 | 3 | Fix and verify, end to end from the CLI | 05, 06 |
| 4 | 4 | GitHub App | 07 |
| 5 | 5 | Google Cloud | 09 |
| 6 | 6 | Evals, README, demo | 08 |

---

## Pre-phase — before writing code

- [ ] Read specs 00, 01, 03 and 02; edit what you disagree with; mark them `Approved`
- [ ] Feasibility spike: diff two real `openai` version pairs with griffe; note load time
- [ ] Feasibility spike: list the real breaking changes in `openai` and `stripe` by hand; count how many are fixable (renames, removals) vs report-only (response shape, behaviour)
- [ ] Decide: keep both providers, or swap one if fixable changes are too few
- [ ] Decide how the LLM is reached in week 1: Vertex AI from day one, or a direct API key until week 5
- [ ] Start a list of candidate real repos for the scanner eval (need 6–8, pinned just before a known break)
- [ ] Export PRD v2 into the repo so the public docs do not link to a private page

---

## Phase 0 — Foundation (days 1–2)

- [ ] Package layout created: `guardian/` with stage packages, `evals/`, `tests/`, `deploy/` [F0.1]
- [ ] Config module loads from environment; `.env.example` committed; `.env` in `.gitignore` [F0.2]
- [ ] Alembic migrations create the six tables [F0.3]
- [ ] SQLAlchemy layer works on SQLite and Postgres with only `DATABASE_URL` changed [F0.4]
- [ ] `pipeline.run(RunRequest) -> RunResult` exists as a plain function; in-process `JobQueue` [F0.5]
- [ ] Shared LLM client with stub implementation; every call writes an `llm_calls` row [F0.6]
- [ ] `BlobStore` interface with local-directory implementation [F0.7]
- [ ] Structured JSON logging with a run ID [F0.8]
- [ ] Retry-with-backoff HTTP helper [F0.9]
- [ ] `guardian run` CLI skeleton with `--repo`, `--package`, `--from`, `--to` [F0.10]
- [ ] CI on every push: ruff, mypy, pytest [F0.11]

**Exit**
- [ ] One command sets up a local environment and `pytest` passes
- [ ] Migrations run twice with no error
- [ ] CI is green
- [ ] Spec 00 marked `Verified`

---

## Phase 1 — Know the changes (week 1)

### Change discovery (spec 01)
- [ ] Watched-package config: `openai`, `stripe` → their GitHub repos [F1.1]
- [ ] Parsers for `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock` [F1.2]
- [ ] Lock file wins over manifest; range-only manifest ends the run [F1.3]
- [ ] Only upgrades proceed; other cases end with a recorded reason [F1.4]
- [ ] Fetch release notes for every version in (old, new] [F1.5]
- [ ] Cache with ETag revalidation [F1.6]
- [ ] Edited note stored as a new revision under the same key [F1.7]
- [ ] Raw text stored byte-for-byte before processing [F1.8]
- [ ] Missing or empty release recorded as a gap [F1.9]

### API diff and reconciliation (spec 03)
- [ ] Fetch wheels from PyPI and load them statically; nothing is executed [F3.1]
- [ ] Public API surface per version; private members excluded [F3.2]
- [ ] Diff old vs new: removals and signature changes [F3.3]
- [ ] Rename inferred from removal plus existing replacement [F3.4]
- [ ] Surface and diff caches [F3.5]; diff JSON stored [F3.6]
- [ ] Symbol-exists check drops records not in the old surface [F3.7]
- [ ] Confidence: high when confirmed, medium when the diff is silent [F3.8]
- [ ] Diff-only records created for unannounced removals [F3.9]

### Extraction (spec 02)
- [ ] Change-record schema and `change_type` enum in Pydantic [F2.1, F2.2]
- [ ] `fixable` forced false for behaviour and response-shape changes [F2.3]
- [ ] One retry on schema failure, then `extraction-failed` [F2.4]
- [ ] Evidence check with the documented normalisation [F2.5]
- [ ] No tools; note passed as delimited data [F2.6]
- [ ] Cached per (package, version, revision) [F2.7]
- [ ] Empty result cached like any other [F2.10]

### Evaluation started (spec 08)
- [ ] 30 release notes hand-labeled **before** running the model on them [F8.1]
- [ ] Injection-payload cases added to the set
- [ ] `make eval-extraction` runs and writes a results file [F8.8]
- [ ] 6–8 real repos for the scanner eval chosen and recorded

**Exit**
- [ ] For a given package and range, a list of change records with confidence is produced
- [ ] Extraction eval numbers recorded (target: precision ≥ 0.85, recall ≥ 0.80, wrong-symbol < 5%)
- [ ] LLM model chosen and written into spec 02
- [ ] Specs 01, 03, 02 marked `Verified`

---

## Phase 2 — Know the repo (week 2)

- [ ] Spec 04 marked `Approved`
- [ ] Scan pinned to one revision [F4.1]
- [ ] libcst parsing with `QualifiedNameProvider` and `ScopeProvider`: imports, aliases [F4.2]
- [ ] Same-file client tracking and attribute-chain resolution [F4.3]
- [ ] One-hop cross-module client, tagged separately [F4.4]
- [ ] Parameter check: finding only when the keyword is passed [F4.5]
- [ ] Default skips and `.guardianignore` [F4.6]
- [ ] Findings with file, line, symbol, tier, confidence [F4.7]
- [ ] No findings ends the run silently [F4.8]
- [ ] Parse errors skipped with a warning [F4.9]
- [ ] Pre-filter on package name; 500-file benchmark under 30 seconds [F4.10]
- [ ] Findings saved before fix generation [F4.11]
- [ ] About 30 fixture cases built: direct import, alias, client variable, cross-module, wrapper, dynamic
- [ ] 5 negative repos added
- [ ] grep baseline and libcst-without-resolution baseline implemented [F8.3]
- [ ] `make eval-scanner` runs; in-scope and known-limit patterns reported separately [F8.4]

**Exit**
- [ ] Scanner eval numbers recorded (target on in-scope patterns: recall ≥ 0.85, precision ≥ 0.90)
- [ ] Decision on diff-only records: keep, or restrict to removals
- [ ] Spec 04 marked `Verified`

---

## Phase 3 — Fix and verify (week 3)

### Fix generation (spec 05)
- [ ] Specs 05 and 06 marked `Approved`
- [ ] Codemods: parameter rename, symbol rename, import path change [F5.1]
- [ ] LLM fixer with change record and enclosing function only [F5.2]
- [ ] Non-fixable findings passed through as report-only [F5.3]
- [ ] AST guard: changed statements must hold a finding or be an SDK import [F5.4]
- [ ] Guard rejects new non-SDK imports, files without findings, non-`.py` files [F5.5]
- [ ] Diff size cap [F5.6]; no partial application [F5.7]
- [ ] Patch applies to the scanned revision [F5.8]; one combined patch per run [F5.9]
- [ ] Adversarial diff test set for the guard written and passing
- [ ] Retry with error output goes through the same guard [F5.11]

### Verification (spec 06)
- [ ] Install phase: PyPI-only network through an allow-listing proxy, no secrets [F6.1]
- [ ] Run phase: no network [F6.2]
- [ ] `base` and `fixed` environments built
- [ ] L1 compile and lint vs baseline [F6.3]
- [ ] L2 type check vs baseline [F6.4]
- [ ] L3 tests that passed in `base` pass in `fixed` [F6.5]
- [ ] "Could not run" and `tests: none` handling [F6.6]
- [ ] Up to 2 retries; best attempt kept [F6.8]
- [ ] No L1 → outcome `unverified` [F6.9]
- [ ] Non-root, resource limits, timeout, container cleanup [F6.10]
- [ ] Canaries: no network in run phase; only PyPI in install phase; metadata address blocked; no secrets in the container [F6.11]
- [ ] Result summary for the PR description [F6.12]

**Exit**
- [ ] `guardian run` works end to end on a local repo and prints what a PR would contain
- [ ] All security canaries pass
- [ ] Specs 05 and 06 marked `Verified`

---

## Phase 4 — GitHub App (week 4)

- [ ] Spec 07 marked `Approved`
- [ ] GitHub App registered; manifest committed; permissions exactly Contents RW, Pull requests RW, Metadata R [F7.1, F7.2]
- [ ] Webhook signature check; 401 on failure [F7.3]
- [ ] Event filter: `pull_request` opened and synchronize from Dependabot or Renovate [F7.4]
- [ ] Unwatched or non-upgrade PRs end silently [F7.5]
- [ ] Fix branch `guardian/fix-<package>-<version>` and draft PR based on the bump branch [F7.6]
- [ ] Nothing is ever pushed to the bump or default branch [F7.7]
- [ ] PR description rendered: changes table, report-only list, could-not-run summary [F7.8]
- [ ] Same fix PR updated when the bump PR moves [F7.9]
- [ ] Closed fix PR remembered and not reopened [F7.10]
- [ ] Comment fallback, edited in place on later runs [F7.11]
- [ ] Comment-only mode via `.github/guardian.yml` read from the default branch [F7.12]
- [ ] Duplicate webhook delivery produces one PR [F7.15]
- [ ] Live test on a real test repo behind a webhook tunnel
- [ ] Checked on a test repo: what GitHub does to the fix PR when the bump PR is merged first
- [ ] `make eval-fix` runs [F8.5]

**Exit**
- [ ] A real Dependabot PR produces a draft fix PR, running locally
- [ ] Fix eval numbers recorded (target: ≥ 60% reach L2 or higher, false-confidence < 10%)
- [ ] Spec 07 marked `Verified`

---

## Phase 5 — Google Cloud (week 5)

- [ ] Spec 09 marked `Approved`
- [ ] GCP project created; billing linked; **budget alert set first** [F9.15]
- [ ] Region chosen for Vertex AI model availability
- [ ] Decision: Cloud SQL or Firestore
- [ ] Two service accounts with minimum roles [F9.10]
- [ ] Secret Manager holds the App private key and webhook secret [F9.8]
- [ ] Pub/Sub topic, pull subscription, dead-letter topic [F9.2]
- [ ] Pub/Sub `JobQueue` implementation; lease extension during a run [F9.3, F9.4]
- [ ] Cloud Storage `BlobStore` implementation [F9.7]
- [ ] Cloud SQL instance; migrations applied; worker connects [F9.6]
- [ ] Vertex AI LLM client implementation [F9.9]
- [ ] Webhook service image built and deployed to Cloud Run; max instances capped [F9.1]
- [ ] Worker VM with Docker; worker as a systemd service [F9.3, F9.16]
- [ ] Firewall rule blocks sandbox networks from 169.254.169.254 [F9.11]
- [ ] No public inbound ports on the VM [F9.12]
- [ ] Deploy and teardown scripts in `deploy/` [F9.13]
- [ ] Structured logs in Cloud Logging; alert on dead-lettered messages [F9.14]
- [ ] GitHub App webhook URL switched to the Cloud Run URL

**Exit**
- [ ] Deploy script works on an empty project
- [ ] A bump PR on a test repo produces a fix PR through the deployed system
- [ ] Metadata canary passes on the deployed VM
- [ ] Duplicate delivery, poison message and VM restart tests pass
- [ ] Cloud Run service account cannot read the App key or reach the database
- [ ] Spec 09 marked `Verified`

---

## Phase 6 — Prove it (week 6)

- [ ] Spec 08 marked `Approved`
- [ ] `make eval-e2e`: replay of historical bump PRs against the deployed app [F8.6]
- [ ] Manual review of every L2 and L3 fix for false confidence
- [ ] All four results files record run date, model and dataset version
- [ ] README results table generated from results files [F8.9]
- [ ] grep vs libcst chart in the README
- [ ] Results published whether or not targets are met [F8.10]
- [ ] Run log query works: considered, matched, skipped, with reasons [F8.11]
- [ ] Limitations section updated with measured miss rates on wrapper and dynamic patterns
- [ ] Threat model and residual risk in the README
- [ ] Demo video, 2–3 minutes: a real bump PR turns into a draft fix PR
- [ ] One-command local setup checked on a clean clone
- [ ] All specs marked `Verified`; open questions closed or recorded as decisions

**Exit**
- [ ] End-to-end demo on at least 2 real repositories on the GCP deployment
- [ ] Public repo has tests, eval data, fixture repos and the deploy script

---

## If time runs short

Cut in this order, and record each cut in the affected spec:

1. [ ] Drop `stripe`; ship with `openai` only
2. [ ] Drop L3 test verification; report L1 and L2 only
3. [ ] Drop comment-only mode
4. [ ] Replace Cloud SQL with SQLite on the worker VM's disk
