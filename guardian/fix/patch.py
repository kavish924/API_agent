"""Patch assembly: bump, codemods and Tier 2 fixes in one patch set (spec 05 F5.11–F5.14).

Order of work for one run:

1. the bump editor rewrites the pinned version (F5.1); lock files are listed for the
   sandbox to regenerate (F5.2);
2. findings whose change is not fixable become report-only (F5.6);
3. codemods fix what they can, file by file (F5.3);
4. the rest goes to Tier 2 (agent or single-shot, F5.4, F5.5), one region at a time,
   bottom-up so earlier line numbers stay valid; every proposal passes the AST guard
   or is dropped whole (F5.7–F5.10).

All edits are made to the files as they were scanned (F5.11). The result records, per
finding, `fixed`, `report-only` or `fix-failed`, and every attempt (F5.12, F5.14).
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from guardian.apidiff.reconcile import GradedChange
from guardian.common.log import get_logger
from guardian.fix.bump import BumpProblem, BumpResult, bump
from guardian.fix.codemods import apply_codemods
from guardian.fix.guard import DEFAULT_LINE_CAP, FileChange, check_patch
from guardian.fix.types import (
    FIX_FAILED,
    FIXED,
    MODE_AGENT,
    REPORT_ONLY,
    TIER_CODEMOD,
    Attempt,
    FindingFix,
    FixGroup,
    FixItem,
    Tier2Fixer,
)
from guardian.scan.analyzer import Finding
from guardian.watch.depfiles import RepoFiles

log = get_logger(__name__)

# Patch set status.
COMPLETE = "complete"  # every finding fixed
PARTIAL = "partial"  # some fixed, some report-only or failed (F5.13)
NOTHING_FIXED = "nothing-fixed"  # no finding fixed: the run takes the issue path
COULD_NOT_BUMP = "could-not-bump"  # the version could not be changed: issue path

AGENT_NOT_BUILT = "the agent fixer is not built yet (spec 11)"


@dataclass
class PatchSet:
    package: str
    old: str
    new: str
    files: dict[str, tuple[str, str]] = field(default_factory=dict)  # path -> (before, after)
    lock_files: list[str] = field(default_factory=list)
    bump_problems: list[BumpProblem] = field(default_factory=list)
    fixes: list[FindingFix] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for fix in self.fixes if fix.status == status)

    @property
    def status(self) -> str:
        if self.bump_problems:
            return COULD_NOT_BUMP
        fixed = self.count(FIXED)
        if not fixed:
            return NOTHING_FIXED
        return COMPLETE if fixed == len(self.fixes) else PARTIAL

    @property
    def partial(self) -> bool:
        return self.status == PARTIAL

    @property
    def cost(self) -> float:
        return sum(attempt.cost for attempt in self.attempts)

    def diff(self) -> str:
        """The whole patch as one unified diff, in path order."""
        parts: list[str] = []
        for path, (before, after) in sorted(self.files.items()):
            for line in difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ):
                parts.append(line if line.endswith("\n") else line + "\n")
        return "".join(parts)


def region_of(source: str, line: int) -> tuple[int, int]:
    """Lines of the innermost function holding `line`, or of its top-level statement."""
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return line, line
    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(PositionProvider)
    best: tuple[int, int] | None = None
    for node, position in positions.items():
        if not isinstance(node, cst.FunctionDef):
            continue
        start = position.start.line
        if node.decorators:
            start = positions[node.decorators[0]].start.line
        end = position.end.line
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end)
    if best is not None:
        return best
    for statement in module.body:
        position = positions[statement]
        if position.start.line <= line <= position.end.line:
            return position.start.line, position.end.line
    return line, line


def _groups(
    path: str,
    source: str,
    items: Sequence[FixItem],
    *,
    package: str,
    old: str,
    new: str,
) -> list[FixGroup]:
    by_region: dict[tuple[int, int], list[FixItem]] = {}
    for item in items:
        by_region.setdefault(region_of(source, item.finding.line), []).append(item)
    groups = [
        FixGroup(path, source, start, end, tuple(found), package, old, new)
        for (start, end), found in by_region.items()
    ]
    return sorted(groups, key=lambda g: -g.start)  # bottom-up


def build_patch(
    files: RepoFiles,
    findings: Sequence[Finding],
    changes: Sequence[GradedChange],
    *,
    package: str,
    module: str,
    old: str,
    new: str,
    mode: str = MODE_AGENT,
    fixer: Tier2Fixer | None = None,
    literal_replacements: Mapping[int, str] | None = None,
    bump_version: bool = True,
    line_cap: int = DEFAULT_LINE_CAP,
    report_reasons: Mapping[int, str] | None = None,
    tier2: bool = True,
) -> PatchSet:
    """Build one patch set for a run's findings.

    `package` is the distribution name (for the bump), `module` the import name (for
    the guard). `fixer` is the Tier 2 fixer for `mode`; without one, findings no
    codemod handles fail with the reason. API-level runs pass `bump_version=False`
    (spec 10 F10.10), `report_reasons` (literal query -> why it is report-only) and
    `tier2=False`: a model swap is a literal codemod or nothing.
    """
    patch = PatchSet(package=package, old=old, new=new)
    literals = literal_replacements or {}

    if bump_version:
        bumped: BumpResult = bump(files, package, old, new)
        patch.lock_files = bumped.lock_files
        patch.bump_problems = bumped.problems
        patch.files.update(bumped.edits)

    # Report-only and unfixable findings first (F5.6).
    fixable: dict[str, list[FixItem]] = {}
    for index, finding in enumerate(findings):
        change = changes[finding.change] if finding.change is not None else None
        replacement = literals.get(finding.query) if finding.query is not None else None
        if change is not None and not change.fixable:
            patch.fixes.append(FindingFix(index, REPORT_ONLY, reason=change.change_type.value))
            continue
        if change is None and replacement is None:
            reasons = report_reasons or {}
            reason = (
                reasons.get(finding.query, "no replacement")
                if finding.query is not None
                else "no replacement"
            )
            patch.fixes.append(FindingFix(index, REPORT_ONLY, reason=reason))
            continue
        fixable.setdefault(finding.file, []).append(FixItem(index, finding, change, replacement))

    if patch.bump_problems:
        for items in fixable.values():
            for item in items:
                patch.fixes.append(FindingFix(item.index, FIX_FAILED, reason="no version bump"))
        patch.files.clear()
        return patch

    for path, items in sorted(fixable.items()):
        original = files.read_text(path)
        if original is None:
            for item in items:
                patch.fixes.append(FindingFix(item.index, FIX_FAILED, reason="file not found"))
            continue
        current = _codemods(patch, path, original, items, changes, literals)
        remaining = [item for item in items if not _decided(patch, item.index)]
        if remaining and not tier2:
            for item in remaining:
                patch.fixes.append(
                    FindingFix(item.index, FIX_FAILED, reason="no codemod fits this code")
                )
        elif remaining:
            current = _tier2(
                patch,
                path,
                current,
                remaining,
                mode=mode,
                fixer=fixer,
                module=module,
                line_cap=line_cap,
            )
        if current != original:
            patch.files[path] = (original, current)

    patch.fixes.sort(key=lambda fix: fix.index)
    log.info(
        "patch built",
        extra={
            "fields": {
                "status": patch.status,
                "fixed": patch.count(FIXED),
                "report_only": patch.count(REPORT_ONLY),
                "fix_failed": patch.count(FIX_FAILED),
                "files": len(patch.files),
            }
        },
    )
    return patch


def _decided(patch: PatchSet, index: int) -> bool:
    return any(fix.index == index for fix in patch.fixes)


def _codemods(
    patch: PatchSet,
    path: str,
    source: str,
    items: Sequence[FixItem],
    changes: Sequence[GradedChange],
    literals: Mapping[int, str],
) -> str:
    local_literals = {
        item.finding.query: item.replacement
        for item in items
        if item.finding.query is not None and item.replacement is not None
    }
    result = apply_codemods(
        source,
        [item.finding for item in items],
        changes,
        literal_replacements=local_literals or literals,
    )
    if not result.changed:
        return source
    if result.text.count("\n") != source.count("\n"):
        # Codemods never add or remove lines; if one did, trust none of them.
        log.error("codemod changed the line count", extra={"fields": {"file": path}})
        return source
    for local in sorted(result.handled):
        patch.fixes.append(FindingFix(items[local].index, FIXED, tier=TIER_CODEMOD))
    covered = tuple(items[local].index for local in sorted(result.handled))
    patch.attempts.append(
        Attempt(
            TIER_CODEMOD, path, covered, accepted=True, diff=_unified(path, source, result.text)
        )
    )
    return result.text


def _tier2(
    patch: PatchSet,
    path: str,
    source: str,
    items: Sequence[FixItem],
    *,
    mode: str,
    fixer: Tier2Fixer | None,
    module: str,
    line_cap: int,
) -> str:
    if fixer is None:
        reason = AGENT_NOT_BUILT if mode == MODE_AGENT else f"no {mode} fixer configured"
        for item in items:
            patch.fixes.append(FindingFix(item.index, FIX_FAILED, tier=mode, reason=reason))
        return source

    current = source
    for group in _groups(path, current, items, package=patch.package, old=patch.old, new=patch.new):
        group = FixGroup(
            group.path,
            current,
            group.start,
            group.end,
            group.items,
            group.package,
            group.old,
            group.new,
        )
        indexes = tuple(item.index for item in group.items)
        proposal = fixer.propose(group)
        if proposal.after is None:
            patch.attempts.append(
                Attempt(
                    fixer.name,
                    path,
                    indexes,
                    accepted=False,
                    reasons=[proposal.reason],
                    cost=proposal.cost,
                )
            )
            status = proposal.give_up_status
            for item in group.items:
                patch.fixes.append(
                    FindingFix(item.index, status, tier=fixer.name, reason=proposal.reason)
                )
            continue
        verdict = check_patch(
            [FileChange(path, current, proposal.after)],
            {path: group.lines},
            package=module,
            findings=len(group.items),
            line_cap=line_cap,
        )
        patch.attempts.append(
            Attempt(
                fixer.name,
                path,
                indexes,
                accepted=verdict.accepted,
                diff=verdict.diff,
                reasons=verdict.reasons,
                cost=proposal.cost,
            )
        )
        if not verdict.accepted:
            reason = "rejected by the AST guard: " + "; ".join(verdict.reasons)
            for item in group.items:
                patch.fixes.append(
                    FindingFix(item.index, FIX_FAILED, tier=fixer.name, reason=reason)
                )
            continue
        current = proposal.after
        for item in group.items:
            patch.fixes.append(FindingFix(item.index, FIXED, tier=fixer.name))
    return current


def _unified(path: str, before: str, after: str) -> str:
    return "".join(
        line if line.endswith("\n") else line + "\n"
        for line in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
