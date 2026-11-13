# Spec 10 — API-level changes
Status: Draft · Phase: 5 · PRD v3 ref: FR3 · Depends on: 01-change-discovery, 02-extraction, 04-repo-scanning, 05-fix-generation, 07-github-integration

## Goal
Cover the changes that break code which changes nothing: a provider retires a model or
sunsets an endpoint on a date, and pinned code stops working that day. v3 handles one
such source end to end, OpenAI's deprecation notices, reusing the same pipeline stages
as SDK changes.

## User stories
- As a developer, when a provider announces that a model I use will be retired, I get
  a draft PR or an issue that names the date and the recommended replacement.
- As a reviewer, I am told plainly that swapping a model changes behaviour and that no
  automated check has verified the new output.

## Requirements
| ID | Requirement |
|---|---|
| F10.1 | A source of kind `api-page` is polled like any other source (F1.2). v3 ships one: OpenAI's deprecations page |
| F10.2 | Each poll fetches the page, reduces it to text, and hashes it. A changed hash stores a new snapshot revision in the blob store |
| F10.3 | Only entries that are new or changed since the previous snapshot are sent to extraction |
| F10.4 | Extraction schema: `provider`, `kind` (`model-retired`, `endpoint-sunset`, `parameter-deprecated`), `identifier` (model ID or endpoint), `sdk_symbol` (optional), `argument` (for example `model`), `replacement` (optional), `shutdown_date`, `evidence_quote` |
| F10.5 | The evidence check from spec 02 (F2.5) applies. Confidence is `medium`: there is no API diff to confirm these |
| F10.6 | Gate and fan-out as in spec 01: only new records start runs, one per active repo, with target `<source>@<revision>` |
| F10.7 | The first ingestion of a source treats every entry whose shutdown date is in the future as new |
| F10.8 | **Scan:** find calls on resolved SDK symbols that pass the identifier as a string literal to the named argument (spec 04 F4.12), including a module-level constant in the same file used one step away |
| F10.9 | Matches of the identifier in non-Python files (YAML, JSON, TOML, `.env.example`) are listed as **possible usages**. They are never edited |
| F10.10 | **Fix:** when the record names a replacement, a literal-replacement codemod (F5.3) rewrites the string. There is no version bump. Without a replacement the finding is report-only |
| F10.11 | Verification runs with a single environment; L1 and L2 apply, and L3 where tests exist |
| F10.12 | Every PR from this spec is labelled a **behaviour change** in its title and description: output, cost and limits may differ, and no check here verifies that |
| F10.13 | Delivery follows spec 07 with branch `guardian/api-<source>`; the title carries the shutdown date. `api_changes` in the repo config selects `pr`, `issue` or `off` |
| F10.14 | Manual path: `guardian ingest --source <id> --file <path>` stores a saved copy of the page as a snapshot, for when the page cannot be fetched by a plain HTTP client |

## Acceptance criteria
- [ ] GIVEN two snapshots where the second adds one entry, THEN exactly one record is extracted from the second.
- [ ] GIVEN an unchanged page, THEN nothing is stored and no runs start.
- [ ] GIVEN a record retiring model `X` with replacement `Y`, and a repo calling `client.chat.completions.create(model="X", …)`, THEN one finding, and the codemod changes only the literal.
- [ ] GIVEN `MODEL = "X"` at module level and `create(model=MODEL)`, THEN the finding points at the constant and the fix edits it.
- [ ] GIVEN `create(model=os.environ["MODEL"])`, THEN no finding in Python, and a match of `X` in `.env.example` is listed as a possible usage.
- [ ] GIVEN the string `"X"` in an unrelated call or a comment, THEN no finding.
- [ ] GIVEN a record with no replacement, THEN an issue is opened with the call sites and the date, and no PR.
- [ ] GIVEN any PR from this spec, THEN its title contains the shutdown date and the behaviour-change label, and its commit changes no dependency file.
- [ ] GIVEN `api_changes: off`, THEN no run output for these changes.
- [ ] GIVEN `guardian ingest --file` with a saved page, THEN the same records result as from a fetch.

## Technical notes
- This is deliberately simple downstream: the scanner already resolves SDK calls, so
  matching a string argument is a small extension, and the fix is one codemod. The new
  work is the page snapshot and diff.
- Page text must be extracted the same way every time, or the hash changes without a
  real change. Strip navigation and scripts and normalise whitespace before hashing.
- The evidence quote is checked against the stored snapshot text.
- A model swap is the clearest case where "passes checks" does not mean "correct".
  The label in F10.12 is not optional.
- Values that arrive from environment or config at runtime cannot be seen by a static
  scan. F10.9 exists to make that limit visible to the user instead of silent.

## Test plan
- Unit: text reduction and hashing stability, snapshot diff (F10.2, F10.3); extraction
  schema and evidence check (F10.4, F10.5); literal matching cases (F10.8).
- Integration: snapshot → record → finding → patch on fixture repos; issue path;
  config switch; manual ingest (F10.6 to F10.14).
- Feeds spec 08: deprecation entries in the extraction set and seeded fixture cases.

## Out of scope
- Stripe dated API versions (stretch goal): the change is tied to the account's API
  version, which the code does not show.
- Suggesting adoption of new features.
- Detecting usage through runtime configuration.
- Any source that needs a login or a headless browser.

## Open questions / decisions
- **Can the deprecations page be fetched by a plain HTTP client, and is its text
  stable enough to diff?** Check in the pre-phase spike. If not, F10.14 is the main
  path for the demo and automated fetching becomes a stretch goal.
- Default for `api_changes`: `pr` makes the better demo; `issue` is the more cautious
  default for a behaviour change. Proposed: `pr`, as a draft with the label.
- How long before the shutdown date should the app act: at once, or only inside a
  window? Proposed: at once.
- 2026-11-13 (v3): new spec. v2 listed server-side changes as out of scope.
