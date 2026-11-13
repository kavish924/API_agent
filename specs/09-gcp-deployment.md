# Spec 09 — Google Cloud deployment
Status: Draft · Phase: 5 · PRD v2 ref: §7 (threat model), §8 (architecture) · Depends on: 00-foundation, 06-verification, 07-github-integration

## Goal
Run the pipeline hosted on GCP with the same code that runs locally: a serverless
webhook service, a queue, and one worker VM that also hosts the Docker sandbox.
Deployment is scripted and the cloud-specific threats are closed.

## User stories
- As the developer, I can deploy or tear down the whole system with one script so that
  the demo is repeatable.
- As the developer, I am alerted before the project costs more than I planned.
- As a repo owner, I want the service's cloud credentials out of reach of the code it
  runs from my repo.

## Requirements
| ID | Requirement |
|---|---|
| F9.1 | **Webhook service on Cloud Run:** the FastAPI app from spec 07. It verifies the signature, publishes one message per run request, and returns. It has no database access. Scales to zero; max instances capped |
| F9.2 | **Pub/Sub:** one topic and one pull subscription for run requests, with a dead-letter topic after a fixed number of delivery attempts |
| F9.3 | **Worker on a Compute Engine VM** with Docker: pulls from the subscription, runs `pipeline.run`, and acknowledges only when the run has finished or ended with a recorded outcome |
| F9.4 | The worker extends the message lease while a run is in progress, so a run of up to 15 minutes is not redelivered mid-flight |
| F9.5 | Redelivery is safe: the run key (repo, package, to_version, head_sha) makes a repeated message a no-op |
| F9.6 | **Cloud SQL for Postgres** holds all tables. The worker connects through the Cloud SQL Auth Proxy or a private IP |
| F9.7 | **Cloud Storage** implements the `BlobStore` interface (F0.7) |
| F9.8 | **Secret Manager** holds the GitHub App private key and the webhook secret. Nothing secret is in an image, a `.env` file on the VM, or the repo |
| F9.9 | **Vertex AI** implements the LLM client (F0.6), authenticated by the worker's service account. There is no LLM API key |
| F9.10 | Two service accounts with only the roles they need: the webhook service (Pub/Sub publisher, read the webhook secret) and the worker (Pub/Sub subscriber, Cloud SQL client, storage object access, Vertex AI user, read the App key) |
| F9.11 | **Metadata server block:** a host firewall rule stops traffic from the Docker sandbox networks to 169.254.169.254. This is in addition to the sandbox's own network setup (F6.11) |
| F9.12 | The VM has no public inbound ports for the app. Admin access is through IAP or OS Login |
| F9.13 | Images are built to Artifact Registry. `deploy/` holds scripts that create every resource and deploy both services, and a teardown script |
| F9.14 | Logs go to Cloud Logging as structured JSON with the run ID. An alert fires on dead-lettered messages |
| F9.15 | A budget alert is set on the project |
| F9.16 | The worker runs as a systemd service that restarts on failure and starts on boot, so the VM can be stopped and started between demos |

## Acceptance criteria
- [ ] GIVEN an empty GCP project, WHEN the deploy script runs, THEN all resources exist and a health check on the Cloud Run URL passes.
- [ ] GIVEN a Dependabot bump PR on a test repo with the App installed, THEN a draft fix PR is opened through the deployed system.
- [ ] GIVEN the same Pub/Sub message delivered twice, THEN one run and one PR.
- [ ] GIVEN a run that takes 8 minutes, THEN the message is not redelivered while it runs.
- [ ] GIVEN a message that fails every attempt, THEN it lands in the dead-letter topic and the alert fires.
- [ ] GIVEN a sandbox container in the install phase on the VM, THEN a request to 169.254.169.254 fails (canary run on the deployed VM).
- [ ] GIVEN the Cloud Run service account, THEN it cannot read the App private key or reach Cloud SQL.
- [ ] GIVEN the VM image and the container images, THEN a scan finds no secret values in them.
- [ ] GIVEN the VM is stopped and started, THEN the worker resumes and processes a queued message.
- [ ] A budget alert is visible in the project's billing settings.

## Technical notes
- **Why a VM:** the sandbox must start containers with the network off. Cloud Run
  cannot start containers from inside a container, so the worker and sandbox share a
  VM. A small general-purpose machine type is enough for one run at a time.
- One run at a time on the VM (subscriber flow control set to 1) keeps sandbox
  resource limits simple. Throughput is not a goal for this version.
- Plain `gcloud` scripts are enough for a project of this size; Terraform is optional.
- Local development is unchanged: SQLite, local blob directory, in-process queue,
  local Docker. Phases 0 to 4 need no GCP resources.
- Cost control: Cloud Run scales to zero; stop the VM outside demos; smallest Cloud
  SQL instance.

## Test plan
- Scripted smoke test after deploy: health check, publish a synthetic run request,
  assert the run row and outcome.
- Security checks on the deployed VM: metadata canary (F9.11), IAM checks for both
  service accounts (F9.10), image secret scan (F9.8).
- Failure tests: duplicate delivery (F9.5), poison message to dead letter (F9.2),
  VM restart (F9.16).

## Out of scope
- Multi-region, autoscaling workers, or high availability.
- Multi-tenant concerns, billing, a dashboard.
- CI/CD beyond a manual deploy script.

## Open questions / decisions
- Cloud SQL for Postgres or Firestore? Cloud SQL is the default because local
  development uses the same SQL schema on SQLite; Firestore would cost less.
- Fallback if Phase 5 slips: SQLite on the worker VM's disk in place of Cloud SQL
  (PRD v2 §10). F9.1 already keeps the webhook service off the database, so this
  swap touches only the worker.
- Which region, chosen for Vertex AI model availability.
- 2026-11-13: new spec; settles the v1 open question "hosted or local-only".
