"""Pipeline entry points (spec 00 F0.5).

`poll`, `run` and `handle_event` are plain functions of a context and explicit inputs.
The job queue is only a way of invoking them.

Built so far: change watching and the version range (spec 01), grading (specs 02 and
03), scanning (spec 04) and fix generation (spec 05). Stages that are not built yet
return the explicit outcome `not-implemented`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
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
from guardian.fix import (
    FIX_FAILED,
    FIXED,
    MODE_SINGLE_SHOT,
    MODES,
    REPORT_ONLY,
    PatchSet,
    SingleShotFixer,
    Tier2Fixer,
    build_patch,
)
from guardian.pipeline.grade import grade_range
from guardian.pipeline.types import (
    GATE_APPLIED,
    GATE_NOT_NEEDED,
    GATE_UNAVAILABLE,
    NO_FINDINGS,
    NOT_IMPLEMENTED,
    POLL_FAILED,
    POLL_OK,
    POLL_PARTIAL,
    EventResult,
    PollResult,
    RunRequest,
    RunResult,
)
from guardian.scan.analyzer import Finding
from guardian.scan.scanner import scan_repo
from guardian.scan.store import LOCAL_INSTALLATION, local_identity, record_fixes, record_scan
from guardian.watch.depfiles import LocalRepoFiles
from guardian.watch.documents import StoredDocument
from guardian.watch.github import SourceError
from guardian.watch.poller import mark_assessed, poll_sources
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
            decided: list[str] = []
            for document in source_poll.pending:
                if assessor is None:
                    continue  # stays pending until a poll has a gate
                assessed = True
                try:
                    verdict = assessor(document)
                except Exception as exc:  # a gate failure must not stop the poll
                    label = f"{document.package} {document.version}"
                    log.error("gate failed", extra={"fields": {"note": label, "error": str(exc)}})
                    gate_errors.append(f"gate for {label}: {exc}")
                    continue  # stays pending: the next poll tries again
                decided.append(document.version)
                if verdict.breaking and (document.revision == 1 or verdict.records_changed):
                    result.runs_enqueued += fan_out(ctx, document)
            mark_assessed(ctx, source_poll.source, decided)
            result.assessed += len(decided)
            result.awaiting_gate += len(source_poll.pending) - len(decided)

        if result.errors:
            result.outcome = POLL_FAILED if len(result.errors) == len(polls) else POLL_PARTIAL
        if gate_errors:
            result.errors += gate_errors
            result.outcome = POLL_PARTIAL
        if assessed:
            result.gate = gate_label
        elif result.awaiting_gate:
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

    grading = grade_range(ctx, source, version_range)
    result.extraction = grading.extraction
    result.notes_failed = grading.notes_failed
    result.api_diff = grading.api_diff
    result.change_types = grading.by_type()
    for name, value in grading.summary().items():
        setattr(result, name, value)

    result.outcome = NOT_IMPLEMENTED
    if grading.api_diff_error is not None or grading.old_surface is None:
        result.reason = f"api diff failed: {grading.api_diff_error}"
        return result
    if files is None:
        result.reason = "changes graded; scanning needs a local checkout or the GitHub App"
        return result

    scan = scan_repo(checkout, source.module, grading.old_surface, grading.changes)
    result.findings = len(scan.findings)
    result.finding_sites = scan.sites()
    result.findings_by_tier = scan.by_tier()
    result.files_scanned = scan.files_total
    result.files_parsed = scan.files_parsed
    result.files_skipped = len(scan.skipped)
    result.scan_seconds = round(scan.seconds, 2)

    outcome = NO_FINDINGS if not scan.findings else NOT_IMPLEMENTED
    repo, head = local_identity(checkout)
    assert version_range.old is not None and version_range.new is not None
    result.run_id = record_scan(
        ctx.sessions,
        repo=repo,
        installation_id=LOCAL_INSTALLATION,
        target=f"{source.package}@{version_range.new}",
        head=request.head_sha or head,
        package=source.package,
        from_version=version_range.old,
        to_version=version_range.new,
        changes=grading.changes,
        result=scan,
        outcome=outcome,
    )
    result.outcome = outcome
    if outcome == NO_FINDINGS:
        result.reason = "no code in this repository uses what changed"  # F4.8: the run ends here
        return result

    mode = request.fix_mode or ctx.settings.fix_mode
    if mode not in MODES:
        result.reason = f"unknown fix mode {mode!r}; expected one of {', '.join(MODES)}"
        return result
    fixer: Tier2Fixer | None = None
    if mode == MODE_SINGLE_SHOT:
        fixer = SingleShotFixer(ctx.llm, run_id=result.run_id)
    patch = build_patch(
        files,
        scan.findings,
        grading.changes,
        package=source.package,
        module=source.module,
        old=version_range.old,
        new=version_range.new,
        mode=mode,
        fixer=fixer,
        line_cap=ctx.settings.fix_line_cap,
    )
    _record_patch(ctx, result, scan.findings, patch, mode)
    result.reason = "patch built; verification is not built yet (spec 06)"
    return result


def _record_patch(
    ctx: AppContext,
    result: RunResult,
    findings: Sequence[Finding],
    patch: PatchSet,
    mode: str,
) -> None:
    assert result.run_id is not None
    fixes = [(fix.index, fix.status, fix.tier) for fix in patch.fixes]
    record_fixes(ctx.sessions, result.run_id, findings, fixes)
    result.patch_status = patch.status
    result.fix_mode = mode
    result.fixed = patch.count(FIXED)
    result.report_only = patch.count(REPORT_ONLY)
    result.fix_failed = patch.count(FIX_FAILED)
    result.patch_files = sorted(patch.files)
    result.lock_files = patch.lock_files
    result.bump_problems = [
        f"{problem.file}: {problem.reason} {problem.detail}".strip()
        for problem in patch.bump_problems
    ]
    result.fix_cost = round(patch.cost, 6)
    if patch.files:
        key = f"patches/run-{result.run_id}.diff"
        result.patch_ref = ctx.blobs.put(key, patch.diff().encode("utf-8"))


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
