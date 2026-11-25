# Spec 07 — GitHub App integration
Status: Implemented · Phase: 5 · PRD v3 ref: FR7 · Depends on: 01-change-discovery, 06-verification, 11-agent-fixer

## Goal
Operate as a real GitHub App: know which repositories are installed, and deliver each
run's result as one draft upgrade PR (version bump and fix together), or one issue, or
nothing.

## User stories
- As a developer, I install the App and it checks my current SDK versions once.
- As a developer, when a new SDK release breaks calls in my code I get one draft PR
  with the bump and the fixes.
- As a reviewer, I see each change with a provider link and quote, the affected files,
  the confidence and the verification level reached.
- As a developer, if the app cannot verify a fix I get one issue listing the affected
  call sites.
- As a developer, I can close a PR and the app will not reopen it for the same changes.
- As a repo owner with sensitive CI secrets, I can switch to issue-only mode so the
  app never pushes code.

## Requirements
| ID | Requirement |
|---|---|
| F7.1 | Run as a GitHub App using installation tokens. Personal access tokens are never used |
| F7.2 | Permissions: Contents (read and write), Pull requests (read and write), Issues (read and write), Metadata (read). **No Workflows permission** |
| F7.3 | Verify the webhook signature (HMAC SHA-256) on every request; reject an invalid one with 401 and process nothing |
| F7.4 | Webhook events: `installation` and `installation_repositories`. The handler verifies, enqueues an `event` job and returns at once. The worker registers or deactivates rows in `repos` |
| F7.5 | A newly installed repo gets a catch-up run per watched package and API source (F1.9) |
| F7.6 | **Upgrade PR:** create or reset branch `guardian/upgrade-<package>` from the run's default-branch commit, add one commit with the bump and the fix, and open a **draft** PR against the default branch |
| F7.7 | The app never pushes to the default branch, and force-pushes only to its own `guardian/*` branches |
| F7.8 | PR description: a table with one row per change (provider link, evidence quote, confidence, affected files); the verification level; remaining call sites that need manual work; could-not-run tests; the fix tier used per finding |
| F7.9 | A `partial` patch (F5.13) opens a PR whose title says so and whose description lists what is left |
| F7.10 | One open PR per (repo, package). A later run updates the same PR: branch reset, title and description rewritten |
| F7.11 | **Closed-PR memory:** when a guardian PR was closed without merging, the change IDs it covered are stored as declined. A new PR opens only when the affected set contains a change that is not declined |
| F7.12 | **Issue fallback:** when nothing could be verified (outcome `unverified`, or every finding report-only), open one issue per (repo, package) listing changes and call sites. A later run edits that issue. A closed issue is remembered like a closed PR |
| F7.13 | **Issue-only mode:** when set for the repo, the patch is posted as a diff in the issue and nothing is pushed |
| F7.14 | If an open Dependabot or Renovate PR bumps the same package, the upgrade PR links to it |
| F7.15 | No findings → no PR and no issue |
| F7.16 | Repo config in `.github/guardian.yml`, read from the default branch: `mode` (`pr` or `issue`), `packages` (subset), `api_changes` (`pr`, `issue` or `off`) |
| F7.17 | The global pause flag (F0.12) is checked before any write to GitHub |
| F7.18 | All GitHub API calls use the backoff helper (F0.9) |
| F7.19 | Delivery is idempotent: a redelivered job produces no duplicate PR, commit or issue |
| F7.20 | Uninstalling deactivates the repo; no further runs target it |

## Acceptance criteria
- [x] GIVEN a payload with an invalid signature, THEN the response is 401 and nothing is enqueued.
- [x] GIVEN an `installation.created` event for 2 repos, THEN 2 active rows exist and catch-up runs are enqueued.
- [x] GIVEN a verified patch, THEN a draft PR exists from `guardian/upgrade-openai` to the default branch, with one commit that changes the dependency file and the code, and the default branch has no new commit.
- [x] GIVEN a later release for the same package while the PR is open, THEN the same PR number now shows the new version and there is no second PR.
- [x] GIVEN the PR was closed covering changes {A, B}, WHEN a new release still affects only {A, B}, THEN no PR is opened.
- [x] GIVEN the same closed PR, WHEN a new release adds affected change C, THEN a new PR is opened.
- [x] GIVEN an `unverified` outcome on two consecutive runs, THEN the repo has exactly one guardian issue.
- [x] GIVEN issue-only mode, THEN no branch is created and the issue contains the diff.
- [x] GIVEN a `partial` patch, THEN the PR title marks it and the description lists the remaining call sites.
- [x] GIVEN the pause flag is set, THEN no PR, commit or issue is written and the run records `paused`.
- [x] GIVEN the same run job delivered twice, THEN one PR with one commit.
- [x] GIVEN the App is uninstalled, THEN the repo is inactive and a later fan-out skips it.
- [x] The permission list in the App manifest matches F7.2 exactly.

