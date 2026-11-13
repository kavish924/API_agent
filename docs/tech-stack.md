# API Change Guardian — Tech Stack

Chosen for a 7-week build, per PRD v3. Every choice maps to a requirement. The
application is Python-only; hosting is Google Cloud.

---

## 1. Language and runtime

| Choice | Version | Why |
|---|---|---|
| **Python** | 3.12+ | The only target language in this version; best ecosystem for the analysis tools (libcst, griffe) |

---

## 2. Core libraries

| Library | Used for | PRD v3 ref |
|---|---|---|
| **Pydantic** | Change-record schemas; validating LLM output; tool argument models for the agent | FR2, FR3, FR5 |
| **griffe** | Static public-API surface of SDK versions; the diff between them; symbol lookup for the agent | FR2, FR5 |
| **libcst** (with `QualifiedNameProvider`, `ScopeProvider`) | Scanning for call sites **and** rewriting them with codemods; also the trees the AST guard compares | FR4, FR5 |
| **packaging**, **tomllib** | Reading pinned versions from dependency files; the bump editor | FR1, FR5 |
| **FastAPI** + Uvicorn | Webhook service | FR7 |
| **SQLAlchemy** + Alembic | One database layer for SQLite (local) and Postgres (cloud); migrations | §8 |
| **httpx** | GitHub, PyPI and provider-page calls with retry and backoff | FR1, FR3, FR7 |
| **PyJWT** | GitHub App authentication (JWT → installation token) | FR7 |
| **Typer** | `guardian` CLI: `poll`, `run`, `ingest` | FR1 |

Not used: tree-sitter (libcst covers scanning), Redis, RQ and Celery (see section 5),
and agent frameworks (see section 4).

---

## 3. LLM layer

| Concern | Choice | Notes |
|---|---|---|
| Access | **Vertex AI**, authenticated by service account | No LLM API key to store; billing stays in the GCP project |
| Model | Open: Claude or Gemini on Vertex AI | Must support structured output and tool use. A small model for extraction, a stronger one for the agent; decided from eval results and cost |
| Extraction | One call per release note or page entry, cached | **No tools**; schema-validated output; never repeated per repo |
| Fixing | Agent with tool use, only when no codemod applies | Budget per finding; every proposed patch passes the AST guard |
| Baseline | Single-shot fixer: one call, no tools | Kept so the evals can compare it with the agent |
| Safety | All inputs treated as untrusted data | Release notes, provider pages, repo code, tool and test output |
| Logging | Every call in `llm_calls`; every agent step in `agent_steps` | Through one shared client wrapper |
| Tests | Scriptable stub behind the same interface | Agent tests are deterministic and make no model calls |

---

## 4. Agent fixer

| Choice | Why |
|---|---|
| **A hand-written loop** on the provider's native tool use | The loop is small (pick a tool, run it, feed the result back) and writing it keeps budgets, logging and safety checks fully visible. No agent framework |
| **Seven fixed tools:** `read_file`, `search_repo`, `lookup_api`, `get_change`, `run_typecheck`, `propose_patch`, `cannot_fix` | Enough to investigate a call site and check a patch; nothing that can act on the world |
| **No shell, network, GitHub or write tools** | A prompt injection has nothing dangerous to call |
| **Path confinement and a secret-file deny list** | The agent cannot read outside the checkout or send committed secrets to the model |
| **Budgets:** steps, proposals, tokens, wall clock, per-run cost | Bounded cost and no runaway loops |
| **AST guard as the only exit** | Safety does not depend on the prompt |

---

## 5. Storage and queueing

| Concern | Local | On GCP | Why |
|---|---|---|---|
| Database | **SQLite** | **Cloud SQL for Postgres** | Same schema through SQLAlchemy; no server needed locally |
| Job queue | In-process | **Pub/Sub** (pull subscription, dead-letter topic) | Three job types: `poll`, `run`, `event`. Redelivery is safe because runs are idempotent |
| Scheduler | Run `guardian poll` by hand | **Cloud Scheduler** | Publishes the daily poll job |
| Blob storage | Local directory | **Cloud Storage** | Raw release notes, page snapshots, API-diff JSON, run output, eval results |

---

## 6. Sandboxing

| Choice | Role | Constraints |
|---|---|---|
| **Docker** | Runs untrusted repo code to verify a patch, and type-checks the agent's candidate patches | Phase 1 install: PyPI-only network through an allow-listing proxy, no secrets. Phase 2 run: no network. Non-root, CPU, memory and time limits |
| `compileall`, **Ruff**, **Pyright**, **pytest** | Checks inside the sandbox | Give verification levels L1 (compile and lint), L2 (types against the new SDK), L3 (tests) |
| The repo's own lock tool (`uv`, Poetry) | Regenerates the lock file after the version bump | Runs inside the install phase only |

---

## 7. GitHub integration

| Choice | Role |
|---|---|
| **GitHub App** (not a PAT) | Installation tokens scoped to installed repos |
| **GitHub REST API** | Release notes, dependency files, clones, branches, draft PRs, issues |
| **Webhooks** | Installation events only; HMAC signature checked on every request |

**Permissions:**

| Permission | Level |
|---|---|
| Contents | read and write |
| Pull requests | read and write |
| Issues | read and write |
| Metadata | read |

No Workflows permission, so the app cannot change CI workflow files.

---

## 8. Google Cloud services

| Service | Used for |
|---|---|
| **Cloud Scheduler** | Daily poll job |
| **Cloud Run** | Webhook service; scales to zero; max instances capped |
| **Pub/Sub** | Job queue with a dead-letter topic |
| **Compute Engine** | One small VM with Docker: the worker and its sandbox |
| **Cloud SQL for Postgres** | All tables |
| **Cloud Storage** | Blob store |
| **Secret Manager** | GitHub App private key and webhook secret |
| **Vertex AI** | Extraction and the agent |
| **Artifact Registry** + Cloud Build | Container images and deploys |
| **Cloud Logging** and **Monitoring** | Logs, dead-letter alert, budget alert |

The worker is a VM, not Cloud Run, because the sandbox must start containers with the
network switched off.

---

## 9. Supporting tooling

| Concern | Choice |
|---|---|
| Project lint and types | Ruff + mypy |
| Testing | pytest |
| Dependency management | uv |
| CI | GitHub Actions: lint, types, tests on every push |
| Config and secrets | Environment variables (`.env` locally, Secret Manager on GCP); no secret ever enters the sandbox or an agent tool result |
| Deployment | `gcloud` scripts in `deploy/` |
| Diagrams | Mermaid (see [`architecture.md`](./architecture.md)) |

---

## 10. Stack ↔ requirement traceability

| Requirement (PRD v3) | Stack pieces |
|---|---|
| FR1 Change watching and fan-out | Cloud Scheduler, Pub/Sub, httpx with ETags, packaging, tomllib, Typer |
| FR2 SDK change records | GitHub REST API, Vertex AI (structured output), Pydantic, griffe |
| FR3 API-level changes | httpx, blob-store snapshots, Vertex AI, libcst literal matching |
| FR4 Repo scanning | libcst metadata providers |
| FR5 Fix generation | Bump editor, libcst codemods, agent loop with tool use, AST guard |
| FR6 Verification | Docker, Ruff, Pyright, pytest |
| FR7 GitHub integration | GitHub App, installation tokens, REST API |
| Observability | `llm_calls` and `agent_steps` tables, structured logs, Cloud Logging |
| Hosting | Cloud Run, Pub/Sub, Compute Engine, Cloud SQL, Secret Manager |
