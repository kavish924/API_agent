# Spec 01 — Change watching and discovery
Status: In progress · Phase: 1 · PRD v3 ref: FR1, FR2 (release notes) · Depends on: 00-foundation

> Approved by instruction on 2026-11-14 ("start implementing phase 1"). Built so far:
> everything in this spec that does not depend on another spec. Still open: the release
> gate needs an assessor from specs 02 and 03 (F1.7), and catch-up runs and reading an
> installed repository need the GitHub App from spec 07 (F1.9, F1.10).

## Goal
Notice when a provider ships a change, decide whether it is worth acting on, and start
one run per installed repository. For each run, establish the exact version range (the
repo's pinned version → the latest release) and the stored release notes for it.

## User stories
- As a developer, I want provider changes picked up within a day so that fixes arrive
  while the change is still news.
- As a developer, I want the range taken from what my repo actually pins so that the
  fix targets my upgrade, not someone else's.
- As a maintainer, I want releases with no breaking change to cost nothing downstream.

## Requirements

### Watching
| ID | Requirement |
|---|---|
| F1.1 | SDK sources are configured as package → GitHub repo. v3 ships `openai` → `openai/openai-python` and `stripe` → `stripe/stripe-python`. API-page sources are defined in spec 10 |
| F1.2 | `pipeline.poll()` checks every source, once a day by default, using ETags and conditional requests. A 304 costs nothing |
| F1.3 | Per-source state (ETag, last seen version) is stored in `sources`. A poll run twice produces no duplicate rows and no duplicate jobs |
| F1.4 | Each new release body is stored byte-for-byte in the blob store before any processing, keyed on (source, version, revision) |
| F1.5 | An edited release body becomes a new revision under the same key. It is re-extracted, and re-enters the gate only if its change records differ |
| F1.6 | A failing source backs off and retries; other sources still complete |

### Gate and fan-out
| ID | Requirement |
|---|---|
| F1.7 | **Release gate:** after extraction (spec 02) and the consecutive API diff (spec 03 F3.11), a release with no breaking or deprecating change record starts no runs |
| F1.8 | **Fan-out:** a release that passes the gate enqueues one `run` job per active repo in `repos`, with target `<package>@<version>` |
| F1.9 | **Catch-up:** installing the app enqueues one run per watched package for that repo |

### Version range per run
| ID | Requirement |
|---|---|
| F1.10 | The run reads the repo's dependency files at the default-branch head through the GitHub contents API, before any clone: `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock` |
| F1.11 | Old version = the pinned version; a lock file wins over a manifest. New version = the latest release |
| F1.12 | The run ends with a recorded reason, and no output, when the repo does not use the package (`package-not-used`), pins no exact version (`version-not-pinned`), or is already current (`up-to-date`) |
| F1.13 | Release notes for every version in (old, new] are fetched on demand and cached. Versions released before the app started watching are backfilled the same way |
| F1.14 | **`CHANGELOG.md` fallback (required):** when a release body is empty, or contains only a pointer elsewhere (for example "See the changelog for the full release notes"), read the section for that version from the repository's `CHANGELOG.md` and store that as the document. The changelog file is fetched with a conditional request and cached like any source document |
| F1.17 | A version with neither a usable release body nor a changelog section is recorded as a gap; the run continues |
| F1.15 | A cap on range size (default 200 releases) ends the run with `range-too-large` |
| F1.16 | CLI: `guardian poll`, and `guardian run --repo PATH --package openai [--from X --to Y]` where the flags override F1.10 and F1.11 |

## Acceptance criteria
- [ ] GIVEN a stubbed GitHub API with one new release, WHEN `poll` runs, THEN one source document is stored and extraction is requested once. *Storing is done and tested; "extraction is requested" waits for spec 02.*
- [x] GIVEN the same poll twice, THEN the second makes conditional requests, stores nothing and enqueues nothing.
- [x] GIVEN a release whose records are all non-breaking (empty list), THEN no run job is enqueued. *Tested with a stand-in assessor; the real one comes from specs 02 and 03.*
- [x] GIVEN a release with one breaking record and 3 active repos, THEN exactly 3 run jobs are enqueued.
- [x] GIVEN a repo with `openai==1.30.0` in `requirements.txt` and latest release 1.45.0, THEN the range is (1.30.0, 1.45.0].
- [x] GIVEN `pyproject.toml` with `openai>=1.0` and a `uv.lock` at 1.30.0, THEN the old version comes from the lock file.
- [x] GIVEN a repo that does not list the package, THEN the run ends `package-not-used` without cloning. *And without any request to GitHub.*
- [x] GIVEN a range covering 5 releases and an empty cache, THEN 5 notes are fetched; a second run fetches none. *The second run makes one conditional request for the release list, answered 304.*
- [x] GIVEN a release body edited upstream with the same extracted records, THEN revision 2 is stored and no new runs start.
- [x] GIVEN a 403 or 429 rate-limit response, THEN the client backs off and retries.
- [x] GIVEN a release whose body is only "See the changelog for the full release notes", THEN the stored document is that version's `CHANGELOG.md` section, and it is the text extraction receives.
- [x] **Regression against real data:** for `stripe` 16.0.0 the stored document contains the `stripe.Reversal` → `stripe.TransferReversal` rename (present only in `CHANGELOG.md`). *Tested on a stored excerpt of the real changelog, and checked live on 2026-11-14.*
- [x] GIVEN a version with an empty body and no changelog section, THEN it is recorded as a gap.

## Technical notes
- Version comparison uses `packaging.version`; pre-releases are ignored unless the repo
  already pins one.
- Map release tags to versions per provider (`v1.45.0` → `1.45.0`).
- The gate is the main cost control. `openai-python` releases several times a week and
  most releases break nothing; without the gate every release would clone every repo.
- A run triggered by release N covers every change in (pinned, N], so a repo that
  ignored an earlier PR still gets a complete one.
- Push-triggered re-scans are out of scope: a repo is re-checked when the next
  gated release arrives, or on reinstall.
- Changelog sections are found by a heading that contains the version, for example
  Stripe's `## <a id="16-0-0"></a>16.0.0 - 2026-09-30`. The section ends at the next
  heading of the same level that carries a version number.
- Detecting a pointer body: short (under about 300 characters), contains a link, and
  mentions "changelog" or "release notes". Record which rule fired, so a missed case
  shows up in the run log.
- Why this is required (spike, 2026-11-13): from v16 on, every Stripe GitHub release
  body is a one-line link to `CHANGELOG.md`. Watching GitHub Releases alone would see
  no changes in Stripe's two latest majors.

## Test plan
- Unit: dependency-file parsers for all four formats (F1.10, F1.11); end reasons
  (F1.12); tag mapping; gate decision (F1.7).
- Integration with a stubbed GitHub API: poll, 304, revision on edit, fan-out count,
  catch-up, changelog fallback, gap, rate limit (F1.2 to F1.9, F1.13, F1.14, F1.17).
- Unit: changelog section extraction and pointer-body detection, with Stripe's real
  heading format as a fixture (F1.14).

## Out of scope
- Understanding the text (spec 02) and the API diff itself (spec 03).
- API-level sources (spec 10), which reuse F1.2 to F1.9.
- Reacting to Dependabot or Renovate PRs; dropped as a trigger in v3 (stretch goal).

## Open questions / decisions
- 2026-11-14 (implementation): each source has a **release index** cached in the blob
  store (`sdk/<package>/index.json` plus the raw bodies). Page 1 is refreshed with an
  ETag; the rest of the history is fetched once, the first time a run needs a range.
  The full history is needed because GitHub lists releases by date, not version, and
  Stripe ships backports to old majors, so "every version above X" cannot be read off
  the newest page.
- 2026-11-14 (implementation): the **first poll of a source is a baseline**. It records
  what exists and announces nothing, so installing the watcher does not start runs for
  a project's whole history. Index entries carry a `handled` flag; a release fetched
  but not yet stored when a crash happens is picked up by the next poll.
- 2026-11-14 (implementation): new end reason `version-conflict`, when dependency files
  pin different exact versions of the package. Also `range-too-large` (F1.15),
  `invalid-version` and `no-releases`.
- 2026-11-14 (implementation): a document's origin (`release` or `changelog`) is carried
  in its blob reference (`…/r1.changelog.md`), not a new column.
- 2026-11-14 (implementation): an upstream edit is noticed only for the newest 100
  releases (page 1), and becomes a new revision only for notes already stored.
- 2026-11-14 (implementation): a gap is reported by the poll that finds it, once. Runs
  recompute gaps for their range each time.
- 2026-11-14 (implementation): the gate is a hook, `poll(ctx, assessor=…)`. Until specs
  02 and 03 supply the assessor, a poll that finds new notes reports `gate:
  unavailable` and starts no runs.
- 2026-11-13 (spike): the `CHANGELOG.md` fallback is required (F1.14), not optional;
  gaps renumbered to F1.17. See `docs/spikes/2026-11-13-pre-phase-spike.md`.
- Poll cadence: daily meets the 24-hour goal; hourly is cheap with ETags. Decide in
  Phase 6 when Cloud Scheduler is set up.
- 2026-11-13 (v3): the trigger is now the provider's release, not a bump PR. The range
  comes from the repo's pinned version, not from a PR diff. Polling returns from v1,
  with the release gate added.
