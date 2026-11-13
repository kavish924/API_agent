# Spec 07 — GitHub App integration
Status: Draft · Phase: 4 · PRD v2 ref: FR1 (trigger), FR7 · Depends on: 01-change-discovery, 06-verification

## Goal
Operate as a real GitHub App that reacts to Dependabot and Renovate bump PRs: start a
run, and deliver the result as one draft fix PR based on the bump branch, or one
comment, or nothing.

## User stories
- As a developer, when a bump PR needs code changes I get a draft PR stacked on it
  with the fixes.
- As a reviewer, I see each change with its release-note link, the affected files, the
  confidence, and the verification level reached.
- As a developer, if the app cannot verify a fix I get one comment on the bump PR
  listing the affected call sites.
- As a developer, I can close a fix PR and the app will not reopen it for the same
  target version.
- As a repo owner with sensitive CI secrets, I can switch to comment-only mode so the
  app never pushes code.

## Requirements
| ID | Requirement |
|---|---|
| F7.1 | Run as a GitHub App using installation tokens. Personal access tokens are never used |
| F7.2 | Permissions: Contents (read and write), Pull requests (read and write), Metadata (read). **No Issues permission and no Workflows permission** |
| F7.3 | Verify the webhook signature (HMAC SHA-256) on every request; reject an invalid one with 401 and process nothing |
| F7.4 | **Trigger:** `pull_request` events `opened` and `synchronize` whose author is `dependabot[bot]` or `renovate[bot]`. The webhook handler verifies, enqueues a run request, and returns at once |
| F7.5 | The worker asks spec 01 for the version range. If the PR does not upgrade a watched package, the run ends silently |
| F7.6 | **Fix PR:** create branch `guardian/fix-<package>-<to_version>` from the bump branch head, commit the patch, and open a **draft** PR whose base is the bump branch |
| F7.7 | The app never pushes to the bump branch or the default branch |
| F7.8 | PR description: a table with one row per change (release-note link, confidence, affected files, verification level); a list of report-only changes with call sites; the could-not-run summary; and the fix tier used |
| F7.9 | One fix PR per bump PR. When the bump PR moves to a new head or a newer version, the same fix PR is updated (branch reset to the new result), not duplicated |
| F7.10 | A closed, unmerged fix PR is remembered by (repo, package, to_version) and is never reopened or recreated for that target version |
| F7.11 | **Comment fallback:** when the outcome is `unverified`, or every finding is report-only, post one comment on the bump PR listing changes and call sites. A later run edits that comment instead of adding another |
| F7.12 | **Comment-only mode:** when enabled for the repo, the patch is posted as a diff in the comment and nothing is pushed |
| F7.13 | No findings → no PR and no comment |
| F7.14 | All GitHub API calls use the backoff helper (F0.9) |
| F7.15 | Delivery is idempotent: a redelivered webhook or a retried run produces no duplicate PR or comment |

## Acceptance criteria
- [ ] GIVEN a payload with an invalid signature, THEN the response is 401 and nothing is enqueued.
- [ ] GIVEN a `pull_request.opened` event from a human author, THEN nothing is enqueued.
- [ ] GIVEN a Dependabot PR that bumps an unwatched package, THEN the run ends with no output.
- [ ] GIVEN a verified patch, THEN a draft PR exists whose base is the bump branch and whose head is `guardian/fix-…`, and the bump branch itself has no new commit.
- [ ] GIVEN the bump PR receives a `synchronize` event to a newer version, THEN the existing fix PR is updated and no second PR exists.
- [ ] GIVEN the fix PR was closed for (repo, package, 1.45.0), WHEN the bump PR is rebased at the same version, THEN no PR is opened.
- [ ] GIVEN an `unverified` outcome on two consecutive runs, THEN the bump PR has exactly one guardian comment.
- [ ] GIVEN comment-only mode, THEN no branch is created and the comment contains the diff.
- [ ] GIVEN the same webhook delivered twice, THEN one PR.
- [ ] The permission list in the App manifest matches F7.2 exactly.

## Technical notes
- Repo settings live in `.github/guardian.yml`, read from the **default branch**, never
  from the PR head. Keys: `comment_only` (default false), `packages` (optional subset).
- The comment carries a hidden marker (`<!-- api-change-guardian -->`) so it can be
  found and edited. Commenting on a PR uses the Pull requests permission; no Issues
  permission is needed.
- Because the App has no Workflows permission, GitHub refuses any push that changes a
  workflow file. That is a second layer behind the AST guard (F5.5).
- Pushing the fix branch triggers the repo's own CI. The patch has passed the AST
  guard by then; comment-only mode is the answer for repos that cannot accept that.
- A foreign commit on a Dependabot branch stops Dependabot rebasing it. F7.7 avoids
  this by using a separate branch.
- The App manifest is committed to the repo.

## Test plan
- Unit: signature check (F7.3), event filter (F7.4), PR description rendering (F7.8),
  closed-PR memory (F7.10), comment marker handling (F7.11).
- Integration against a stubbed GitHub API: fix PR, update on synchronize, comment
  fallback, comment-only mode, duplicate delivery (F7.6 to F7.15).
- One live end-to-end test on a real test repo, run locally behind a webhook tunnel,
  recorded for the demo.

## Out of scope
- Auto-merge in any form.
- Opening the app's own bump PR for repos without Dependabot or Renovate (stretch goal).
- Hosting (spec 09).

## Open questions / decisions
- What happens to the fix PR when the bump PR is merged or closed first? Proposed:
  bump PR closed unmerged → close the fix PR with a note; bump PR merged → leave the
  fix PR and check how GitHub retargets its base. Verify the retargeting behaviour on
  a test repo in Phase 4.
- Should a "no affected call sites found" comment exist? Default is silence (F7.13);
  PRD v2 lists this as open.
- 2026-11-13: hosting question from v1 is settled: GCP, spec 09. Issues are replaced
  by comments on the bump PR.
