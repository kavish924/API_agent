# Spec 08 — Evaluation harness and published results
Status: Draft · Phase: 1–6 · PRD v2 ref: §9, §11 · Depends on: 02-extraction (extraction eval), 04-repo-scanning (scanner eval), 06-verification (fix eval), 09-gcp-deployment (replay eval)

## Goal
Measure every pipeline stage separately with published, reproducible numbers, so that
a failure can be traced to one stage and the portfolio claims are backed by data.
Real breaking changes in two SDKs are scarce, so the scanner and fix evals combine
real repos with seeded fixture repos.

## User stories
- As a maintainer, I can see which changes a run considered, matched or skipped, and
  why.
- As a hiring engineer reading the repo, I want eval numbers and the grep vs libcst
  comparison in one table.

## Requirements
| ID | Requirement |
|---|---|
| F8.1 | **Extraction eval.** Dataset: 30 hand-labeled release notes from openai-python and stripe-python, including every major release. Metrics: precision and recall of (symbol, change type); wrong-symbol rate. Targets: precision ≥ 0.85, recall ≥ 0.80, wrong-symbol rate < 5% |
| F8.2 | **Scanner eval.** Dataset: 6–8 real repos pinned just before a known break; about 30 fixture cases; 5 negative repos that use the SDK but not the changed symbol. Metrics: call-site recall and precision. Targets on in-scope patterns: recall ≥ 0.85, precision ≥ 0.90 |
| F8.3 | The scanner eval compares three configurations: grep, libcst without name resolution, libcst with resolution |
| F8.4 | Wrapper, dynamic and cross-module patterns are scored separately and published as known limits, not folded into the headline number |
| F8.5 | **Fix eval.** Dataset: findings from the scanner set. Metrics: share reaching L1, L2 and L3; AST match with the human fix where one exists; false-confidence rate. Targets: ≥ 60% reach L2 or higher; false-confidence < 10% |
| F8.6 | **End-to-end eval.** Replay of historical bump PRs on the real repos, against the deployed app. Metrics: false-alert rate; median time from bump PR to result. Targets: false-alert < 10%; median < 10 minutes |
| F8.7 | Labeled data, fixture repos and the methodology file are committed to the repo |
| F8.8 | Each eval runs from committed data with a single command and writes a results file |
| F8.9 | The README results table and the grep vs libcst chart are generated from results files, not hand-edited |
| F8.10 | Results are published whether or not they meet the targets |
| F8.11 | Run log: for any run, the changes considered, matched and skipped can be retrieved with the reason for each |

## Definitions
- **Fixture case:** one small repo with one seeded usage of one real breaking change,
  plus a test that passes on the old SDK version. Usage patterns: direct import,
  alias, client variable, cross-module client, wrapper, dynamic.
- **Wrong-symbol rate:** records whose symbol does not match the label, or that are
  dropped by the symbol-exists check (F3.7), divided by all records.
- **Matches human fix:** the changed call expressions are AST-equal after formatting.
- **False confidence:** the run reported L2 or L3 but manual review finds the fix wrong.
- **False alert:** a PR or comment was produced where no call site was actually affected.

## Acceptance criteria
- [ ] `make eval-extraction`, `eval-scanner`, `eval-fix` and `eval-e2e` (or equivalents) each run from committed data.
- [ ] Every results file records run date, model and version, and dataset version.
- [ ] The README table is produced by a script from the results files.
- [ ] The chart shows the three scanner configurations.
- [ ] The scanner results report in-scope and known-limit patterns as separate rows.
- [ ] For any run ID, one query returns the considered, matched and skipped changes with reasons.

## Technical notes
- Labeled data is JSONL with a dataset version stamp.
- Finding the 6–8 real repos is a Phase 1 task, done before the scanner is built, so
  that the scanner is not tuned to the eval set.
- Labels for the extraction set are written before running the model on those notes.
- Injection cases (release notes with embedded instructions) are part of the
  extraction set and reported as their own row.
- The LLM stub is not used in evals; they run against the real model and record its cost.

## Test plan
- The evals are the tests for system-level quality. Unit-test only the metric
  computations against fixtures with known answers.

## Out of scope
- Improving the numbers; that is iteration on specs 02–06.

## Open questions / decisions
- Which 6–8 real repos anchor the scanner eval (PRD v2 open question).
- Who reviews for false confidence, and on how many samples? Proposed: all L2 and L3
  fixes in the eval set, since the set is small.
- 2026-11-13: dataset sizes and the end-to-end metric changed from v1 (40–50 notes,
  15–20 repos, alerts per month).
