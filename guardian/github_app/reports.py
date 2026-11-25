"""Turn a run's findings, changes and fixes into a delivery `Report` (spec 07 F7.8)."""

from __future__ import annotations

from collections.abc import Sequence

from guardian.apidiff.reconcile import FROM_NOTES, GradedChange
from guardian.apilevel.page import ApiRecord
from guardian.apilevel.run import ApiRun
from guardian.apilevel.source import ApiSource
from guardian.fix import PatchSet
from guardian.github_app.render import API, SDK, Report, ReportItem
from guardian.scan.analyzer import Finding
from guardian.verify.static import Verification
from guardian.watch.sources import SdkSource


def describe_change(change: GradedChange) -> str:
    text = f"{change.change_type.value} `{change.public_symbol}`"
    if change.affected_parameter:
        text += f": `{change.affected_parameter}`"
    if change.replacement:
        text += f" → `{change.replacement}`"
    return text


def change_key(package: str, change: GradedChange) -> str:
    return ":".join(
        [
            "sdk",
            package,
            change.change_type.value,
            change.symbol,
            change.affected_parameter or "",
            change.replacement or "",
        ]
    )


def sdk_report(
    source: SdkSource,
    old: str,
    new: str,
    findings: Sequence[Finding],
    changes: Sequence[GradedChange],
    patch: PatchSet,
    verification: Verification | None,
) -> Report:
    fixes = {fix.index: fix for fix in patch.fixes}
    release = f"https://github.com/{source.repo}/releases/tag/{source.tag_prefix}{new}"
    items: list[ReportItem] = []
    for index, finding in enumerate(findings):
        if finding.change is None:
            continue
        change = changes[finding.change]
        fix = fixes.get(index)
        items.append(
            ReportItem(
                file=finding.file,
                line=finding.line,
                code=finding.code,
                change=describe_change(change),
                change_key=change_key(source.package, change),
                evidence=change.evidence_quote,
                link=release if change.origin == FROM_NOTES else None,
                confidence=finding.confidence,
                status=fix.status if fix else "fix-failed",
                tier=fix.tier if fix else None,
                reason=fix.reason if fix else "",
            )
        )
    return Report(SDK, source.package, source.module, old, new, items, patch, verification)


def _api_change(record: ApiRecord) -> str:
    text = f"`{record.identifier}` retires {record.shutdown_date}"
    if record.replacement:
        text += f" → `{record.replacement}`"
    return text


def api_report(
    source: ApiSource, run: ApiRun, patch: PatchSet, verification: Verification | None
) -> Report:
    fixes = {fix.index: fix for fix in patch.fixes}
    link = source.url.removesuffix(".md")
    items = [
        ReportItem(
            file=finding.file,
            line=finding.line,
            code=finding.code,
            change=_api_change(record),
            change_key=f"api:{source.id}:{record.identifier}:{record.replacement or ''}",
            evidence=record.evidence_quote,
            link=link,
            confidence="medium",  # no API diff confirms a page record (F10.5)
            status=fixes[index].status if index in fixes else "fix-failed",
            tier=fixes[index].tier if index in fixes else None,
            reason=fixes[index].reason if index in fixes else "",
        )
        for index, (finding, record) in enumerate(
            zip(run.findings, run.finding_records, strict=True)
        )
    ]
    shutdown = run.earliest_shutdown
    return Report(
        API,
        source.id,
        source.module,
        None,
        None,
        items,
        patch,
        verification,
        possible_usages=[str(p) for p in run.possible],
        shutdown_date=shutdown.isoformat() if shutdown else None,
    )
