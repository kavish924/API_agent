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

from guardian.agent import AgentFixer, Budget, StaticTypeChecker, Workspace
from guardian.apidiff.service import ApiDiffService
from guardian.apidiff.wheels import cached_wheel
from guardian.apilevel.run import latest_upcoming, record_api_run, run_api
from guardian.apilevel.source import API_SOURCES, ApiSource, api_source, poll_api_source
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.common.log import get_logger, run_context
from guardian.common.models import Repo
from guardian.fix import (
    FIX_FAILED,
    FIXED,
    MODE_AGENT,
    MODE_SINGLE_SHOT,
    MODES,
    REPORT_ONLY,
    PatchSet,
    SingleShotFixer,
    Tier2Fixer,
    build_patch,
)
from guardian.github_app.checkout import CheckoutError, RemoteRepo, fetch_checkout
from guardian.github_app.client import GitHubError
from guardian.github_app.config import read_config
from guardian.github_app.events import apply_event
from guardian.github_app.publish import SKIPPED, Publisher
from guardian.github_app.render import Report
from guardian.github_app.reports import api_report, sdk_report
from guardian.pipeline.grade import Grading, grade_range
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
from guardian.scan.store import (
    LOCAL_INSTALLATION,
    finding_row_ids,
    local_identity,
    record_fixes,
    record_scan,
)
from guardian.verify.static import Verification, verify_static
from guardian.watch.depfiles import NOT_USED, LocalRepoFiles, find_pin
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
DELIVERY_FAILED = "delivery-failed"  # a GitHub call failed; the next run retries it
REPO_INACTIVE = "repo-inactive"  # the App was uninstalled there (F7.20)
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
    api_sources: Iterable[ApiSource] = API_SOURCES,
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

        for api in api_sources:  # spec 10: provider pages, gated by their own records
            page = poll_api_source(ctx, api)
            result.api_pages_checked += 1
            if page.error is not None:
                result.errors.append(f"{api.id}: {page.error}")
                continue
            if page.not_modified or (page.snapshot is not None and not page.snapshot.created):
                result.api_pages_unchanged += 1
            result.api_records_new += len(page.new_records)
            result.runs_enqueued += page.runs_enqueued
        # Decided once, over every source checked: SDK sources and API pages alike.
        failed, checked = len(result.errors), len(polls) + result.api_pages_checked
        if failed:
            result.outcome = POLL_FAILED if failed == checked else POLL_PARTIAL
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
    remote: RemoteRepo | None = None
    if request.installation_id is not None and not Path(request.repo).is_dir():
        fetched = _fetch_remote(request, ctx)
        if isinstance(fetched, RunResult):
            return fetched
        remote = fetched
        request = request.model_copy(update={"repo": str(remote.path), "head_sha": remote.head_sha})
    api = api_source(request.target.split("@", 1)[0])
    if api is not None and request.package is None:
        return _run_api(request, ctx, api, remote)
    return _run_sdk(request, ctx, remote)


def _fetch_remote(request: RunRequest, ctx: AppContext) -> RemoteRepo | RunResult:
    """Download an installed repo at its default-branch commit (spec 07, spec 04 F4.1)."""
    assert request.installation_id is not None
    if ctx.github is None:
        return RunResult(
            outcome=NOT_IMPLEMENTED,
            reason="the GitHub App is not configured (GUARDIAN_GITHUB_APP_ID and key)",
        )
    with session_scope(ctx.sessions) as session:
        row = session.scalars(
            select(Repo).where(
                Repo.installation_id == request.installation_id,
                Repo.full_name == request.repo,
            )
        ).first()
        if row is not None and not row.active:
            return RunResult(outcome=REPO_INACTIVE, reason="the App is not installed there")
    try:
        return fetch_checkout(
            ctx.github, request.installation_id, request.repo, ctx.settings.work_dir
        )
    except (GitHubError, CheckoutError, httpx.HTTPError) as exc:
        return RunResult(outcome=SOURCE_ERROR, reason=f"could not read {request.repo}: {exc}")


def _identity(
    checkout: Path, remote: RemoteRepo | None, request: RunRequest
) -> tuple[str, int, str]:
    """(repo name, installation id, commit) a run is stored under."""
    if remote is not None:
        return remote.full_name, remote.installation_id, remote.head_sha
    repo, head = local_identity(checkout)
    return repo, LOCAL_INSTALLATION, request.head_sha or head


