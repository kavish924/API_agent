# API Change Guardian — Tech Stack

Chosen for a 6-week build, per PRD v2. Every choice maps to a requirement. The
application is Python-only; hosting is Google Cloud.

---

## 1. Language and runtime

| Choice | Version | Why |
|---|---|---|
| **Python** | 3.12+ | The only target language in this version; best ecosystem for the analysis tools (libcst, griffe) |

---

## 2. Core libraries

| Library | Used for | PRD v2 ref |
|---|---|---|
| **Pydantic** | Change-record schema; validating LLM output; run request and result models | FR3 |
| **griffe** | Static public-API surface of the old and new SDK version, and the diff between them | FR2 |
| **libcst** (with `QualifiedNameProvider`, `ScopeProvider`) | Scanning for call sites **and** rewriting them with codemods; one tool, so findings point at nodes the fixer can change | FR4, FR5 |
| **packaging**, **tomllib** | Reading the old and new version from dependency files | FR1 |
| **FastAPI** + Uvicorn | Webhook service | FR7 |
| **SQLAlchemy** + Alembic | One database layer for SQLite (local) and Postgres (cloud); migrations | §8 |
| **httpx** | GitHub and PyPI calls with retry and backoff | FR2, FR7 |
| **PyJWT** | GitHub App authentication (JWT → installation token) | FR7 |
| **Typer** | `guardian` CLI | FR1 |

Not used: tree-sitter (libcst covers scanning), Redis, RQ and Celery (see section 4).

---

## 3. LLM layer

| Concern | Choice | Notes |
|---|---|---|
| Access | **Vertex AI**, authenticated by service account | No LLM API key to store; billing stays in the GCP project |
| Model | Open: Claude or Gemini on Vertex AI | A small model for extraction, a stronger one for fixes; decided from eval results and cost |
| Extraction | One call per release note, cached per (package, version, revision) | Never repeated per repo |
| Fixing | Only when no codemod applies; capped per run | Output must pass the AST guard |
| Safety | No tools on any call; schema-validated output; all inputs treated as untrusted data | Release notes, repo code, test output |
| Logging | Every call logged to `llm_calls` (input, output, model, tokens, latency, cost) | Through one shared client wrapper |
| Tests | Stub implementation behind the same interface | Unit and integration tests make no model calls |

---

## 4. Storage and queueing

| Concern | Local | On GCP | Why |
|---|---|---|---|
| Database | **SQLite** | **Cloud SQL for Postgres** | Same schema through SQLAlchemy; no server needed locally |
| Job queue | In-process | **Pub/Sub** (pull subscription, dead-letter topic) | One message per run; redelivery is safe because runs are idempotent |
| Blob storage | Local directory | **Cloud Storage** | Raw release notes, API-diff JSON, run output, eval results |

---

## 5. Sandboxing

| Choice | Role | Constraints |
|---|---|---|
| **Docker** | Runs untrusted repo code to verify a patch | Phase 1 install: PyPI-only network through an allow-listing proxy, no secrets. Phase 2 run: no network. Non-root, CPU, memory and time limits |
| `compileall`, **Ruff**, **Pyright**, **pytest** | Checks inside the sandbox | Give verification levels L1 (compile and lint), L2 (types against the new SDK), L3 (tests) |

---

## 6. GitHub integration

| Choice | Role |
|---|---|
| **GitHub App** (not a PAT) | Installation tokens scoped to installed repos |
| **GitHub REST API** | Dependency-file diffs, release notes, branches, draft PRs, PR comments |
| **Webhooks** | `pull_request` opened and synchronize from Dependabot or Renovate; HMAC signature checked on every request |

**Permissions:**

| Permission | Level |
|---|---|
| Contents | read and write |
| Pull requests | read and write |
| Metadata | read |

No Issues permission (the app comments on the bump PR instead of opening issues) and
no Workflows permission (so it cannot change CI workflow files).

---

## 7. Google Cloud services

| Service | Used for |
|---|---|
| **Cloud Run** | Webhook service; scales to zero; max instances capped |
| **Pub/Sub** | Job queue with a dead-letter topic |
| **Compute Engine** | One small VM with Docker: the worker and its sandbox |
| **Cloud SQL for Postgres** | All tables |
| **Cloud Storage** | Blob store |
| **Secret Manager** | GitHub App private key and webhook secret |
| **Vertex AI** | LLM calls |
| **Artifact Registry** + Cloud Build | Container images and deploys |
| **Cloud Logging** and **Monitoring** | Logs, dead-letter alert, budget alert |

The worker is a VM, not Cloud Run, because the sandbox must start containers with the
network switched off.

---

## 8. Supporting tooling

| Concern | Choice |
|---|---|
| Project lint and types | Ruff + mypy |
| Testing | pytest |
| Dependency management | uv |
| CI | GitHub Actions: lint, types, tests on every push |
| Config and secrets | Environment variables (`.env` locally, Secret Manager on GCP); no secret ever enters the sandbox |
| Deployment | `gcloud` scripts in `deploy/` |
| Diagrams | Mermaid (see [`architecture.md`](./architecture.md)) |

---

## 9. Stack ↔ requirement traceability

| Requirement (PRD v2) | Stack pieces |
|---|---|
| FR1 Trigger and version range | Webhooks, FastAPI, packaging, tomllib, Typer |
| FR2 Change discovery | GitHub REST API, httpx, griffe, blob store |
| FR3 Extraction | Vertex AI (structured output), Pydantic |
| FR4 Repo scanning | libcst metadata providers |
| FR5 Fix generation | libcst codemods, LLM diff, AST guard |
| FR6 Verification | Docker, Ruff, Pyright, pytest |
| FR7 GitHub integration | GitHub App, installation tokens, REST API |
| Observability | `llm_calls` table, structured logs, Cloud Logging |
| Hosting | Cloud Run, Pub/Sub, Compute Engine, Cloud SQL, Secret Manager |
