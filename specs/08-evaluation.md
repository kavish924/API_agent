# Spec 08 — Evaluation harness and published results
Status: Draft · Phase: 1–7 · PRD v3 ref: §9, §11 · Depends on: 02-extraction (extraction eval), 04-repo-scanning (scanner eval), 06-verification and 11-agent-fixer (fix eval), 09-gcp-deployment (replay eval)

## Goal
Measure every pipeline stage separately with published, reproducible numbers, so that
a failure can be traced to one stage and the portfolio claims are backed by data. The
fix eval also answers a question of its own: when is an agent worth its cost over a
single model call?

Real breaking changes in two SDKs are scarce, so the scanner and fix evals combine
real repos with seeded fixture repos.

## User stories
- As a maintainer, I can see which changes a run considered, matched or skipped, and
  every step the agent took.
- As a hiring engineer reading the repo, I want eval numbers, the grep vs libcst
  comparison and the agent vs single-shot comparison in one place.

## Requirements
| ID | Requirement |
|---|---|
| F8.1 | **Extraction eval.** Dataset: 30 hand-labeled release notes from openai-python and stripe-python, including every major release, plus the entries of one deprecations-page snapshot. Metrics: precision and recall of (symbol or identifier, change type); wrong-symbol rate. Targets: precision ≥ 0.85, recall ≥ 0.80, wrong-symbol rate < 5% |
| F8.2 | **Scanner eval.** Dataset: 6–8 real repos pinned just before a known break; about 30 fixture cases; 5 negative repos that use the SDK but not the changed symbol. Metrics: call-site recall and precision. Targets on in-scope patterns: recall ≥ 0.85, precision ≥ 0.90 |
| F8.3 | The scanner eval compares three configurations: grep, libcst without name resolution, libcst with resolution |
| F8.4 | Wrapper, dynamic, cross-module and runtime-config patterns are scored separately and published as known limits |
| F8.5 | **Fix eval.** Dataset: findings from the scanner set. Every finding is run through each applicable fixer: codemod, single-shot, agent. Metrics per fixer: share reaching L1, L2 and L3; AST match with the human fix where one exists; false-confidence rate. Targets for the system as shipped: ≥ 60% reach L2 or higher; false-confidence < 10% |
| F8.6 | **Agent comparison.** For single-shot and agent on the same findings: fix rate at L2 or higher, cost per finding, cost per successful fix, steps per session, guard rejections, budget exhaustions. Published as a table and a chart |
| F8.7 | **Agent safety cases.** A set of fixture repos with injected instructions in code, comments and docstrings. Metric: number of harmful patches accepted. Target: zero |
| F8.8 | **End-to-end eval.** Replay of historical releases against the real repos pinned before them, through the deployed app. Metrics: false-alert rate; time from release detection to result. Targets: false-alert < 10%; result within 24 hours of release |
| F8.9 | **API-level cases.** Seeded fixture repos for model retirement: literal, constant, runtime config. Reported with the scanner and fix numbers |
| F8.10 | Labeled data, fixture repos, agent trajectories and the methodology file are committed to the repo |
| F8.11 | Each eval runs from committed data with a single command and writes a results file |
| F8.12 | The README results table and both charts are generated from results files, not hand-edited |
| F8.13 | Results are published whether or not they meet the targets |
| F8.14 | Run log: for any run, the changes considered, matched and skipped can be retrieved with the reason for each |

## Definitions
- **Fixture case:** one small repo with one seeded usage of one real breaking change,
  plus a test that passes on the old SDK version. Usage patterns: direct import,
  alias, client variable, cross-module client, wrapper, dynamic.
- **Wrong-symbol rate:** records whose symbol does not match the label, or that are
  dropped by the symbol-exists check (F3.7), divided by all records.
- **Matches human fix:** the changed call expressions are AST-equal after formatting.
- **False confidence:** the run reported L2 or L3 but manual review finds the fix wrong.
- **False alert:** a PR or issue was produced where no call site was actually affected.
- **Harmful patch:** a patch that adds behaviour unrelated to the migration (a new
  import, network call, file access or changed statement outside the finding).

## Acceptance criteria
- [ ] `make eval-extraction`, `eval-scanner`, `eval-fix` and `eval-e2e` (or equivalents) each run from committed data.
- [ ] Every results file records run date, model and version, dataset version and budget settings.
- [ ] The fix results show one row per fixer and the agent comparison table.
- [ ] The README table and charts are produced by a script from the results files.
- [ ] The scanner results report in-scope and known-limit patterns as separate rows.
- [ ] The agent safety set reports zero harmful patches accepted, or lists each one.
- [ ] For any run ID, one query returns the considered, matched and skipped changes with reasons, and the agent steps.

## Technical notes
- Labeled data is JSONL with a dataset version stamp.
- Finding the 6–8 real repos is a Phase 1 task, done before the scanner is built, so
  that the scanner is not tuned to the eval set.
- Labels for the extraction set are written before running the model on those notes.
- Injection cases are part of both the extraction set and the agent safety set, and
  are reported as their own rows.
- The agent comparison is only fair on findings both fixers attempt. Report how many
  findings codemods already covered, since that is the size of the problem the agent
  is solving.
- Evals run against the real model, never the stub, and record cost.

## Test plan
- The evals are the tests for system-level quality. Unit-test only the metric
  computations against fixtures with known answers.

## Out of scope
- Improving the numbers; that is iteration on specs 02–06, 10 and 11.

## Open questions / decisions
- Which 6–8 real repos anchor the scanner eval (PRD v3 open question).
- Who reviews for false confidence, and on how many samples? Proposed: all L2 and L3
  fixes in the eval set, since the set is small.
- If the agent does not beat single-shot on this dataset, publish that. Proposed
  framing: the result and the cost numbers are the finding either way.
- 2026-11-13 (v3): three-way fix comparison, agent safety set and API-level cases
  added; end-to-end now replays releases, not bump PRs.
