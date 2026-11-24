"""Shapes shared by the fix tiers (spec 05) and, later, the agent fixer (spec 11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from guardian.apidiff.reconcile import GradedChange
from guardian.scan.analyzer import Finding

# Per-finding result (F5.12).
FIXED = "fixed"
REPORT_ONLY = "report-only"
FIX_FAILED = "fix-failed"

# Tiers (F5.3, F5.4).
TIER_CODEMOD = "codemod"
TIER_SINGLE_SHOT = "single-shot"
TIER_AGENT = "agent"

# Tier 2 mode setting (F5.5).
MODE_AGENT = "agent"
MODE_SINGLE_SHOT = "single-shot"
MODES = (MODE_AGENT, MODE_SINGLE_SHOT)


@dataclass(frozen=True)
class FixItem:
    index: int  # into the run's findings
    finding: Finding
    change: GradedChange | None  # None for a literal query (spec 10)
    replacement: str | None = None  # a literal query's replacement value


@dataclass(frozen=True)
class FixGroup:
    """Findings that share one code region, fixed together by one Tier 2 call."""

    path: str
    source: str  # the file as it is now (after codemods and earlier accepted fixes)
    start: int  # region lines, 1-based and inclusive
    end: int
    items: tuple[FixItem, ...]
    package: str  # distribution name, for the prompt
    old: str
    new: str

    @property
    def lines(self) -> list[int]:
        return [item.finding.line for item in self.items]


@dataclass(frozen=True)
class Proposal:
    after: str | None  # the whole file, rewritten; None when the fixer gave up
    reason: str = ""
    cost: float = 0.0


class Tier2Fixer(Protocol):
    name: str

    def propose(self, group: FixGroup, feedback: str | None = None) -> Proposal:
        """Rewrite the group's region. `feedback` is a previous attempt's rejection."""
        ...


@dataclass
class Attempt:
    """One proposed change and what the guard said about it (F5.14)."""

    tier: str
    path: str
    findings: tuple[int, ...]
    accepted: bool
    diff: str = ""
    reasons: list[str] = field(default_factory=list)
    cost: float = 0.0


@dataclass
class FindingFix:
    index: int
    status: str  # FIXED | REPORT_ONLY | FIX_FAILED
    tier: str | None = None
    reason: str = ""
