# Spec 01 — Change discovery
Status: Draft · Phase: 1 · PRD v2 ref: FR1 (version range), FR2 (release notes) · Depends on: 00-foundation

## Goal
Turn "this branch bumps a watched SDK" into two exact facts: the old and new version,
and the stored release notes for every version in between. Every later stage works
from these.

## User stories
- As a developer, I want the version range read from what actually changed in my
  dependency files so that the fix targets the version I am moving to.
- As a maintainer, I want each release note fetched once and reused across repos so
  that cost and rate-limit use stay low.

## Requirements
| ID | Requirement |
|---|---|
| F1.1 | Watched packages are configured as package → GitHub repo. v2 ships `openai` → `openai/openai-python` and `stripe` → `stripe/stripe-python` |
| F1.2 | Read the old and new version of a watched package by comparing dependency files between a base and a head revision: `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock`. Never parse the PR title |
| F1.3 | When both a manifest and a lock file exist, the lock file wins. A manifest with only a range (`>=1.0`) and no lock file ends the run with reason `version-not-pinned` |
| F1.4 | A run proceeds only for an upgrade (new > old). No change, a downgrade, or an added or removed package ends the run with a recorded reason |
| F1.5 | List the released versions in (old, new] and fetch the GitHub Release body for each, on demand |
| F1.6 | Cache by (provider, package, version). A cached note is revalidated with a conditional request (ETag); a 304 costs nothing |
| F1.7 | If a note's body changed, store a new revision under the same key. Never create a second key for the same version |
| F1.8 | Store the raw text byte-for-byte in the blob store before any processing |
| F1.9 | A version with no release or an empty body is recorded as a gap; the run continues |
| F1.10 | The same logic backs the CLI: `--from` and `--to` skip F1.2 to F1.4 |

## Acceptance criteria
- [ ] GIVEN a diff that changes `openai==1.30.0` to `openai==1.45.0` in `requirements.txt`, WHEN discovery runs, THEN the range is (1.30.0, 1.45.0].
- [ ] GIVEN `pyproject.toml` with `openai>=1.0` and a `uv.lock` moving 1.30.0 to 1.45.0, THEN the range comes from the lock file.
- [ ] GIVEN a range-only manifest and no lock file, THEN the run ends with `version-not-pinned` and nothing else happens.
- [ ] GIVEN a downgrade, THEN the run ends with `not-an-upgrade`.
- [ ] GIVEN a range covering 5 releases and an empty cache, THEN 5 notes are fetched and stored; a second run fetches none (cache or 304).
- [ ] GIVEN a release body edited upstream, WHEN revalidated, THEN revision 2 exists under the same (package, version) and revision 1 is kept.
- [ ] GIVEN a 403 or 429 rate-limit response, THEN the client backs off and retries.
- [ ] Stored raw text is byte-identical to the fetched body.

## Technical notes
- Version comparison uses `packaging.version`; pre-releases are excluded unless the
  target itself is one.
- Map release tags to versions per provider (`v1.45.0` → `1.45.0`).
- Output shape: `VersionRange(package, old, new)` plus an ordered list of release-note
  references and a list of gaps.
- A configurable cap on range size (default 200 releases) ends the run with
  `range-too-large` instead of spending without limit.

## Test plan
- Unit: dependency-file parsers for all four formats (F1.2, F1.3); upgrade check
  (F1.4); tag mapping.
- Integration with a stubbed GitHub API: fetch, cache hit, 304, revision on edit,
  gap, rate limit (F1.5 to F1.9).

## Out of scope
- Understanding the text (spec 02).
- Detecting that a PR is a bump PR (spec 07 decides which PRs to look at).
- Background or scheduled polling of releases; dropped in PRD v2.

## Open questions / decisions
- Should `CHANGELOG.md` be a fallback when a release body is empty? Decide in Phase 1
  after checking how often gaps occur for the two providers.
- 2026-11-13: replaces `01-ingestion.md`. Daily polling and the content-hash dedup key
  are removed.
