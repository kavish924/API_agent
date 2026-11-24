# Spec 04 — Repository scanning
Status: Implemented · Phase: 2 · PRD v3 ref: FR4 · Depends on: 00-foundation, 02-extraction, 03-api-surface-diffing

> Approved by instruction on 2026-11-18 ("start phase 2") and implemented the same
> day. Moves to `Verified` once the scanner eval has run on the 6–8 real repositories
> the spec 08 plan asks for; so far it has run on 29 written cases (see below).

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
- [x] GIVEN `from openai import OpenAI as OAI` and `OAI().chat.completions.create(...)`, WHEN scanning for a change on that method, THEN the call is found through the alias.
- [x] GIVEN `import openai as oa` and `oa.OpenAI()`, THEN the client is resolved the same way.
- [x] GIVEN a change renaming keyword `engine`, and two calls of which one passes `engine=`, THEN exactly one finding.
- [x] GIVEN a repo that imports the SDK but never uses the changed symbol, THEN zero findings and outcome `no-findings`.
- [x] GIVEN a matching call inside `vendor/` listed in `.guardianignore`, THEN it produces no finding.
- [x] GIVEN a Python file with a syntax error, THEN it is skipped with a warning and other files are scanned.
- [x] GIVEN a local function named `create` unrelated to the SDK, THEN it is not matched (no name-only matching).
- [x] GIVEN a search for literal `"X"` on argument `model`, THEN `create(model="X")` on a resolved client matches, and the same string in an unrelated call, a comment or a docstring does not.
- [x] GIVEN `client.audio.speech.create(model="tts-1")`, THEN the match records API family `audio.speech`. *Tested on the fixture SDK and the family map; the real openai audio call is checked when spec 10 is built.*
- [x] GIVEN a removed params key `display_width` and `create(tools=[{"type": "computer", "display_width": 1024}])`, THEN one finding points at that dict key; GIVEN `stripe.Customer.create(coupon="x")` and a removed key `coupon`, THEN one finding points at that keyword.
- [x] A 500-file benchmark repo scans in under 30 seconds. *3.5 s for 500 files, 50 of them using the SDK.*

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
- 2026-11-18 (resolved): is a positional argument ever affected by a parameter
  rename? Not in the measured ranges: stripe 15.6.2 → 16.0.0 and openai 2.0.0 → 2.54.0
  have no parameter renames at all, and Stripe's 1,034 positional-only changes break
  only keyword use, which F4.5 already checks. Revisit if a later diff reports
  `param-renamed` on a parameter that can be passed positionally.
- 2026-11-18 (implementation): names are resolved through the **old version's API
  surface** (spec 03), never by spelling. That is what makes inheritance
  (`customer.request` → `StripeObject.request`) and client properties
  (`client.chat.completions.create`, `client.v1.customers.retrieve`) resolve.
- 2026-11-18 (implementation): types are inferred from imports, assignments, `with ...
  as`, annotated parameters, class-level declarations, `self.x = ...`, what an SDK
  method or property returns, and constructor calls. A name bound to two different
  things, or to anything unknown, is not guessed; only a `x = None` placeholder is
  ignored.
- 2026-11-18 (implementation): tiers and confidence: `direct`, `aliased` and
  `client-variable` are high; `cross-module` and `shape-match` are medium. A finding's
  confidence is the lower of its tier's and its change's (a rename inferred from names
  alone is medium).
- 2026-11-18 (implementation): `shape-match` is a new tier. When a parameter is typed
  with a union alias (openai's `tools: Iterable[ToolParam]`), the dict literal is
  matched to a `TypedDict` only if all its keys are keys of that type and there are at
  least two. Without it, openai's removed `ComputerToolParam` keys could not be found.
- 2026-11-18 (implementation): a `MISSING` finding (a newly required parameter not
  passed) is not claimed when the call splats `**kwargs`. Positional passing of a new
  required parameter is not checked.
- 2026-11-18 (implementation): when the diff reports one break both as a removed
  `**params` keyword and as a removed `TypedDict` key, only one finding is made per call.
- 2026-11-18 (implementation): local CLI runs are stored too, under a repo named
  `local:<path>` with installation 0; the head is the git commit, marked `-dirty` if
  the working tree has changes. Findings point at change records stored per version
  range (`from..to`), so diff-only changes have a row to point at.
- 2026-11-18 (implementation): the surface now types an unannotated attribute from the
  constructor it is built with (`self.v1 = V1Services(...)`). Without it Stripe's
  `client.v1.*` services did not resolve. This changed the surface format to 2 (spec 03).
- 2026-11-18 (eval): 29 written cases on the real stripe 15.6.2 → 16.0.0 and openai
  2.0.0 → 2.54.0 changes, 5 of them negative and 4 known limits. In scope: recall 1.00
  (24/24), precision 1.00; libcst by name 0.75 and grep 0.71 precision. Known limits:
  0/4, as expected. One "false positive" in the first run was a labelling mistake (a
  real use of a removed type that the case had not marked). The cases were written
  while building the scanner, so they are not an independent test; the real-repository
  eval is.

- 2026-11-13 (spike): API family recorded on literal matches (F4.12) and `TypedDict`
  key matching added (F4.13), following from the spike's changes to specs 03 and 10.
- 2026-11-13 (v3): the scan target is the default branch head, not a bump branch;
  literal-argument matching added for API-level changes.
- 2026-11-13: tree-sitter replaced by libcst; version gating removed; target changed
  from 2 minutes to 30 seconds.
