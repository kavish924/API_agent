"""Pipeline entry points (spec 00 F0.5).

`poll`, `run` and `handle_event` are plain functions of a context and explicit inputs.
The job queue is only a way of invoking them.

Built so far: change watching and the version range (spec 01). Stages that are not
built yet return the explicit outcome `not-implemented`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.common.log import get_logger, run_context
from guardian.common.models import Repo
from guardian.pipeline.types import (
    GATE_APPLIED,
    GATE_NOT_NEEDED,
    GATE_UNAVAILABLE,
    NOT_IMPLEMENTED,
    POLL_FAILED,
    POLL_OK,
    POLL_PARTIAL,
    EventResult,
    PollResult,
    RunRequest,
    RunResult,
)
from guardian.watch.depfiles import LocalRepoFiles
from guardian.watch.documents import StoredDocument
from guardian.watch.github import SourceError
from guardian.watch.poller import poll_sources
from guardian.watch.range import OK, resolve_range
from guardian.watch.sources import SDK_SOURCES, SdkSource, source_for_package

__all__ = [
    "Assessment",
    "Assessor",
    "EventResult",
    "PollResult",
    "RunRequest",
    "RunResult",
    "fan_out",
    "handle_event",
    "poll",
    "register_handlers",
    "run",
]

log = get_logger(__name__)

PACKAGE_NOT_WATCHED = "package-not-watched"
SOURCE_ERROR = "source-error"


@dataclass(frozen=True)
class Assessment:
    """The release gate's verdict on one document (spec 01 F1.7)."""

    breaking: bool  # it holds at least one breaking or deprecating change record
    records_changed: bool = True  # for an edited note: do its records differ from before?


# Decides whether a document passes the release gate. Supplied by extraction and the
# API diff (specs 02 and 03) once they exist.
Assessor = Callable[[StoredDocument], Assessment]


def fan_out(ctx: AppContext, document: StoredDocument) -> int:
    """Enqueue one run per active installed repo for a release that passed the gate."""
    with session_scope(ctx.sessions) as session:
        repos = [
            (repo.full_name, repo.installation_id)
            for repo in session.scalars(select(Repo).where(Repo.active).order_by(Repo.id))
        ]
    for full_name, installation_id in repos:
        request = RunRequest(
            repo=full_name,
            target=f"{document.package}@{document.version}",
            package=document.package,
            installation_id=installation_id,
        )
        ctx.queue.publish(Job(JobType.RUN, request.model_dump()))
    return len(repos)


def poll(
    ctx: AppContext,
    *,
    assessor: Assessor | None = None,
    gate_label: str = GATE_APPLIED,
    sources: Iterable[SdkSource] = SDK_SOURCES,
) -> PollResult:
    """Check every change source, apply the release gate, fan out runs (spec 01).

    `gate_label` is what the result reports when the assessor ran, so a partial gate
    (for example the API diff alone) is never mistaken for the full one.
    """
    with run_context():
        log.info("poll started")
        polls = poll_sources(ctx, sources)
        result = PollResult(outcome=POLL_OK, sources_checked=len(polls))
        assessed = False
        gate_errors: list[str] = []
        for source_poll in polls:
            if source_poll.error is not None:
                result.errors.append(f"{source_poll.source.id}: {source_poll.error}")
                continue
            result.sources_unchanged += source_poll.unchanged
            result.baselined += source_poll.baselined
            result.new_documents += len(source_poll.new)
            result.edited_documents += len(source_poll.edited)
            result.gaps += len(source_poll.gaps)
            for document in [*source_poll.new, *source_poll.edited]:
                if assessor is None:
                    continue
                assessed = True
                try:
                    verdict = assessor(document)
                except Exception as exc:  # a gate failure must not stop the poll
                    label = f"{document.package} {document.version}"
                    log.error("gate failed", extra={"fields": {"note": label, "error": str(exc)}})
                    gate_errors.append(f"gate for {label}: {exc}")
                    continue
                if verdict.breaking and (not document.is_edit or verdict.records_changed):
                    result.runs_enqueued += fan_out(ctx, document)

        if result.errors:
            result.outcome = POLL_FAILED if len(result.errors) == len(polls) else POLL_PARTIAL
        if gate_errors:
            result.errors += gate_errors
            result.outcome = POLL_PARTIAL
        if assessed:
            result.gate = gate_label
        elif result.new_documents or result.edited_documents:
            result.gate = GATE_UNAVAILABLE
        else:
            result.gate = GATE_NOT_NEEDED
        log.info("poll finished", extra={"fields": result.model_dump()})
        return result


def run(request: RunRequest, ctx: AppContext) -> RunResult:
    """One run for one repo and target: range, scan, fix, verify, deliver."""
    with run_context():
        log.info("run started", extra={"fields": {"repo": request.repo, "target": request.target}})
        result = _run(request, ctx)
        log.info("run finished", extra={"fields": result.model_dump()})
        return result


def _run(request: RunRequest, ctx: AppContext) -> RunResult:
    package = request.package or request.target.split("@", 1)[0]
    source = source_for_package(package)
    if source is None:
        return RunResult(outcome=PACKAGE_NOT_WATCHED, package=package)

    checkout = Path(request.repo)
    files = LocalRepoFiles(checkout) if checkout.is_dir() else None
    if files is None and request.from_version is None:
        return RunResult(
            outcome=NOT_IMPLEMENTED,
            package=package,
            reason="reading an installed repository needs the GitHub App (spec 07)",
        )

    try:
        version_range = resolve_range(
            ctx,
            source,
            files,
            from_version=request.from_version,
            to_version=request.to_version,
        )
    except (SourceError, httpx.HTTPError) as exc:
        return RunResult(outcome=SOURCE_ERROR, package=package, reason=str(exc))

    result = RunResult(
        outcome=version_range.outcome,
        package=package,
        from_version=version_range.old,
        to_version=version_range.new,
        pinned_in=version_range.decided_by,
        notes=len(version_range.documents),
        notes_stored=version_range.stored,
        gaps=len(version_range.gaps),
    )
    if version_range.outcome != OK:
        return result  # the run ends here, silently, with the reason recorded

    result.outcome = NOT_IMPLEMENTED
    result.reason = "range resolved; extraction, scanning and fixing are not built yet"
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
