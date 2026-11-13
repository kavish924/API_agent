# Spec 04 — Repository scanning
Status: Draft · Phase: 2 · PRD v3 ref: FR4 · Depends on: 00-foundation, 02-extraction, 03-api-surface-diffing

## Goal
For a run's change records and the repository at the default branch head, find the exact
call sites each change affects, or find nothing and end the run silently. Precision
and low noise are the product.

## User stories
- As a developer, I only hear about changes that touch code in my repo so that every
  PR or issue is worth reading.
- As a reviewer, I get file, line, matched symbol and confidence for every finding so
  that I can jump straight to the code.
- As a developer, I can exclude vendored or generated code with an ignore file.

## Requirements
| ID | Requirement |
|---|---|
| F4.1 | Scan the repository at one pinned revision: the head commit of the default branch at the start of the run (or the working tree for the CLI) |
| F4.2 | Parse Python files with **libcst** and resolve names with its metadata providers (`QualifiedNameProvider`, `ScopeProvider`): imports, `from` imports, aliases |
| F4.3 | Track simple client assignments in the same file (a variable or attribute bound to a client class instance) and resolve attribute chains to qualified names, e.g. `client = OpenAI(); client.chat.completions.create(...)` |
| F4.4 | Should: follow one hop to a module-level client imported from another module in the same repo (`from app.clients import client`). Findings from this path are tagged so the eval can report them separately |
| F4.5 | Match call sites and references to each change record's `qualified_symbol`. For a parameter change, the finding exists only if the call passes that parameter by keyword |
| F4.6 | Skip by default: virtualenvs, `site-packages`, `node_modules`, `.git`, build output. Also skip every path matched by `.guardianignore` (gitignore syntax) at the repo root |
| F4.7 | Emit findings with `file`, `line`, `matched_symbol`, `change_record`, `match_tier`, `confidence` |
| F4.8 | **No findings → the run ends** with outcome `no-findings`. Nothing is passed downstream and nothing is posted |
| F4.9 | A file that fails to parse is skipped with a logged warning; the scan continues |
| F4.10 | A 500-file repo scans in under 30 seconds on a cold cache. Files that do not mention the package name are skipped before parsing |
| F4.11 | Findings are written to the database before fix generation starts |
| F4.12 | **Literal-argument matching** (used by spec 10): find calls on a resolved SDK method that pass a given string literal to a named argument, for example `model="…"`. A module-level constant in the same file, used one step away, also matches, and the finding points at the constant. Each match records the method's **API family** (spec 10 F10.15), from a reviewed map of SDK resource paths to families (for example `chat.completions.create` → `chat`, `audio.speech.create` → `audio.speech`) |
| F4.13 | **`TypedDict` key matching** (for spec 03 F3.14): a removed or renamed params key is found when passed (a) as a keyword argument to a method that takes that params type as `**params` (Stripe style), or (b) as a key of a dict literal passed to the argument typed with it, one level deep, including dicts inside a list literal (for example `tools=[{"type": …, "display_width": …}]`) |

## Acceptance criteria
- [ ] GIVEN `from openai import OpenAI as OAI` and `OAI().chat.completions.create(...)`, WHEN scanning for a change on that method, THEN the call is found through the alias.
- [ ] GIVEN `import openai as oa` and `oa.OpenAI()`, THEN the client is resolved the same way.
- [ ] GIVEN a change renaming keyword `engine`, and two calls of which one passes `engine=`, THEN exactly one finding.
- [ ] GIVEN a repo that imports the SDK but never uses the changed symbol, THEN zero findings and outcome `no-findings`.
- [ ] GIVEN a matching call inside `vendor/` listed in `.guardianignore`, THEN it produces no finding.
- [ ] GIVEN a Python file with a syntax error, THEN it is skipped with a warning and other files are scanned.
- [ ] GIVEN a local function named `create` unrelated to the SDK, THEN it is not matched (no name-only matching).
- [ ] GIVEN a search for literal `"X"` on argument `model`, THEN `create(model="X")` on a resolved client matches, and the same string in an unrelated call, a comment or a docstring does not.
- [ ] GIVEN `client.audio.speech.create(model="tts-1")`, THEN the match records API family `audio.speech`.
- [ ] GIVEN a removed params key `display_width` and `create(tools=[{"type": "computer", "display_width": 1024}])`, THEN one finding points at that dict key; GIVEN `stripe.Customer.create(coupon="x")` and a removed key `coupon`, THEN one finding points at that keyword.
- [ ] A 500-file benchmark repo scans in under 30 seconds.

## Technical notes
- Match tiers, from most to least certain: direct qualified name → aliased import →
  same-file client variable → cross-module client (F4.4).
- Finding confidence is the lower of the change record's confidence (spec 03) and the
  tier's fixed confidence. The mapping is a documented table, not a model output.
- The scanner uses the same libcst trees and positions the fixer uses (spec 05), so a
  finding points at a node the codemod can rewrite.
- There is no version gating. The run's range (spec 01) already decided which changes
  apply.
- Scanning is deterministic on purpose, even though the fixer is an agent. The scanner
  decides whether the app speaks at all, so its precision must be measurable and
  repeatable.

## Test plan
- Unit: import and alias resolution (F4.2), client tracking (F4.3, F4.4), parameter
  check (F4.5), ignore rules (F4.6), parse-error handling (F4.9).
- Integration: full scan of 2–3 fixture repos with known expected findings, including
  a repo with none (F4.7, F4.8).
- Benchmark: F4.10.
- Feeds spec 08 scanner eval (recall ≥ 0.85, precision ≥ 0.90 on in-scope patterns).

## Out of scope
- Type-aware analysis (Pyright is a stretch goal).
- Wrapped clients (the SDK hidden behind the repo's own class) and dynamically built
  calls (`getattr`, `**kwargs` dicts). These are measured and reported as known limits.
- Languages other than Python.

## Open questions / decisions
- Is a positional argument ever affected by a parameter rename in the two SDKs? If
  yes, F4.5 needs a positional rule. Check during Phase 2.
- 2026-11-13 (spike): API family recorded on literal matches (F4.12) and `TypedDict`
  key matching added (F4.13), following from the spike's changes to specs 03 and 10.
- 2026-11-13 (v3): the scan target is the default branch head, not a bump branch;
  literal-argument matching added for API-level changes.
- 2026-11-13: tree-sitter replaced by libcst; version gating removed; target changed
  from 2 minutes to 30 seconds.