def _run_sdk(request: RunRequest, ctx: AppContext, remote: RemoteRepo | None) -> RunResult:
    package = request.package or request.target.split("@", 1)[0]
    source = source_for_package(package)
    if source is None:
        return RunResult(outcome=PACKAGE_NOT_WATCHED, package=package)
    if remote is not None and not read_config(remote.path).wants(source.package):
        return RunResult(outcome=SKIPPED, package=package, reason="not in packages")

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
    repo, installation, head = _identity(checkout, remote, request)
    assert version_range.old is not None and version_range.new is not None
    result.run_id = record_scan(
        ctx.sessions,
        repo=repo,
        installation_id=installation,
        target=f"{source.package}@{version_range.new}",
        head=head,
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
    elif mode == MODE_AGENT:
        fixer = _agent(ctx, source, checkout, version_range.new, grading, scan.findings, result)
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
    checker = StaticTypeChecker(
        checkout,
        lambda: _wheel(ctx, source.package, version_range.new or ""),
        base_sdk_dir=lambda: _wheel(ctx, source.package, version_range.old or ""),
    )
    verification = _verify(result, scan.findings, patch, checker)
    if remote is not None:
        report = sdk_report(
            source,
            version_range.old,
            version_range.new,
            scan.findings,
            grading.changes,
            patch,
            verification,
        )
        _publish(ctx, remote, report, result)
    return result


def _publish(ctx: AppContext, remote: RemoteRepo, report: Report, result: RunResult) -> None:
    assert ctx.github is not None
    try:
        published = Publisher(ctx, ctx.github, remote).publish(report, read_config(remote.path))
    except (GitHubError, httpx.HTTPError) as exc:
        # The run's work is stored; delivery is retried by the next run for this target.
        log.error(
            "delivery failed", extra={"fields": {"repo": remote.full_name, "error": str(exc)}}
        )
        result.delivery = DELIVERY_FAILED
        result.reason = f"delivery failed: {exc}"
        return
    result.delivery = published.action
    result.delivery_number = published.number
    result.delivery_url = published.url
    if published.reason:
        result.reason = published.reason


def _wheel(ctx: AppContext, package: str, version: str) -> Path:
    return cached_wheel(
        ctx.http, package, version, ctx.settings.work_dir / "wheels", sleep=ctx.sleep
    )


def _verify(
    result: RunResult,
    findings: Sequence[Finding],
    patch: PatchSet,
    checker: StaticTypeChecker,
) -> Verification | None:
    """Interim static L1/L2 on the patch (decision 2026-11-25), recorded on the result."""
    if not patch.count(FIXED):
        result.reason = "nothing was fixed: the findings go to an issue (spec 07)"
        return None
    left: dict[str, list[int]] = {}
    for fix in patch.fixes:
        if fix.status != FIXED:
            finding = findings[fix.index]
            left.setdefault(finding.file, []).append(finding.line)
    verification = verify_static(patch.files, checker, report_only_lines=left)
    result.verification_level = verification.level
    result.verification = {
        "compile_errors": verification.compile_errors,
        "lint_errors": verification.lint_errors,
        "type_errors": verification.type_errors,
        "remaining": verification.remaining,
        "notes": verification.notes,
    }
    result.reason = (
        f"patch built and verified to {verification.level} (static; tests not run)"
        if verification.verified
        else "the patch could not be verified: it goes to an issue (spec 07)"
    )
    return verification


def _run_api(
    request: RunRequest, ctx: AppContext, api: ApiSource, remote: RemoteRepo | None
) -> RunResult:
    """An API-level run: model retirements on a provider page (spec 10)."""
    result = RunResult(outcome=NOT_IMPLEMENTED, api_source=api.id, behaviour_change=True)
    if remote is not None and read_config(remote.path).api_changes == "off":
        result.outcome, result.reason = SKIPPED, "api_changes is off"
        return result
    checkout = Path(request.repo)
    if not checkout.is_dir():
        result.reason = "reading an installed repository needs the GitHub App (spec 07)"
        return result
    records = latest_upcoming(ctx, api)
    result.api_records = len(records)
    if not records:
        result.outcome = NO_FINDINGS
        result.reason = f"no upcoming records: {api.id} has no snapshot yet, or nothing is due"
        return result
    files = LocalRepoFiles(checkout)
    pin = find_pin(files, api.module)
    if pin.status == NOT_USED:
        result.outcome = NOT_USED
        result.reason = f"the repository does not use {api.module}"
        return result
    sdk = source_for_package(api.module)
    if sdk is None:
        result.reason = f"{api.module} is not a watched SDK"
        return result
    try:
        version = pin.version or _latest_version(ctx, api.module)
        surface = ApiDiffService.from_context(ctx).surface(sdk, version)
    except Exception as exc:  # the SDK surface is needed to resolve calls
        result.outcome = SOURCE_ERROR
        result.reason = f"could not load the {api.module} API: {exc}"
        return result
    result.package, result.from_version = api.module, version

    outcome = run_api(checkout, api, records, surface)
    assert outcome.scan is not None
    result.findings = len(outcome.findings)
    result.finding_sites = len({(f.file, f.line, f.column) for f in outcome.findings})
    result.files_scanned, result.files_parsed = outcome.scan.files_total, outcome.scan.files_parsed
    result.possible_usages = [str(usage) for usage in outcome.possible]
    shutdown = outcome.earliest_shutdown
    result.shutdown_date = shutdown.isoformat() if shutdown else None
    repo, installation, head = _identity(checkout, remote, request)
    status = NO_FINDINGS if not outcome.findings else NOT_IMPLEMENTED
    result.run_id = record_api_run(
        ctx.sessions,
        repo=repo,
        installation_id=installation,
        target=request.target,
        head=head,
        result=outcome,
        outcome=status,
    )
    result.outcome = status
    if not outcome.findings:
        result.reason = "no Python call passes a retiring model to its own API"
        return result
    patch = outcome.patch
    assert patch is not None
    result.patch_status = patch.status
    result.fixed = patch.count(FIXED)
    result.report_only = patch.count(REPORT_ONLY)
    result.fix_failed = patch.count(FIX_FAILED)
    result.patch_files = sorted(patch.files)
    if patch.files:
        key = f"patches/run-{result.run_id}.diff"
        result.patch_ref = ctx.blobs.put(key, patch.diff().encode("utf-8"))
    # One environment: no bump, so base and fixed share the SDK (F10.11).
    checker = StaticTypeChecker(checkout, lambda: _wheel(ctx, sdk.package, version))
    verification = _verify(result, outcome.findings, patch, checker)
    if remote is not None:
        _publish(ctx, remote, api_report(api, outcome, patch, verification), result)
    return result


def _latest_version(ctx: AppContext, package: str) -> str:
    response = ctx.http.get(f"https://pypi.org/pypi/{package}/json")
    response.raise_for_status()
    return str(response.json()["info"]["version"])


def _agent(
    ctx: AppContext,
    source: SdkSource,
    checkout: Path,
    new_version: str,
    grading: Grading,
    findings: Sequence[Finding],
    result: RunResult,
) -> AgentFixer:
    settings = ctx.settings
    assert result.run_id is not None

    def sdk_dir() -> Path:
        return _wheel(ctx, source.package, new_version)

    return AgentFixer(
        ctx.llm,
        Workspace(checkout),
        module=source.module,
        new_surface=grading.new_surface,
        typechecker=StaticTypeChecker(checkout, sdk_dir),
        budget=Budget(
            max_steps=settings.agent_max_steps,
            max_proposals=settings.agent_max_proposals,
            max_tokens=settings.agent_max_tokens,
            max_seconds=settings.agent_max_seconds,
            run_cost_cap=settings.agent_run_cost_cap,
        ),
        sessions=ctx.sessions,
        run_id=result.run_id,
        finding_ids=finding_row_ids(ctx.sessions, result.run_id, findings),
        line_cap=settings.fix_line_cap,
    )


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
    """Apply a GitHub installation event to the `repos` table (spec 07 F7.4, F7.5, F7.20).

    `payload` is the event job's payload: `{"event": ..., "payload": <GitHub body>}`.
    """
    with run_context():
        event = str(payload.get("event", ""))
        body = payload.get("payload") or {}
        log.info("event received", extra={"fields": {"event": event, "action": body.get("action")}})
        change = apply_event(ctx, event, body)
        return EventResult(
            outcome="applied",
            activated=change.activated,
            deactivated=change.deactivated,
            runs_enqueued=change.runs_enqueued,
        )


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
