"""Pipeline entry points (spec 00 F0.5).

`poll`, `run` and `handle_event` are plain functions of a context and explicit inputs.
The job queue is only a way of invoking them. In Phase 0 they are stubs that return an
explicit `not-implemented` outcome; later phases fill in the stages.
"""

from __future__ import annotations

from typing import Any

from guardian.common.context import AppContext
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.common.log import get_logger, run_context
from guardian.pipeline.types import (
    NOT_IMPLEMENTED,
    EventResult,
    PollResult,
    RunRequest,
    RunResult,
)

__all__ = [
    "EventResult",
    "PollResult",
    "RunRequest",
    "RunResult",
    "handle_event",
    "poll",
    "register_handlers",
    "run",
]

log = get_logger(__name__)


def poll(ctx: AppContext) -> PollResult:
    """Check every change source, apply the release gate, fan out runs (spec 01)."""
    with run_context():
        log.info("poll started")
        result = PollResult(outcome=NOT_IMPLEMENTED)
        log.info("poll finished", extra={"fields": result.model_dump()})
        return result


def run(request: RunRequest, ctx: AppContext) -> RunResult:
    """One run for one repo and target: range, scan, fix, verify, deliver."""
    with run_context():
        log.info("run started", extra={"fields": {"repo": request.repo, "target": request.target}})
        result = RunResult(outcome=NOT_IMPLEMENTED)
        log.info("run finished", extra={"fields": result.model_dump()})
        return result


def handle_event(payload: dict[str, Any], ctx: AppContext) -> EventResult:
    """Apply a GitHub installation event to the `repos` table (spec 07)."""
    with run_context():
        log.info("event received", extra={"fields": {"action": payload.get("action")}})
        return EventResult(outcome=NOT_IMPLEMENTED)


def register_handlers(queue: InProcessQueue, ctx: AppContext) -> None:
    """Connect the three job types to their entry points."""

    def on_poll(_job: Job) -> None:
        poll(ctx)

    def on_run(job: Job) -> None:
        run(RunRequest.model_validate(job.payload), ctx)

    def on_event(job: Job) -> None:
        handle_event(job.payload, ctx)

    queue.register(JobType.POLL, on_poll)
    queue.register(JobType.RUN, on_run)
    queue.register(JobType.EVENT, on_event)
