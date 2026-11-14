"""Poll the SDK sources for new and edited releases (spec 01 F1.2–F1.6).

One conditional request per source when nothing changed. A source that fails is
reported and does not stop the others.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import httpx

from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.log import get_logger
from guardian.common.models import Source
from guardian.watch import github_client
from guardian.watch.documents import DocumentStore, Gap, Notes, StoredDocument
from guardian.watch.github import SourceError
from guardian.watch.index import ReleaseIndex
from guardian.watch.sources import SDK_SOURCES, SdkSource, ensure_rows

log = get_logger(__name__)


@dataclass
class SourcePoll:
    source: SdkSource
    unchanged: bool = False  # the conditional request returned 304
    baselined: int = 0  # releases recorded on the first poll, not announced
    new: list[StoredDocument] = field(default_factory=list)
    edited: list[StoredDocument] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    error: str | None = None


def poll_source(ctx: AppContext, source: SdkSource) -> SourcePoll:
    github = github_client(ctx)
    index = ReleaseIndex(source, github, ctx.blobs)
    store = DocumentStore(ctx.sessions, ctx.blobs)
    notes = Notes(source, index, github, store, ctx.blobs)
    result = SourcePoll(source)

    first_poll = index.is_empty
    refresh = index.refresh()
    result.unchanged = refresh.not_modified
    if first_poll:
        result.baselined = len(index.entries)

    # Unhandled entries are new releases. Normally they come from this refresh; after
    # a crash they can be left over from an earlier one, so they are read from the index.
    pending = index.unhandled()
    for version in pending:
        if not index.entry(version).prerelease:
            outcome = notes.materialize(version)
            if isinstance(outcome, Gap):
                result.gaps.append(outcome)
            elif outcome.created:
                result.new.append(outcome)
        index.mark_handled(version)

    # An edited body becomes a new revision, but only for notes already in use.
    for version in refresh.changed:
        if store.has(source.id, str(version)):
            outcome = notes.materialize(version)
            if not isinstance(outcome, Gap) and outcome.created:
                result.edited.append(outcome)

    if pending:
        index.save()  # persist the handled flags
    latest = index.latest_stable()
    with session_scope(ctx.sessions) as session:
        row = session.get(Source, source.id)
        if row is not None:
            row.etag = index.etag
            row.last_seen = str(latest) if latest is not None else None
    return result


def poll_sources(ctx: AppContext, sources: Iterable[SdkSource] = SDK_SOURCES) -> list[SourcePoll]:
    sources = tuple(sources)
    with session_scope(ctx.sessions) as session:
        ensure_rows(session, sources)
    results: list[SourcePoll] = []
    for source in sources:
        try:
            results.append(poll_source(ctx, source))
        except (SourceError, httpx.HTTPError) as exc:
            log.error(
                "source poll failed",
                extra={"fields": {"source": source.id, "error": str(exc)}},
            )
            results.append(SourcePoll(source, error=str(exc)))
    return results
