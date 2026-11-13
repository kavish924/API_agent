# Spec 03 — API-surface diffing and reconciliation
Status: Draft · Phase: 1 · PRD v3 ref: FR2 · Depends on: 00-foundation, 01-change-discovery; reconciliation consumes 02-extraction

## Goal
Diff the public API surface of the run's exact old and new SDK versions with
**griffe**, then use that diff to grade the extracted change records: confirmed
records get high confidence, records the diff cannot see are kept at medium, and
records about symbols that never existed are dropped.

## User stories
- As a reviewer, I want removals and signature changes confirmed against the actual
  package so that I know which claims are facts.
- As a developer, I do not want a real change thrown away just because a static diff
  cannot see it.

## Requirements
| ID | Requirement |
|---|---|
| F3.1 | Fetch the exact old and new versions of the package from PyPI and load them **statically**. Package code is never imported or executed |
| F3.2 | Build the public API surface of each version with griffe. Underscore-private members are excluded |
| F3.3 | Diff old against new directly (not release by release). Output: removed objects, and signature changes (parameter removed, renamed, made required, kind changed) |
| F3.4 | A **rename** is inferred, since griffe does not report one: a symbol removed in the diff whose extracted `replacement` exists in the new surface |
| F3.5 | Cache each version's surface and each (package, old, new) diff; a second run with the same pair downloads nothing |
| F3.6 | Store the diff as JSON in the blob store for audit |
| F3.7 | **Symbol-exists check:** drop any extracted record whose `qualified_symbol` is not in the old version's surface, with reason `symbol-not-in-old-version` |
| F3.8 | **Confidence:** a record whose symbol and change the diff confirms is `high`; a record the diff is silent on is `medium`. Silence never removes a record |
| F3.9 | A removal or signature change in the diff with no matching extracted record becomes a record with `source: diff`, confidence `high`, and no migration hint. It is fixable only where a codemod applies |
| F3.10 | Output for the run: the list of change records with confidence, ready for the scanner |
| F3.11 | **Gate diff:** at poll time, diff each new release against the release before it. A removal or signature change found there counts as a breaking change record for the release gate (F1.7), even when the notes are silent |
| F3.12 | **API lookup:** `lookup(version, symbol)` returns the signature and docstring of a symbol from a cached surface. The agent fixer's `lookup_api` tool uses it (spec 11) |

## Acceptance criteria
- [ ] GIVEN fixture versions `1.2.0` and `1.3.0` where `Client.create(engine=)` became `Client.create(model=)`, THEN the diff reports the parameter change and the matching record is `high`.
- [ ] GIVEN a record for a narrowed literal value that the diff cannot see, THEN the record is kept with confidence `medium`.
- [ ] GIVEN a record naming `Client.does_not_exist`, THEN it is dropped with `symbol-not-in-old-version`.
- [ ] GIVEN `Client.old` removed and a record with `replacement: Client.new` that exists in the new surface, THEN the change is classed as a rename.
- [ ] GIVEN a removed symbol no release note mentions, THEN a `source: diff` record exists for it.
- [ ] GIVEN two runs over the same version pair, THEN PyPI is contacted only for the first.
- [ ] GIVEN a new release that removes a public symbol its notes do not mention, THEN the release passes the gate.
- [ ] GIVEN `lookup("1.3.0", "Client.create")`, THEN the signature and docstring of that version are returned; an unknown symbol returns a clear "not found".
- [ ] A canary fixture package whose `__init__` raises on import loads without error, proving nothing is executed.

## Technical notes
- Fetch the wheel file directly through the PyPI JSON API, unzip it, and point griffe
  at the directory. Never `pip install` here, and do not `pip download` an sdist,
  which can run build code.
- A symbol added and then removed inside the range does not exist in the old surface,
  so F3.7 drops it. That is correct: a repo on the old version cannot be using it.
- Matching a record to a diff entry is by qualified name, then by parameter name for
  signature changes. Document the matching rules next to the code; spec 08 measures
  how often they miss.

## Test plan
- Unit: diff → confirmation logic, rename inference, symbol-exists check, confidence
  assignment (F3.4, F3.7, F3.8, F3.9) on small fixture packages.
- Integration: real griffe on fixture packages, cache behaviour, no-execution canary
  (F3.1, F3.5).

## Out of scope
- Server-side API changes that have no trace in the Python package.
- Runtime or behavioural differences; only the symbol surface.

## Open questions / decisions
- Do `source: diff` records add useful findings or mostly noise? Measure in the
  scanner eval; if noise, restrict F3.9 to removals only.
- 2026-11-13 (v3): two diffs now exist. The gate diff (F3.11) is release vs previous
  release, at poll time. The run diff (F3.3) is the repo's pinned version vs the
  latest, per run. `lookup` added for the agent.
- 2026-11-13: v1's rule "the diff overrides the LLM and rejects the record" is
  replaced by F3.8. The v1 open question (pairwise or first-vs-last) is settled:
  old vs new directly.
