"""API-page sources: fetch, snapshot, gate and fan out (spec 10 F10.1–F10.3, F10.6, F10.7, F10.14).

A page is fetched with conditional requests (`If-None-Match`, `If-Modified-Since`).
New content is stored byte-for-byte as a new snapshot revision, blob first, then the
row. Records are parsed from the snapshot with plain code and compared with the
previous revision's: only new or changed records start runs, one per active repo,
with target `<source>@r<revision>`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardian.apilevel.page import ApiRecord, changed_records, parse_page
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.http import request_with_retry
from guardian.common.jobs import Job, JobType
from guardian.common.log import get_logger
from guardian.common.models import Repo, Source, SourceDocument

log = get_logger(__name__)

API_PAGE = "api-page"
SNAPSHOT = "snapshot"  # the `version` of every snapshot row; revisions number them


@dataclass(frozen=True)
class ApiSource:
    id: str
    provider: str
    url: str
    module: str  # the SDK whose calls carry the identifiers, e.g. openai


API_SOURCES: tuple[ApiSource, ...] = (
    ApiSource(
        id="openai-deprecations",
        provider="openai",
        url="https://developers.openai.com/api/docs/deprecations.md",
        module="openai",
    ),
)


def api_source(source_id: str) -> ApiSource | None:
    return next((s for s in API_SOURCES if s.id == source_id), None)


@dataclass(frozen=True)
class Snapshot:
    source_id: str
    revision: int
    blob_ref: str
    created: bool  # False: this exact content was the latest snapshot already


@dataclass
class ApiPoll:
    source: ApiSource
    not_modified: bool = False  # the server answered 304
    snapshot: Snapshot | None = None
    new_records: list[ApiRecord] = field(default_factory=list)
    runs_enqueued: int = 0
    error: str | None = None


def _ensure_row(ctx: AppContext, source: ApiSource) -> None:
    with session_scope(ctx.sessions) as session:
        if session.get(Source, source.id) is None:
            session.add(
                Source(
                    id=source.id,
                    kind=API_PAGE,
                    provider=source.provider,
                    package=None,
                    url=source.url,
                )
            )


def store_snapshot(ctx: AppContext, source: ApiSource, data: bytes) -> Snapshot:
    """Store new content as the next revision; unchanged content stores nothing (F10.2)."""
    _ensure_row(ctx, source)
    digest = hashlib.sha256(data).hexdigest()
    with session_scope(ctx.sessions) as session:
        latest = _latest(session, source.id)
        if latest is not None and latest.content_hash == digest:
            return Snapshot(source.id, latest.revision, latest.blob_ref, created=False)
        revision = latest.revision + 1 if latest is not None else 1
        ref = ctx.blobs.put(f"api/{source.id}/r{revision}.md", data)
        session.add(
            SourceDocument(
                source_id=source.id,
                version=SNAPSHOT,
                revision=revision,
                content_hash=digest,
                blob_ref=ref,
            )
        )
        return Snapshot(source.id, revision, ref, created=True)


def _latest(session: Session, source_id: str) -> SourceDocument | None:
    return session.scalars(
        select(SourceDocument)
        .where(SourceDocument.source_id == source_id, SourceDocument.version == SNAPSHOT)
        .order_by(SourceDocument.revision.desc())
        .limit(1)
    ).first()


def snapshot_text(ctx: AppContext, source: ApiSource, revision: int | None = None) -> str | None:
    """The Markdown of a snapshot revision; the latest when `revision` is None."""
    with session_scope(ctx.sessions) as session:
        query = select(SourceDocument).where(
            SourceDocument.source_id == source.id, SourceDocument.version == SNAPSHOT
        )
        if revision is not None:
            query = query.where(SourceDocument.revision == revision)
        row = session.scalars(query.order_by(SourceDocument.revision.desc()).limit(1)).first()
        ref = row.blob_ref if row is not None else None
    return ctx.blobs.get(ref).decode("utf-8") if ref is not None else None


def records_at(ctx: AppContext, source: ApiSource, revision: int | None = None) -> list[ApiRecord]:
    text = snapshot_text(ctx, source, revision)
    return parse_page(text, provider=source.provider) if text is not None else []


def new_records(
    ctx: AppContext, source: ApiSource, snapshot: Snapshot, today: date
) -> list[ApiRecord]:
    """What this snapshot adds or changes compared with the one before it."""
    current = records_at(ctx, source, snapshot.revision)
    previous = records_at(ctx, source, snapshot.revision - 1) if snapshot.revision > 1 else None
    return changed_records(previous, current, today=today)


def fan_out_api(ctx: AppContext, source: ApiSource, revision: int) -> int:
    with session_scope(ctx.sessions) as session:
        repos = [
            (repo.full_name, repo.installation_id)
            for repo in session.scalars(select(Repo).where(Repo.active).order_by(Repo.id))
        ]
    for full_name, installation_id in repos:
        payload = {
            "repo": full_name,
            "target": f"{source.id}@r{revision}",
            "installation_id": installation_id,
        }
        ctx.queue.publish(Job(JobType.RUN, payload))
    return len(repos)


def _gate(ctx: AppContext, result: ApiPoll, today: date) -> None:
    assert result.snapshot is not None
    if not result.snapshot.created:
        return  # unchanged content: nothing new, no runs (acceptance: unchanged page)
    result.new_records = new_records(ctx, result.source, result.snapshot, today)
    if result.new_records:
        result.runs_enqueued = fan_out_api(ctx, result.source, result.snapshot.revision)


def poll_api_source(ctx: AppContext, source: ApiSource, *, today: date | None = None) -> ApiPoll:
    """One conditional fetch; a new snapshot is gated and fanned out (F10.1, F10.6)."""
    today = today or datetime.now(UTC).date()
    _ensure_row(ctx, source)
    result = ApiPoll(source)
    with session_scope(ctx.sessions) as session:
        row = session.get(Source, source.id)
        assert row is not None
        etag, last_modified = row.etag, row.last_seen
    headers = {"Accept": "text/markdown"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        response = request_with_retry(ctx.http, "GET", source.url, headers=headers, sleep=ctx.sleep)
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    if response.status_code == 304:
        result.not_modified = True
        return result
    if response.status_code != 200:
        result.error = f"GET {source.url}: HTTP {response.status_code}"
        return result
    result.snapshot = store_snapshot(ctx, source, response.content)
    with session_scope(ctx.sessions) as session:
        row = session.get(Source, source.id)
        assert row is not None
        row.etag = response.headers.get("ETag")
        row.last_seen = response.headers.get("Last-Modified")
    _gate(ctx, result, today)
    log.info(
        "api page polled",
        extra={
            "fields": {
                "source": source.id,
                "revision": result.snapshot.revision,
                "new_records": len(result.new_records),
                "runs": result.runs_enqueued,
            }
        },
    )
    return result


def ingest(
    ctx: AppContext, source: ApiSource, data: bytes, *, today: date | None = None
) -> ApiPoll:
    """Store a saved copy of the page, exactly as a fetch would (F10.14)."""
    result = ApiPoll(source, snapshot=store_snapshot(ctx, source, data))
    _gate(ctx, result, today or datetime.now(UTC).date())
    return result