## Technical notes
- PRs and issues carry a hidden marker (`<!-- api-change-guardian:<package> -->`) so
  they can be found and edited. State is confirmed through the API at delivery time;
  no `pull_request` webhook is needed.
- Because the App has no Workflows permission, GitHub refuses any push that changes a
  workflow file. That is a second layer behind the AST guard (F5.8).
- Pushing the branch triggers the repo's own CI. The patch has passed the AST guard by
  then; issue-only mode is the answer for repos that cannot accept that.
- The app opens its own bump only when call sites are affected. A plain bump with no
  affected code is left to Dependabot or Renovate, which avoids duplicate PRs in the
  common case.
- The App manifest is committed to the repo.

### As built (2026-11-25)
- Code: `guardian/github_app/` — `client.py` (App JWT, installation tokens, retry),
  `webhook.py` (FastAPI: signature check, enqueue, 202), `events.py` (installs,
  catch-up runs), `checkout.py` (tarball of the default-branch commit, safe
  extraction), `config.py` (`.github/guardian.yml`), `render.py` (titles and bodies with
  the hidden marker), `reports.py` (findings to report rows), `publish.py` (PR or issue,
  closed-item memory, Git Data API commit, pause gate). `guardian serve` runs the webhook
  and worker locally. Manifest: `deploy/github-app-manifest.json`. Setup:
  `docs/github-app-setup.md`.
- An installed repo is downloaded at its default-branch commit and runs through the same
  local pipeline; the run is stored under `owner/name` and the installation id.
- Closed-item memory: one `pull_requests` row per (repo, subject) for the PR and one
  per (repo, `<subject>:issue`) for the issue; `declined_change_ids` only grows. Change
  ids are stable hashes of the change, so the same change is recognised across runs.
- A redelivered job builds the same tree; if the branch already holds a commit with that
  tree on the same parent, no commit is written and the PR is only re-rendered.
- New files in a patch are written with mode `100644`.
- Tested against an in-memory GitHub that checks the App's JWT and refuses workflow
  files and non-forced ref moves. The live test on a real repository is still open.

## Test plan
- Unit: signature check (F7.3), PR description rendering (F7.8), declined-set logic
  (F7.11), marker handling (F7.12), config parsing (F7.16).
- Integration against a stubbed GitHub API: install and uninstall, upgrade PR, update
  on a later run, issue fallback, issue-only mode, pause, duplicate delivery.
- One live end-to-end test on a real test repo, run locally behind a webhook tunnel,
  recorded for the demo.

## Out of scope
- Auto-merge in any form.
- Stacking fixes onto an existing Dependabot or Renovate PR (stretch goal).
- API-level PR specifics (spec 10) and hosting (spec 09).

## Open questions / decisions
- Should a merged Dependabot PR for the same version close the guardian PR, or should
  the next run simply find the repo `up-to-date` and close it then? Proposed: the
  latter.
- Should the app also update its PR when the default branch moves and the PR
  conflicts? Proposed: not in v3; the next gated release refreshes it.
- 2026-11-25: until the spec 06 sandbox exists, delivery uses an **interim static
  verification**: L1 = every changed Python file compiles (no code runs) and Ruff finds
  no new errors compared with the unpatched files; L2 = the static mypy check from
  spec 11 finds no new errors. There is no L3. PRs say plainly that tests were not run.
  The sandbox replaces this in spec 06.
- 2026-11-25: spec approved. Built after spec 10 so both SDK and API-level results go
  through the same delivery.
- 2026-11-13 (v3): bump-PR trigger, stacked fix PR and comment fallback are replaced by
  installation events, a self-contained upgrade PR and an issue fallback. Issues
  permission added.
