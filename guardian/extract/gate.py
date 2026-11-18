"""The full release gate: release notes and the API diff together (spec 01 F1.7).

A release passes when either source shows a breaking change:

- extraction finds at least one breaking or deprecating change in the note (this is
  the only way to catch changes code cannot show: behaviour, response shapes,
  dependency swaps);
- the API diff against the previous release finds a break the note may not mention.

For an edited note, only a change in the extracted records counts: an edit does not
change the code, so the diff cannot make an edit pass again.
"""

from __future__ import annotations

from guardian.apidiff.gate import make_diff_assessor
from guardian.common.context import AppContext
from guardian.common.log import get_logger
from guardian.extract.extractor import EXTRACTED, Extractor
from guardian.pipeline import Assessment, Assessor
from guardian.watch.documents import StoredDocument

log = get_logger(__name__)


def make_release_assessor(
    ctx: AppContext,
    extractor: Extractor | None = None,
    diff_assessor: Assessor | None = None,
) -> Assessor:
    notes = extractor or Extractor(ctx.llm, ctx.sessions, ctx.blobs)
    diff = diff_assessor or make_diff_assessor(ctx)

    def assess(document: StoredDocument) -> Assessment:
        result = notes.extract(document)
        if result.status != EXTRACTED:
            # Two invalid answers from the model. The note is marked failed (F2.4) and
            # the code still gets a say, so the release is not silently dropped.
            log.error(
                "extraction failed; gating on the API diff alone",
                extra={"fields": {"package": document.package, "version": document.version}},
            )

        if document.revision > 1:
            before = notes.previous(document)
            changed = before is None or before.keys() != result.keys()
            return Assessment(breaking=result.breaking, records_changed=changed)

        if result.breaking:
            return Assessment(breaking=True)  # the note is enough; no need to fetch wheels
        return diff(document)

    return assess
