# Spec 01 — Change watching and discovery
Status: Draft · Phase: 1 · PRD v3 ref: FR1, FR2 (release notes) · Depends on: 00-foundation

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
| F1.14 | A version with no release or an empty body is recorded as a gap; the run continues |
| F1.15 | A cap on range size (default 200 releases) ends the run with `range-too-large` |
| F1.16 | CLI: `guardian poll`, and `guardian run --repo PATH --package openai [--from X --to Y]` where the flags override F1.10 and F1.11 |

## Acceptance criteria
- [ ] GIVEN a stubbed GitHub API with one new release, WHEN `poll` runs, THEN one source document is stored and extraction is requested once.
- [ ] GIVEN the same poll twice, THEN the second makes conditional requests, stores nothing and enqueues nothing.
- [ ] GIVEN a release whose records are all non-breaking (empty list), THEN no run job is enqueued.
- [ ] GIVEN a release with one breaking record and 3 active repos, THEN exactly 3 run jobs are enqueued.
- [ ] GIVEN a repo with `openai==1.30.0` in `requirements.txt` and latest release 1.45.0, THEN the range is (1.30.0, 1.45.0].
- [ ] GIVEN `pyproject.toml` with `openai>=1.0` and a `uv.lock` at 1.30.0, THEN the old version comes from the lock file.
- [ ] GIVEN a repo that does not list the package, THEN the run ends `package-not-used` without cloning.
- [ ] GIVEN a range covering 5 releases and an empty cache, THEN 5 notes are fetched; a second run fetches none.
- [ ] GIVEN a release body edited upstream with the same extracted records, THEN revision 2 is stored and no new runs start.
- [ ] GIVEN a 403 or 429 rate-limit response, THEN the client backs off and retries.

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

## Test plan
- Unit: dependency-file parsers for all four formats (F1.10, F1.11); end reasons
  (F1.12); tag mapping; gate decision (F1.7).
- Integration with a stubbed GitHub API: poll, 304, revision on edit, fan-out count,
  catch-up, gap, rate limit (F1.2 to F1.9, F1.13, F1.14).

## Out of scope
- Understanding the text (spec 02) and the API diff itself (spec 03).
- API-level sources (spec 10), which reuse F1.2 to F1.9.
- Reacting to Dependabot or Renovate PRs; dropped as a trigger in v3 (stretch goal).

## Open questions / decisions
- Should `CHANGELOG.md` be a fallback when a release body is empty? Decide after
  checking how often gaps occur for the two providers.
- Poll cadence: daily meets the 24-hour goal; hourly is cheap with ETags. Decide in
  Phase 6 when Cloud Scheduler is set up.
- 2026-11-13 (v3): the trigger is now the provider's release, not a bump PR. The range
  comes from the repo's pinned version, not from a PR diff. Polling returns from v1,
  with the release gate added.
