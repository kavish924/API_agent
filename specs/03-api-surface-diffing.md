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
| F3.2 | Load each version with griffe and build the public API surface: every name reachable from the package without an underscore segment. **Aliases are followed to their targets**, including targets in underscore-private modules (F3.13) |
| F3.3 | Compare old against new directly (not release by release) with **this project's own comparison**, not `griffe.find_breaking_changes` (see technical notes). Output kinds: public name removed; public method removed from a public class; parameter removed; parameter renamed; parameter made required; parameter kind changed (for example keyword to positional-only); `TypedDict` key removed or made required (F3.14) |
| F3.4 | A **rename** is inferred, since neither griffe nor the comparison sees one directly: (a) a removed name whose extracted `replacement` exists in the new surface; or (b) a removed name paired with exactly one added name in the same module or class whose name contains it or is contained by it (for example `Reversal` and `TransferReversal`), reported as a rename **candidate** with medium confidence |
| F3.5 | Cache each version's surface and each (package, old, new) diff; a second run with the same pair downloads nothing |
| F3.6 | Store the diff as JSON in the blob store for audit |
| F3.7 | **Symbol-exists check:** drop any extracted record whose `qualified_symbol` is not in the old version's surface, with reason `symbol-not-in-old-version` |
| F3.8 | **Confidence:** a record whose symbol and change the diff confirms is `high`; a record the diff is silent on is `medium`. Silence never removes a record |
| F3.9 | A removal or signature change in the diff with no matching extracted record becomes a record with `source: diff`, confidence `high`, and no migration hint. It is fixable only where a codemod applies |
| F3.10 | Output for the run: the list of change records with confidence, ready for the scanner |
| F3.11 | **Gate diff:** at poll time, diff each new release against the release before it. A removal or signature change found there counts as a breaking change record for the release gate (F1.7), even when the notes are silent |
| F3.12 | **API lookup:** `lookup(version, symbol)` returns the signature and docstring of a symbol from a cached surface. The agent fixer's `lookup_api` tool uses it (spec 11) |
| F3.13 | **Alias following:** a public name that is an alias (`from pkg._private import Thing as Thing`) is compared through its final target. An alias that cannot be resolved statically is counted and reported, never silently dropped |
| F3.14 | **`TypedDict` params:** for public classes that are `TypedDict`s (request params passed as `**params`), compare keys: a key removed, renamed, or changed from optional to required is a breaking change on every method that takes that type |
| F3.15 | **Noise filter**, applied before the gate (F3.11) and before diff-only records (F3.9): ignore a parameter default that changes from one "not given" placeholder to another; ignore a public class that becomes a type alias of the same name in a generated types module; ignore changes on names under `types` modules that are only used as return types. Every ignored change is counted by rule in the diff JSON (F3.6) |

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
- [ ] GIVEN a fixture package that exports `Reversal` from `pkg._reversal` through `pkg/__init__.py`, and a new version that exports `TransferReversal` instead, THEN `Reversal` is reported removed and paired with `TransferReversal` as a rename candidate.
- [ ] GIVEN a public method on an aliased class whose path parameter changes from positional-or-keyword to positional-only, THEN a parameter-kind change is reported.
- [ ] GIVEN a `TypedDict` params class that loses the key `payment_method_types`, THEN a key removal is reported for that class.
- [ ] GIVEN only placeholder-default changes and class-to-alias changes in a types module, THEN the filtered diff is empty, the release does not pass the gate, and the diff JSON counts each ignored change by rule.
- [ ] **Regression against real data:** Stripe 15.6.2 → 16.0.0 reports the `Reversal` rename candidate, `request` removed from public resource classes, and positional-only path parameters (the spike found 1, 88 and 652 respectively).

## Technical notes
- Fetch the wheel file directly through the PyPI JSON API, unzip it, and point griffe
  at the directory. Never `pip install` here, and do not `pip download` an sdist,
  which can run build code.
- A symbol added and then removed inside the range does not exist in the old surface,
  so F3.7 drops it. That is correct: a repo on the old version cannot be using it.
- Matching a record to a diff entry is by qualified name, then by parameter name for
  signature changes. Document the matching rules next to the code; spec 08 measures
  how often they miss.
- **Why not `griffe.find_breaking_changes`** (spike, 2026-11-13): it skips aliases and
  underscore modules, and Stripe exports its public names as aliases into private
  modules. On Stripe 15.6.2 → 16.0.0 it reported 0 breakages for a release with a
  documented class rename, a removed method and a signature change on every service
  method. A 100-line comparison that follows aliases found all three in 0.2 s after a
  9.5 s load. On `openai` 3.0.0 → 3.49.0 the built-in check flagged 42 public changes,
  of which about 2 break callers; the rest are what F3.15 filters.
- griffe is still the loader: it parses packages statically, resolves aliases, and
  gives parameters with their kinds and defaults. Only the comparison is ours.
- Load times seen in the spike: 1–23 s per version, depending on the OS file cache.
  The per-version surface cache (F3.5) matters more than diff speed.
- What remains invisible here, by design: response-field **type** changes (`openai`
  2.0.0) and dependency swaps (`openai` 3.0.0, `httpx` to HTTPX2). These come only
  from the release notes, at medium confidence.

## Test plan
- Unit: diff → confirmation logic, rename inference, symbol-exists check, confidence
  assignment (F3.4, F3.7, F3.8, F3.9) on small fixture packages.
- Unit: alias following into private modules, `TypedDict` keys, each noise-filter rule
  (F3.13–F3.15) on small fixture packages built to mimic Stripe's and OpenAI's layouts.
- Regression: the Stripe 15.6.2 → 16.0.0 criterion, run against cached wheels so it
  does not depend on PyPI speed.
- Integration: real griffe on fixture packages, cache behaviour, no-execution canary
  (F3.1, F3.5).

## Out of scope
- Server-side API changes that have no trace in the Python package.
- Runtime or behavioural differences; only the symbol surface.

## Open questions / decisions
- Do `source: diff` records add useful findings or mostly noise? Measure in the
  scanner eval; if noise, restrict F3.9 to removals only. The spike supports keeping
  them: `openai` 2.x removed ten public fields in minor releases with no breaking label.
- The F3.15 filter rules come from one `openai` range. Measure them on consecutive
  single releases in Phase 1 before relying on the gate.
- 2026-11-13 (spike): the comparison is our own on top of griffe's loader; alias
  following, `TypedDict` keys, rename candidates and the noise filter added
  (F3.2–F3.4, F3.13–F3.15). See `docs/spikes/2026-11-13-pre-phase-spike.md`.
- 2026-11-13 (v3): two diffs now exist. The gate diff (F3.11) is release vs previous
  release, at poll time. The run diff (F3.3) is the repo's pinned version vs the
  latest, per run. `lookup` added for the agent.
- 2026-11-13: v1's rule "the diff overrides the LLM and rejects the record" is
  replaced by F3.8. The v1 open question (pairwise or first-vs-last) is settled:
  old vs new directly.
