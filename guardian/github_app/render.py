"""Titles and descriptions for upgrade PRs and issues (spec 07 F7.8, F7.9, F7.12, F7.13;
spec 10 F10.12, F10.13).

Every PR and issue carries a hidden marker naming its subject, so it can be found and
edited later. Text from release notes and provider pages is quoted, never followed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from guardian.fix import FIXED, REPORT_ONLY, PatchSet
from guardian.verify.static import Verification

SDK = "sdk"
API = "api"

BEHAVIOUR_CHANGE = (
    "**Behaviour change.** Swapping a model changes output, cost and limits. No check "
    "here verifies the new model's output: review and test it before merging."
)
FOOTER = (
    "_This is a draft opened by API Change Guardian. It never merges anything and never "
    "pushes to the default branch; a person reviews and merges._"
)


def marker(subject: str) -> str:
    return f"<!-- api-change-guardian:{subject} -->"


@dataclass(frozen=True)
class ReportItem:
    file: str
    line: int
    code: str
    change: str  # one line naming what changed, in Markdown
    change_key: str  # stable across runs: what closed-PR memory compares
    evidence: str | None
    link: str | None
    confidence: str
    status: str  # fixed | report-only | fix-failed
    tier: str | None = None
    reason: str = ""


@dataclass
class Report:
    kind: str  # SDK | API
    subject: str  # the package, or the API source id
    package: str  # the SDK the code imports
    old: str | None
    new: str | None
    items: list[ReportItem]
    patch: PatchSet | None
    verification: Verification | None
    possible_usages: list[str] = field(default_factory=list)
    shutdown_date: str | None = None
    related_prs: list[str] = field(default_factory=list)  # Dependabot / Renovate (F7.14)

    @property
    def fixed(self) -> int:
        return sum(item.status == FIXED for item in self.items)

    @property
    def partial(self) -> bool:
        return 0 < self.fixed < len(self.items)


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pr_title(report: Report) -> str:
    partial = " (partial)" if report.partial else ""
    if report.kind == API:
        return f"[behaviour change] Replace OpenAI models retiring {report.shutdown_date}{partial}"
    return f"Upgrade {report.package} {report.old} → {report.new}{partial}"


def issue_title(report: Report) -> str:
    if report.kind == API:
        return f"OpenAI models used here retire on {report.shutdown_date}"
    return f"{report.package} {report.old} → {report.new}: code needs changes"


def _changes_table(report: Report) -> list[str]:
    by_change: dict[str, list[ReportItem]] = defaultdict(list)
    for item in report.items:
        by_change[item.change_key].append(item)
    lines = [
        "| Change | Source | Confidence | Where | Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for items in by_change.values():
        first = items[0]
        source = f"[notice]({first.link})" if first.link else "API diff"
        if first.evidence:
            source += f": “{_cell(first.evidence, 120)}”"
        where = ", ".join(f"`{i.file}:{i.line}`" for i in items[:6])
        if len(items) > 6:
            where += f" and {len(items) - 6} more"
        results = sorted({_result(i) for i in items})
        lines.append(
            f"| {_cell(first.change)} | {source} | {first.confidence} | {where} | "
            f"{'; '.join(results)} |"
        )
    return lines


def _result(item: ReportItem) -> str:
    if item.status == FIXED:
        return f"fixed ({item.tier or 'codemod'})"
    if item.status == REPORT_ONLY:
        return "needs a person" + (f": {_cell(item.reason, 80)}" if item.reason else "")
    return "fix failed" + (f": {_cell(item.reason, 80)}" if item.reason else "")


def _verification(report: Report) -> list[str]:
    v = report.verification
    if v is None:
        return ["**Verification:** not run (nothing was fixed)."]
    lines = [
        f"**Verification: {v.level}** (static; L1 = compiles and lints, L2 = type-checks "
        "against the new version)."
    ]
    for note in v.notes:
        lines.append(f"- {note}")
    for title, errors in (
        ("Compile errors", v.compile_errors),
        ("Lint errors", v.lint_errors),
        ("New type errors", v.type_errors),
    ):
        if errors:
            lines.append(f"- {title}:")
            lines += [f"  - `{_cell(e, 200)}`" for e in errors[:10]]
    return lines


def _remaining(report: Report) -> list[str]:
    left = [i for i in report.items if i.status != FIXED]
    lines: list[str] = []
    if left:
        lines.append("### Left for a person")
        lines += [f"- `{i.file}:{i.line}` `{_cell(i.code, 100)}`: {_result(i)}" for i in left]
    if report.verification and report.verification.remaining:
        lines.append("")
        lines.append("Type errors at those call sites:")
        lines += [f"- `{_cell(e, 200)}`" for e in report.verification.remaining[:10]]
    if report.possible_usages:
        lines.append("")
        lines.append("### Possible usages (not edited)")
        lines.append("The identifier also appears where a static scan cannot tell if it is used:")
        lines += [f"- {_cell(u, 200)}" for u in report.possible_usages[:20]]
    return lines


def pr_body(report: Report) -> str:
    lines = [marker(report.subject), ""]
    if report.kind == API:
        lines += [BEHAVIOUR_CHANGE, "", f"Shutdown date: **{report.shutdown_date}**.", ""]
        lines.append("This PR replaces model IDs that OpenAI retires. It changes no dependency.")
    else:
        lines.append(
            f"This PR upgrades `{report.package}` from {report.old} to {report.new} and "
            "fixes the code the upgrade breaks, in one commit."
        )
        if report.patch and report.patch.lock_files:
            lock = ", ".join(f"`{f}`" for f in report.patch.lock_files)
            lines.append(f"Regenerate {lock} after merging (the sandbox step is not built).")
    if report.partial:
        lines += ["", "**Partial:** some call sites still need a person; see below."]
    if report.related_prs:
        lines += ["", "Related dependency PRs: " + ", ".join(report.related_prs)]
    lines += ["", "### Changes", *_changes_table(report), "", *_verification(report), ""]
    lines += _remaining(report)
    lines += ["", FOOTER]
    return "\n".join(lines).strip() + "\n"


def issue_body(report: Report, *, diff: str | None = None, why: str = "") -> str:
    lines = [marker(report.subject), ""]
    if report.kind == API:
        lines += [BEHAVIOUR_CHANGE, "", f"Shutdown date: **{report.shutdown_date}**.", ""]
    if why:
        lines += [why, ""]
    lines += ["### Changes", *_changes_table(report), ""]
    if report.verification is not None:
        lines += [*_verification(report), ""]
    lines += _remaining(report)
    if diff:
        lines += [
            "",
            "### Proposed patch",
            "Apply it yourself after review:",
            "",
            "```diff",
            diff.rstrip("\n"),
            "```",
        ]
    lines += ["", FOOTER.replace("This is a draft opened", "Opened")]
    return "\n".join(lines).strip() + "\n"
