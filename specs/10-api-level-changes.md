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
| F10.1 | A source of kind `api-page` is polled like any other source (F1.2), with conditional requests on `ETag` and `Last-Modified`. v3 ships one: OpenAI's deprecations page, fetched as **Markdown** from `https://developers.openai.com/api/docs/deprecations.md` |
| F10.2 | Each poll that returns new content stores the Markdown byte-for-byte as a new snapshot revision. No HTML reduction is needed |
| F10.3 | **Table parsing (plain code, no LLM):** parse the tables under "Upcoming deprecations" into rows of shutdown date, model IDs, status, replacement and notice date. Compare with the previous snapshot by model ID; only new or changed rows go further |
| F10.4 | Record schema: `provider`, `kind` (`model-retired`, `endpoint-sunset`, `parameter-deprecated`), `identifier` (one record per model ID), `api_family`, `argument` (for example `model`), `replacement` (optional), `replacement_kind` (`single`, `choice`, `with-settings`, `none`), `shutdown_date`, `evidence_quote` (the table row) |
| F10.5 | The LLM is used only for (a) a replacement cell that is not exactly one model ID, to fill `replacement_kind` and any settings it names; and (b) prose sections without a table, such as "Feature and endpoint deprecations". The evidence check from spec 02 (F2.5) applies to both. Confidence is `medium`: there is no API diff to confirm these |
| F10.15 | **API family:** each model ID is mapped to an API family (`chat`, `responses`, `audio.speech`, `audio.transcriptions`, `realtime`, `images`, `embeddings`) using a reviewed prefix table kept in the repo as config, for example `tts-*` → `audio.speech`, `gpt-realtime*` → `realtime`, `gpt-image*` → `images`, `*transcribe*` and `whisper-*` → `audio.transcriptions`. An unknown prefix maps to `unknown` |
| F10.16 | **Fixable only when safe:** a retirement is fixable only when `replacement_kind` is `single` and the replacement's API family equals the retired model's. Every other case (a different family, a choice of models, extra settings, no replacement, `unknown`) is report-only, with the reason stated in the issue or PR |
| F10.6 | Gate and fan-out as in spec 01: only new records start runs, one per active repo, with target `<source>@<revision>` |
| F10.7 | The first ingestion of a source treats every entry whose shutdown date is in the future as new |
| F10.8 | **Scan:** find calls on resolved SDK methods **of the record's API family** that pass the identifier as a string literal to the named argument (spec 04 F4.12), including a module-level constant in the same file used one step away. A match on a method of another family is reported as a possible usage, not fixed |
| F10.9 | Matches of the identifier in non-Python files (YAML, JSON, TOML, `.env.example`) are listed as **possible usages**. They are never edited |
| F10.10 | **Fix:** when F10.16 says the record is fixable, a literal-replacement codemod (F5.3) rewrites the string. There is no version bump. Otherwise the finding is report-only |
| F10.11 | Verification runs with a single environment; L1 and L2 apply, and L3 where tests exist |
| F10.12 | Every PR from this spec is labelled a **behaviour change** in its title and description: output, cost and limits may differ, and no check here verifies that |
| F10.13 | Delivery follows spec 07 with branch `guardian/api-<source>`; the title carries the shutdown date. `api_changes` in the repo config selects `pr`, `issue` or `off` |
| F10.14 | Manual path: `guardian ingest --source <id> --file <path>` stores a saved copy of the page as a snapshot, for when the page cannot be fetched (kept as a fallback; the spike fetched it without trouble) |

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
- [ ] **Real snapshot fixture** (the page as fetched on 2026-11-13, stored in the repo): the parser finds 34 upcoming rows and 52 model IDs with no LLM call, and:
    - `gpt-5` → `gpt-6.1-sol`, shutdown 2026-12-11: `single`, same family, **fixable**.
    - `tts-1` → `gpt-realtime-2.1-mini`: different family, **report-only**.
    - `gpt-image-1.5` → "`gpt-image-2.6-sunburst` or `gpt-image-2.6-flare`": `choice`, **report-only**.
    - `o3-pro` → `gpt-6.1-sol` with `reasoning.mode: pro`: `with-settings`, **report-only**.
    - `gpt-5.5-cyber-preview`: no replacement, **report-only**.
- [ ] GIVEN `client.audio.speech.create(model="tts-1")` and a record retiring `tts-1` in favour of a realtime model, THEN the finding is report-only and the issue says why.
- [ ] GIVEN `"gpt-5"` passed to an images method, THEN no chat-family finding is made; it is listed as a possible usage.
- [ ] GIVEN two fetches with the same `ETag`, THEN the second is a 304 and nothing is stored.

## Technical notes
- This is deliberately simple downstream: the scanner already resolves SDK calls, so
  matching a string argument is a small extension, and the fix is one codemod. The new
  work is the page snapshot and diff.
- Spike results (2026-11-13): `platform.openai.com/docs/deprecations` redirects to
  `developers.openai.com/api/docs/deprecations`; plain HTTP fetch returns 200 with no
  bot challenge; the same URL plus `.md` returns `text/markdown` (64 kB) with `ETag`
  and `Last-Modified`; two fetches were identical. Fetching the Markdown removes the
  HTML-reduction and hash-stability problems this spec originally worried about.
- Why plain code parses the table: the rows are regular, and parsing them without a
  model is cheaper, deterministic, and immune to prompt injection. The LLM only reads
  replacement cells that are prose, and prose sections.
- Why the API-family check exists: of 23 rows that name exactly one replacement on
  2026-11-13, 3 (`tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`) point to a realtime model, a
  different API. Swapping the string there would break the call. The string alone is
  not enough; the SDK method it is passed to decides.
- The prefix table is config, reviewed by a person, and listed in the PR description
  when it decided a case. An `unknown` family is always report-only.
- The evidence quote is the table row, checked against the stored Markdown.
- A model swap is the clearest case where "passes checks" does not mean "correct".
  The label in F10.12 is not optional.
- Values that arrive from environment or config at runtime cannot be seen by a static
  scan. F10.9 exists to make that limit visible to the user instead of silent.

## Test plan
- Unit: table parser and snapshot diff on the stored real snapshot (F10.2, F10.3);
  family mapping and the fixable rule (F10.15, F10.16); LLM path for prose cells with
  a stub (F10.5); literal matching with the family check (F10.8).
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
- 2026-11-13 (spike): the page **can** be fetched by a plain HTTP client, as stable
  Markdown. F10.1–F10.5 rewritten to parse the table with plain code; API-family check
  added (F10.15, F10.16, F10.8). See `docs/spikes/2026-11-13-pre-phase-spike.md`.
- Should `with-settings` replacements (for example `reasoning.mode: pro`) become agent
  work instead of report-only? Not in v3; revisit after the fix eval.
- Demo timing: `gpt-5`, `gpt-5-mini`, `gpt-5-nano` and `o3` shut down on 2026-12-11.
  Phase 5 starts after that date, so the live demo will need a later retirement or a
  replay of the 2026-11-13 snapshot.
- Default for `api_changes`: `pr` makes the better demo; `issue` is the more cautious
  default for a behaviour change. Proposed: `pr`, as a draft with the label.
- How long before the shutdown date should the app act: at once, or only inside a
  window? Proposed: at once.
- 2026-11-13 (v3): new spec. v2 listed server-side changes as out of scope.
