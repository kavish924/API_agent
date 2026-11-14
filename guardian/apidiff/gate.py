"""The API-diff half of the release gate (spec 03 F3.11, spec 01 F1.7).

At poll time a new release is diffed against the release before it. If the code shows
a breaking change, the release passes the gate even when its notes say nothing.

This is only half the gate. Changes the diff cannot see (response shapes, behaviour,
dependency swaps) pass only through extraction (spec 02).
"""

from __future__ import annotations

from collections.abc import Iterable

from packaging.version import Version

from guardian.apidiff.service import ApiDiffService
from guardian.common.context import AppContext
from guardian.common.log import get_logger
from guardian.pipeline import Assessment, Assessor
from guardian.watch import github_client
from guardian.watch.documents import StoredDocument
from guardian.watch.index import ReleaseIndex
from guardian.watch.sources import SDK_SOURCES, SdkSource, source_for_package

log = get_logger(__name__)


def make_diff_assessor(
    ctx: AppContext,
    service: ApiDiffService | None = None,
    sources: Iterable[SdkSource] = SDK_SOURCES,
) -> Assessor:
    api = service or ApiDiffService.from_context(ctx)
    watched = tuple(sources)

    def assess(document: StoredDocument) -> Assessment:
        source = source_for_package(document.package, watched)
        if source is None:
            return Assessment(breaking=False, records_changed=False)
        index = ReleaseIndex(source, github_client(ctx), ctx.blobs)
        version = Version(document.version)
        earlier = [v for v in index.versions() if v < version]
        if not earlier:
            return Assessment(breaking=False, records_changed=False)
        diff = api.diff(source, str(earlier[-1]), document.version)
        log.info(
            "gate diff",
            extra={
                "fields": {
                    "package": document.package,
                    "old": diff.old,
                    "new": diff.new,
                    "changes": len(diff.changes),
                    "ignored": diff.ignored,
                }
            },
        )
        # An edited note does not change the code, so it never changes this verdict.
        return Assessment(breaking=diff.breaking, records_changed=False)

    return assess
