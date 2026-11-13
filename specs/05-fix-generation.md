# Spec 05 — Fix generation
Status: Draft · Phase: 3 · PRD v2 ref: FR5 · Depends on: 04-repo-scanning

## Goal
Produce a **minimal** patch for the fixable findings of a run: deterministic codemods
where possible, an LLM diff otherwise. Every LLM diff must pass an AST guard that
limits what it is able to change, because the patch will run in the repository's own
CI.

## User stories
- As a reviewer, I want the smallest possible diff so that I can approve quickly.
- As a repo owner, I want a hard limit on what generated code can touch so that an
  injected instruction cannot add arbitrary code to my branch.

## Requirements
| ID | Requirement |
|---|---|
| F5.1 | Tier 1: deterministic libcst codemods for parameter renames, symbol renames and import path changes. Formatting and comments are preserved |
| F5.2 | Tier 2: for other fixable findings, call the LLM with the change record, the migration hint and the **enclosing function only**, and request a unified diff limited to the call site |
| F5.3 | Findings whose change record has `fixable: false` get no diff. They are passed through as report-only items with their call sites |
| F5.4 | **AST guard, applied to every Tier 2 diff:** comparing the file before and after, every changed statement must either contain a finding or be an import from the watched SDK |
| F5.5 | The guard rejects a diff that adds an import of any other module, touches a file without findings, or touches a non-`.py` file |
| F5.6 | Diff size cap per finding (configurable, default 40 changed lines). Over the cap is a rejection |
| F5.7 | A rejected diff is never partly applied. The finding is marked `fix-rejected` with the reason |
| F5.8 | Patches apply to the exact revision that was scanned (F4.1), never a re-read of the branch |
| F5.9 | All fixes for a run are combined into one patch set, grouped by file |
| F5.10 | Each attempt records its tier, the diff, the guard result and the LLM cost |
| F5.11 | On a verification failure (spec 06), the fixer accepts the error output for a retry. That output is untrusted input and the retry's diff goes through the same guard |

## Acceptance criteria
- [ ] GIVEN a keyword rename and a call using the old keyword, WHEN the codemod runs, THEN only that argument name changes and the rest of the file is byte-identical.
- [ ] GIVEN an import path change, THEN only the import statement changes.
- [ ] GIVEN an LLM diff that edits the matched call and also adds `import subprocess`, THEN the guard rejects it.
- [ ] GIVEN an LLM diff that changes a statement with no finding, THEN the guard rejects it.
- [ ] GIVEN an LLM diff that edits `.github/workflows/ci.yml` or `pyproject.toml`, THEN the guard rejects it.
- [ ] GIVEN an LLM diff over the line cap, THEN it is rejected and the finding is `fix-rejected`.
- [ ] GIVEN a `response-shape-changed` finding, THEN no diff is produced and the finding appears in the report-only list.
- [ ] GIVEN two fixable findings in one file, THEN one combined patch applies cleanly.

## Technical notes
- The guard works on parsed trees, not on diff text: parse both versions, map changed
  nodes to their enclosing statements, and check each against the set of finding
  statements and SDK imports.
- Request diffs, not whole files: cheaper, and the checks are mechanical.
- Tier 1 output does not need the guard; it is deterministic and covered by tests.
- The guard narrows what a diff can do. It does not prove a diff harmless: a change
  inside the matched statement could still be wrong. That residual risk is why PRs
  are drafts and why comment-only mode exists (spec 07).

## Test plan
- Unit: each codemod with formatting checks (F5.1); guard against a set of adversarial
  diffs (F5.4, F5.5); size cap (F5.6); patch combination (F5.9).
- Integration with a stubbed LLM: tier fallback, rejection handling, retry with error
  output (F5.2, F5.7, F5.11).

## Out of scope
- Whether the patch works (spec 06).
- Changing the dependency files; the bump PR already did that.

## Open questions / decisions
- Default for the diff size cap; revisit after the fix eval.
- Should the guard also reject new call targets inside a matched statement (for
  example wrapping the call in `eval`)? Likely yes; decide when writing the
  adversarial test set.
- 2026-11-13: AST guard and report-only path added for PRD v2.
