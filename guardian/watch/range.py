"""The version range of one run, and its release notes (spec 01 F1.10–F1.17).

Old is the version the repository pins; new is the latest stable release. The notes
for every version in (old, new] are stored on demand and reused by later runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.version import InvalidVersion, Version

from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.watch import github_client
from guardian.watch.depfiles import PINNED, PinLocation, RepoFiles, find_pin
from guardian.watch.documents import DocumentStore, Gap, Notes, StoredDocument
from guardian.watch.index import ReleaseIndex
from guardian.watch.sources import SdkSource, ensure_rows

OK = "ok"
UP_TO_DATE = "up-to-date"
RANGE_TOO_LARGE = "range-too-large"
INVALID_VERSION = "invalid-version"
NO_RELEASES = "no-releases"

DEFAULT_RANGE_CAP = 200


@dataclass
class RangeResult:
    outcome: str  # OK, or the reason the run ends here
    package: str
    old: str | None = None
    new: str | None = None
    decided_by: str | None = None  # the file the pinned version came from
    pins: tuple[PinLocation, ...] = ()
    documents: list[StoredDocument] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    stored: int = 0  # notes stored by this call (0 when all were cached)
    versions: int = 0  # versions in the range


def _parse(value: str) -> Version | None:
    try:
        return Version(value)
    except InvalidVersion:
        return None


def resolve_range(
    ctx: AppContext,
    source: SdkSource,
    files: RepoFiles | None,
    *,
    from_version: str | None = None,
    to_version: str | None = None,
    cap: int = DEFAULT_RANGE_CAP,
) -> RangeResult:
    """Work out (old, new] for a repository and make sure its notes are stored.

    `from_version` and `to_version` override the pinned version and the latest
    release (the CLI flags). `files` may be None only when `from_version` is given.
    """
    result = RangeResult(OK, source.package)

    if from_version is not None:
        result.old = from_version
    else:
        if files is None:
            raise ValueError("files is required when from_version is not given")
        pin = find_pin(files, source.package)
        result.pins = pin.locations
        if pin.status != PINNED:
            result.outcome = pin.status
            return result
        result.old, result.decided_by = pin.version, pin.decided_by
    old = _parse(result.old or "")
    if old is None:
        result.outcome = INVALID_VERSION
        return result

    with session_scope(ctx.sessions) as session:
        ensure_rows(session, (source,))
    github = github_client(ctx)
    index = ReleaseIndex(source, github, ctx.blobs)
    index.refresh()
    index.ensure_complete()

    new = _parse(to_version) if to_version is not None else index.latest_stable()
    if new is None:
        result.outcome = INVALID_VERSION if to_version is not None else NO_RELEASES
        return result
    result.new = str(new)
    if old >= new:
        result.outcome = UP_TO_DATE
        return result

    # Pre-releases are skipped unless the repository already pins one.
    in_range = [v for v in index.versions(include_prereleases=old.is_prerelease) if old < v <= new]
    result.versions = len(in_range)
    if len(in_range) > cap:
        result.outcome = RANGE_TOO_LARGE
        return result

    notes = Notes(source, index, github, DocumentStore(ctx.sessions, ctx.blobs), ctx.blobs)
    for version in in_range:
        outcome = notes.materialize(version)
        if isinstance(outcome, Gap):
            result.gaps.append(outcome)
        else:
            result.documents.append(outcome)
            result.stored += outcome.created
    return result
