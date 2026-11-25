# Running the GitHub App locally

This is the live end-to-end test from spec 07: a real GitHub App, installed on a test
repository, receiving webhooks on your machine through a tunnel. Everything else in
spec 07 is tested against an in-memory GitHub (`tests/test_github_app.py`).

## 1. Register the App

1. Pick a public HTTPS URL that forwards to `http://127.0.0.1:8000`. Any tunnel works,
   for example `cloudflared tunnel --url http://127.0.0.1:8000` or a smee.io channel.
2. Copy `deploy/github-app-manifest.json` and replace the `hook_attributes.url`
   placeholder with `<your tunnel URL>/webhook`.
3. On GitHub: **Settings → Developer settings → GitHub Apps → New GitHub App**, and fill
   it in from the manifest. The permissions must be exactly:
   - Contents: read and write
   - Pull requests: read and write
   - Issues: read and write
   - Metadata: read-only

   **No Workflows permission.** Without it GitHub refuses any push that touches a
   workflow file, a second guard behind the AST guard.
4. Set a webhook secret. Generate a private key and download the `.pem` file.

## 2. Configure the worker

In `.env` (never commit it):

```dotenv
GUARDIAN_GITHUB_APP_ID=<the App ID>
GUARDIAN_GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
GUARDIAN_GITHUB_WEBHOOK_SECRET=<the webhook secret>
```

Then start the webhook and worker in one process:

```bash
python -m guardian.cli serve --port 8000
```

`serve` checks every signature, queues installation events, and runs the jobs in a
background thread, one at a time.

## 3. Install it and watch a run

1. Install the App on a test repository that pins an old `openai` or `stripe` version
   and calls something a later release changed.
2. The `installation` event registers the repo and queues one catch-up run per SDK and
   per API source.
3. Expected result: a **draft** PR from `guardian/upgrade-<package>` with the bump and
   the fix in one commit, or one issue if nothing could be fixed or verified. Nothing is
   ever pushed to the default branch.

Optional per-repo settings live in `.github/guardian.yml` on the default branch:

```yaml
mode: pr            # pr | issue (issue: never push code; the diff goes in the issue)
packages: [openai]  # default: every watched SDK
api_changes: pr     # pr | issue | off (model retirements)
```

## What the App does not do yet

- **Verification is static** (interim, spec 07 decision 2026-11-25): L1 compiles and
  lints, L2 type-checks against the new SDK. Tests are not run until the spec 06
  sandbox exists, and every PR says so.
- **No LLM provider is wired in yet.** Codemods work without one; release-note
  extraction and the Tier 2 fixers (single-shot, agent) need a provider and model
  (open checklist item).
- **Lock files are not regenerated** (spec 06 F6.14). The PR says to regenerate them.
- The global pause flag is `GUARDIAN_PAUSED=true`; with it set nothing is written.
