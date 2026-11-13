# Spec 06 — Sandbox verification
Status: Draft · Phase: 3 · PRD v3 ref: FR6 · Depends on: 05-fix-generation

## Goal
Test a patch against the **new** SDK version inside a locked-down Docker sandbox and
report honestly how far it was verified: L1, L2 or L3. A patch that cannot reach L1
never becomes a PR.

## User stories
- As a reviewer, I want to know whether a fix was only linted, type-checked, or
  actually tested, so that I know how carefully to review it.
- As a maintainer, I want untrusted repo code run in isolation so that my
  infrastructure and credentials are never exposed.

## Requirements
| ID | Requirement |
|---|---|
| F6.1 | **Phase 1, install:** in a container with no secrets and network access to PyPI only, install the repo's dependencies. Two environments are built: `base` (the default branch as it is, with its pinned SDK version) and `fixed` (bumped dependency files, new SDK version, patched code) |
| F6.2 | **Phase 2, run:** all checks run with the network off |
| F6.3 | **L1:** every changed file compiles, and the linter reports no new errors compared with `base` |
| F6.4 | **L2:** the type checker reports no errors in `fixed` that are absent from `base`. Errors are compared by file, rule and message, ignoring line numbers |
| F6.5 | **L3:** every test that passed in `base` passes in `fixed` |
| F6.6 | A test that fails in `base` is reported as **could not run**, not as a failure. A repo with no tests gets `tests: none`. L3 is reached only if at least one test passed in `base` |
| F6.7 | The reported level is the highest level reached with all lower levels passing |
| F6.8 | On a failed level, retry fix generation with the error output, up to 2 retries (3 attempts). The best attempt is kept |
| F6.9 | If no attempt reaches L1, the outcome is `unverified` and no PR is opened (spec 07 opens an issue) |
| F6.10 | Containers run as non-root with CPU, memory and wall-clock limits. A timeout is a failure of that level, and the container is removed |
| F6.11 | Sandbox containers cannot reach the cloud metadata server (169.254.169.254) in either phase |
| F6.12 | Each attempt's output is stored, and a summary (level, counts, could-not-run list) is returned for the PR description |
| F6.13 | **Type-check service for the agent:** apply a candidate diff to a copy of the `fixed` environment, run the type checker offline, and return only the errors that are new compared with `base`, truncated to a size cap. Used by the `run_typecheck` tool (spec 11 F11.8) |
| F6.14 | **Lock regeneration:** when the repo has a lock file, the repo's own lock tool runs inside the install phase after the bump editor has changed the manifest. The regenerated lock is returned as part of the patch. Failure gives outcome `could-not-lock` |
| F6.15 | For API-level runs (spec 10) there is no bump, so `base` and `fixed` share one environment and differ only in the patch |
| F6.16 | In a `partial` patch, errors located at the remaining report-only call sites are listed separately and do not by themselves fail L2 |

## Acceptance criteria
- [ ] GIVEN a correct patch for a repo with passing tests, THEN the result is L3.
- [ ] GIVEN a correct patch for a repo whose tests all need an API key, THEN those tests are "could not run" and the result is L2.
- [ ] GIVEN a repo with no type-checker errors in `base` and a patch that leaves one call unfixed, THEN L2 fails and the retry receives the error.
- [ ] GIVEN a patch with a syntax error on all 3 attempts, THEN the outcome is `unverified`.
- [ ] GIVEN a repo that already fails lint in `base`, THEN the same errors in `fixed` do not fail L1.
- [ ] GIVEN the run phase, THEN an outbound request from inside the container fails (canary).
- [ ] GIVEN the install phase, THEN a request to PyPI succeeds and requests to another host and to 169.254.169.254 fail (canary).
- [ ] GIVEN any phase, THEN no secret environment variable or credential file is present in the container.
- [ ] GIVEN a hanging test, THEN the timeout ends the attempt and the container is removed.
- [ ] GIVEN a candidate diff sent to the type-check service, THEN the working checkout is unchanged and only new errors come back.
- [ ] GIVEN a repo with `uv.lock`, THEN the patch contains the regenerated lock at the new version; GIVEN the lock tool fails, THEN the outcome is `could-not-lock`.
- [ ] GIVEN a partial patch with one unfixed call site, THEN the type error at that site is reported as remaining work, not as an L2 failure of the fix.

## Technical notes
- PyPI-only network: put install containers on an internal Docker network whose only
  route out is an allow-listing proxy (`pypi.org`, `files.pythonhosted.org`). The same
  setup blocks the metadata server; spec 09 adds a host firewall rule as a second layer.
- Tools inside the sandbox: `python -m compileall`, Ruff, Pyright, pytest. Ruff uses
  the repo's own config when present, otherwise only syntax and undefined-name rules.
- L2 is the strongest general signal: a removed symbol or parameter shows up as a
  type error against the new SDK even when no test covers the call.
- `base` is built once per run and reused across attempts.
- Installing dependencies runs untrusted build code. That is why phase 1 has no
  secrets and a restricted network, not just phase 2.

## Test plan
- Integration against fixture repos: clean patch, broken patch, key-dependent tests,
  no tests, lint-dirty baseline, hanging test (F6.3 to F6.10).
- Security canaries: network and metadata checks in both phases, environment audit
  (F6.1, F6.2, F6.11).
- Feeds spec 08 fix eval (≥ 60% reach L2 or higher; false-confidence < 10%).

## Out of scope
- Generating the patch (spec 05).
- Creating the PR or issue (spec 07).
- Running the repo's own CI configuration.

## Open questions / decisions
- How to detect the repo's install method (requirements file, Poetry, uv) and what to
  do when detection fails. Proposed: outcome `could-not-install`, which is treated as
  no level reached beyond a compile check on the host image.
- Pyright for all repos, or mypy when the repo is configured for it? Decide in Phase 3.
- 2026-11-13 (v3): `base` is now the default branch with its pinned version; the
  type-check service for the agent, lock regeneration, and the partial-patch rule are
  added.
- 2026-11-13: v1's single offline run and pass/fail result are replaced by two phases
  and levels.
