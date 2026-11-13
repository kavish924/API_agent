# Spec 00 — Foundation
Status: Verified · Phase: 0 · PRD v3 ref: §8 · Depends on: —

> Verified 2026-11-13: CI green on GitHub (run 43411603849) for Python 3.11, Python 3.12
> and Postgres 16.

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
| F0.1 | Package layout: `guardian/` with `watch/`, `apidiff/`, `extract/`, `apilevel/`, `scan/`, `fix/`, `agent/`, `verify/`, `github_app/`, `pipeline/`, `common/` (db, config, logging, llm client, queue, blob store) and `cli.py`; plus `evals/`, `tests/`, `deploy/` |
| F0.2 | Config module loads from environment variables (`.env` locally); secrets never hardcoded; a `.env.example` is committed |
| F0.3 | Migrations (Alembic) create the tables in "Data model" below |
| F0.4 | One SQLAlchemy layer: SQLite for local development and tests, Postgres when deployed; switching `DATABASE_URL` needs no code change |
| F0.5 | `pipeline.poll()` and `pipeline.run(RunRequest) -> RunResult` are plain functions. A `JobQueue` interface carries three job types (`poll`, `run`, `event`); the local implementation is in-process, the Pub/Sub implementation is spec 09 |
| F0.6 | Shared LLM client wrapper supporting structured output and tool use. It logs every call to `llm_calls` (inputs, outputs, model, tokens, latency, cost). All later specs use it, never a raw SDK call. Vertex AI implementation plus a scriptable stub for tests |
| F0.7 | `BlobStore` interface for raw release notes, page snapshots, diff JSON and run output: local-directory implementation here, Cloud Storage implementation in spec 09 |
| F0.8 | Structured JSON logging to stdout with a run ID on every line |
| F0.9 | Retry-with-backoff helper for HTTP calls (GitHub, PyPI, provider pages) that honours `Retry-After` and GitHub secondary rate limits |
| F0.10 | CLI skeleton (`guardian`, Typer) with `poll`, `run` and `ingest` wired to the pipeline; stages may be stubs at this point |
| F0.11 | CI on every push: ruff, mypy, pytest |
| F0.12 | A global pause flag in config, checked before any write to GitHub |

## Data model
| Table | Key | Main fields |
|---|---|---|
| `repos` | (installation, full name) | active flag, default branch, installed-at |
| `sources` | source ID | kind (`sdk-release` or `api-page`), provider, package, URL, ETag, last seen version or hash |
| `source_documents` | (source, version or snapshot, revision) | content hash, raw-text blob reference, fetched-at |
| `change_records` | id; belongs to one source document, or to an API diff | layer (`sdk` or `api`), change type, qualified symbol, identifier, affected parameter, replacement, fixable, shutdown date, evidence quote, migration hint, origin (`notes`, `page` or `diff`) |
| `runs` | (repo, target, head SHA) | from version, to version, status, outcome, verification level, timestamps |
| `findings` | id; belongs to a run and a change record | file, line, matched symbol, match tier, confidence, fix tier, fix status |
| `pull_requests` | (repo, package or source) | PR or issue number, branch, state, covered change IDs, declined change IDs |
| `llm_calls` | id; optional run | purpose, model, input, output, tokens in and out, latency, cost |
| `agent_steps` | (run, finding, step number) | tool, arguments, result summary, tokens, latency |

A run's `target` is a string such as `openai@1.45.0` or `openai-deprecations@rev7`.
Confidence is per run, because it depends on the run's API diff; it is stored on
`findings`.

## Acceptance criteria
- [x] One documented command (`python scripts/dev.py`, or `make dev`) creates the environment and a SQLite database, and `pytest` passes. Checked on a fresh copy of the repo.
- [x] Running migrations on a fresh database creates all nine tables; a second run is a no-op. (`tests/test_db.py`)
- [x] The same test suite passes with `DATABASE_URL` pointing at SQLite and at Postgres. Postgres 16 checked in CI.
- [x] A smoke test calls the shared LLM client against the stub, once for structured output and once with a tool call, and asserts the `llm_calls` rows. (`tests/test_llm.py`)
- [x] `guardian --help` lists `poll`, `run` and `ingest`. (`tests/test_pipeline.py`)
- [x] With the pause flag set, a delivery attempt writes nothing and records `paused`. (`tests/test_pipeline.py`)
- [x] `ruff` and `mypy` pass with zero errors in CI (mypy in strict mode).

## Technical notes
- No Redis, RQ or Celery, and no docker compose for services. Docker is needed only
  for the sandbox (spec 06).
- Keep every stage a function of explicit inputs so specs 01–06 and 11 are testable
  without GitHub or GCP.
- The stub LLM takes a script of responses, including tool calls, so agent tests are
  deterministic.

## Test plan
- Integration: migration idempotency, config loading, LLM stub logging, blob store
  round trip, pause flag (F0.2, F0.3, F0.6, F0.7, F0.12).
- CI green = F0.11.

## Out of scope
- Any business logic; later specs.
- GitHub App registration (spec 07) and cloud resources (spec 09).

## Open questions / decisions
- 2026-11-13 (v2): Postgres, Redis and RQ from the v1 spec are dropped.
- 2026-11-13 (implementation): minimum Python is **3.11**, not the 3.12 in
  `docs/tech-stack.md`, because that is what the dev machine has; CI tests 3.11 and
  3.12. Setup uses the standard library's `venv` and pip through `scripts/dev.py`, so it
  needs no `make`, `uv` or Docker. `uv` stays optional.
- 2026-11-13 (implementation): `pipeline.poll`, `run` and `handle_event` return the
  explicit outcome `not-implemented` until their phases land, rather than pretending to
  succeed. The default LLM provider raises `LLMNotConfiguredError` for the same reason.
- 2026-11-13 (implementation): blob keys must be canonical (`a//b` and `./a` are
  rejected), so one blob can never be stored under two names.
- 2026-11-13 (v3): `repos`, `sources`, `agent_steps` added; `release_notes` generalised
  to `source_documents`; the queue carries `poll`, `run` and `event` jobs; pause flag
  added.
