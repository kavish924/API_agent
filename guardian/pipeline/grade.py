"""Grade the changes in a run's version range (specs 02 and 03 together).

Every note in (old, new] is extracted (cached per note), the API of the exact old and
new versions is diffed (cached per pair), and reconciliation turns both into graded
changes: what changed, how sure we are, whether a codemod or the fixer can handle it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import httpx

from guardian.apidiff.reconcile import FROM_DIFF, HIGH, GradedChange, reconcile
from guardian.apidiff.service import ApiDiffService
from guardian.apidiff.surface import Surface, SurfaceError
from guardian.apidiff.wheels import WheelError
from guardian.common.context import AppContext
from guardian.common.llm import LLMError
from guardian.common.log import get_logger
from guardian.extract.extractor import EXTRACTED, Extractor
from guardian.extract.schema import ExtractedChange
from guardian.watch.range import RangeResult
from guardian.watch.sources import SdkSource

log = get_logger(__name__)

EXTRACTION_DONE = "done"
EXTRACTION_PARTIAL = "partial"  # some notes failed after two attempts
EXTRACTION_UNAVAILABLE = "unavailable"  # no LLM provider configured

API_DIFF_DONE = "done"
API_DIFF_FAILED = "failed"


@dataclass
class Grading:
    extraction: str = EXTRACTION_DONE
    records: list[ExtractedChange] = field(default_factory=list)
    notes_failed: int = 0
    api_diff: str = API_DIFF_DONE
    api_diff_error: str | None = None
    changes: list[GradedChange] = field(default_factory=list)
    dropped: int = 0
    old_surface: Surface | None = None  # the API the repository is on, for the scanner

    def summary(self) -> dict[str, int]:
        return {
            "changes": len(self.changes),
            "changes_high": sum(c.confidence == HIGH for c in self.changes),
            "changes_fixable": sum(c.fixable for c in self.changes),
            "changes_from_diff": sum(c.origin == FROM_DIFF for c in self.changes),
            "records": len(self.records),
            "records_dropped": self.dropped,
        }

    def by_type(self) -> dict[str, int]:
        return dict(Counter(c.change_type.value for c in self.changes).most_common())


def grade_range(ctx: AppContext, source: SdkSource, version_range: RangeResult) -> Grading:
    grading = Grading()

    extractor = Extractor(ctx.llm, ctx.sessions, ctx.blobs)
    if not extractor.available:
        grading.extraction = EXTRACTION_UNAVAILABLE
    else:
        for document in version_range.documents:
            try:
                result = extractor.extract(document)
            except LLMError as exc:  # the provider failed (network, limits): skip this note
                log.error(
                    "extraction call failed",
                    extra={"fields": {"version": document.version, "error": str(exc)}},
                )
                grading.notes_failed += 1
                continue
            if result.status == EXTRACTED:
                grading.records.extend(result.records)
            else:
                grading.notes_failed += 1
        if grading.notes_failed:
            grading.extraction = EXTRACTION_PARTIAL

    old, new = version_range.old, version_range.new
    assert old is not None and new is not None  # an OK range always has both
    service = ApiDiffService.from_context(ctx)
    try:
        diff = service.diff(source, old, new)
        old_surface, new_surface = service.surface(source, old), service.surface(source, new)
    except (WheelError, SurfaceError, httpx.HTTPError) as exc:
        grading.api_diff = API_DIFF_FAILED
        grading.api_diff_error = str(exc)
        log.error(
            "api diff failed", extra={"fields": {"package": source.package, "error": str(exc)}}
        )
        return grading

    grading.old_surface = old_surface
    reconciled = reconcile(grading.records, old_surface, new_surface, diff)
    grading.changes = reconciled.changes
    grading.dropped = len(reconciled.dropped)
    return grading
