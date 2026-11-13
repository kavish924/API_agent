# Spec 00 — Foundation
Status: Draft · Phase: 0 · PRD v2 ref: §8 · Depends on: —

## Goal
Stand up the project skeleton so every later spec builds on stable ground: package
layout, configuration, database schema, the interfaces that let the same code run
locally and on GCP, the CLI entry point, and CI.

## User stories
- As a developer, I can clone the repo and set up a working local environment with one
  command, with no database server and no cloud account.
- As a developer, I get lint, type and test feedback on every push so that regressions
  are caught early.

## Requirements
| ID | Requirement |
|---|---|
| F0.1 | Package layout: `guardian/` with `discovery/`, `apidiff/`, `extract/`, `scan/`, `fix/`, `verify/`, `github_app/`, `pipeline/`, `common/` (db, config, logging, llm client, queue, blob store) and `cli.py`; plus `evals/`, `tests/`, `deploy/` |
| F0.2 | Config module loads from environment variables (`.env` locally); secrets never hardcoded; a `.env.example` is committed |
| F0.3 | Migrations (Alembic) create the tables in "Data model" below |
| F0.4 | One SQLAlchemy layer: SQLite for local development and tests, Postgres when deployed; switching `DATABASE_URL` needs no code change |
| F0.5 | `pipeline.run(RunRequest) -> RunResult` is a plain function. A `JobQueue` interface invokes it; the local implementation is in-process, the Pub/Sub implementation is spec 09 |
| F0.6 | Shared LLM client wrapper that logs every call to `llm_calls` (inputs, outputs, model, tokens, latency, cost). All later specs use it, never a raw SDK call. Provider sits behind an interface: Vertex AI implementation plus a stub for tests |
| F0.7 | `BlobStore` interface for raw release notes, diff JSON and run output: local-directory implementation here, Cloud Storage implementation in spec 09 |
| F0.8 | Structured JSON logging to stdout with a run ID on every line |
| F0.9 | Retry-with-backoff helper for HTTP calls (GitHub, PyPI) that honours `Retry-After` and GitHub secondary rate limits |
| F0.10 | CLI skeleton (`guardian`, Typer) with the `run` command wired to `pipeline.run`; stages may be stubs at this point |
| F0.11 | CI on every push: ruff, mypy, pytest |

## Data model
| Table | Key | Main fields |
|---|---|---|
| `release_notes` | (package, version, revision) | provider, content hash, raw-text blob reference, ETag, fetched-at |
| `change_records` | id; belongs to one release-note revision | change type, qualified symbol, affected parameter, replacement, fixable, evidence quote, migration hint, source (`notes` or `diff`) |
| `runs` | (repo, package, to_version, head_sha) | from_version, bump PR number, status, outcome, verification level, timestamps |
| `findings` | id; belongs to a run and a change record | file, line, matched symbol, match tier, confidence, fix status |
| `pull_requests` | (repo, package, to_version) | bump PR number, fix PR number, branch, state, closed-remembered flag, comment ID |
| `llm_calls` | id; optional run | purpose, model, input, output, tokens in and out, latency, cost |

Confidence is per run, not per change record, because it depends on the run's API
diff. It is stored on `findings` and in the run output.

## Acceptance criteria
- [ ] One documented command (for example `make dev`) creates the environment and a SQLite database, and `pytest` passes.
- [ ] Running migrations on a fresh database creates all six tables; a second run is a no-op.
- [ ] The same test suite passes with `DATABASE_URL` pointing at SQLite and at Postgres.
- [ ] A smoke test calls the shared LLM client against the stub and asserts one `llm_calls` row with tokens and latency.
- [ ] `guardian run --help` lists `--repo`, `--package`, `--from`, `--to`.
- [ ] `ruff` and `mypy` pass with zero errors in CI.

## Technical notes
- No Redis, RQ or Celery, and no docker compose for services. Docker is needed only
  for the sandbox (spec 06).
- `release_notes` is a sixth table that PRD v2 folds into `change_records` "with
  revisions". It is split out because one note yields several change records.
- Keep every stage a function of explicit inputs so specs 01–06 are testable without
  GitHub or GCP.

## Test plan
- Integration: migration idempotency, config loading, LLM stub logging, blob store
  round trip (F0.2, F0.3, F0.6, F0.7).
- CI green = F0.11.

## Out of scope
- Any business logic; later specs.
- GitHub App registration (spec 07) and cloud resources (spec 09).

## Open questions / decisions
- 2026-11-13: Postgres, Redis and RQ from the v1 spec are dropped. SQLite locally,
  Cloud SQL when deployed.
- 2026-11-13: `release_notes` added as a separate table (see technical notes).
