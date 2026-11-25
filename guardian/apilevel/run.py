"""One API-level run for one repository (spec 10 F10.8–F10.12).

The repo is checked against every upcoming record of the source's latest snapshot
(decision 2026-11-25): a string literal passed as the record's argument (`model=`) to
a resolved SDK method **of one of the record's API families** is a finding; the same
literal passed to a method of another family, and the identifier in a non-Python
file, are possible usages, listed and never edited (F10.8, F10.9). A fixable record
gets the literal codemod; there is no version bump (F10.10).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import delete, select

from guardian.agent.workspace import DENIED_DIRS, denied
from guardian.apidiff.surface import Surface
from guardian.apilevel.families import FamilyTable, default_table
from guardian.apilevel.page import MODEL_RETIRED, ApiRecord
from guardian.apilevel.source import ApiSource, records_at
from guardian.common.context import AppContext
from guardian.common.db import SessionFactory, session_scope
from guardian.common.models import ChangeRecord, Finding, Repo, Run
from guardian.fix import MODE_AGENT, PatchSet, build_patch
from guardian.scan import rules as R
from guardian.scan.analyzer import Finding as ScanFinding
from guardian.scan.files import SKIP_DIRS
from guardian.scan.scanner import ScanResult, scan_repo
from guardian.watch.depfiles import LocalRepoFiles

# Files where a model ID may be configured; they are searched, never edited (F10.9).
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".txt")
CONFIG_NAMES = (".env.example", ".env.sample", ".env.template", "Dockerfile")
MAX_CONFIG_BYTES = 1_000_000


@dataclass(frozen=True)
class PossibleUsage:
    file: str
    line: int
    identifier: str
    why: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} `{self.identifier}` ({self.why})"


@dataclass
class ApiRun:
    records: list[ApiRecord] = field(default_factory=list)  # considered: upcoming models
    findings: list[ScanFinding] = field(default_factory=list)  # in the record's family
    finding_records: list[ApiRecord] = field(default_factory=list)  # parallel to findings
    possible: list[PossibleUsage] = field(default_factory=list)
    scan: ScanResult | None = None
    patch: PatchSet | None = None

    @property
    def earliest_shutdown(self) -> date | None:
        dates = [r.shutdown_date for r in self.finding_records if r.shutdown_date is not None]
        return min(dates) if dates else None


def upcoming(records: Sequence[ApiRecord], today: date) -> list[ApiRecord]:
    return [
        r
        for r in records
        if r.kind == MODEL_RETIRED and (r.shutdown_date is None or r.shutdown_date >= today)
    ]


def _identifier_pattern(identifier: str) -> re.Pattern[str]:
    # Whole token: `gpt-5` must not match `gpt-5-mini` or `gpt-5.1`.
    return re.compile(rf"(?<![\w.-]){re.escape(identifier)}(?![\w-]|\.\d)")


def config_usages(root: Path, records: Sequence[ApiRecord]) -> list[PossibleUsage]:
    """The identifiers in non-Python files: file and line only, never the line itself."""
    patterns = [(r.identifier, _identifier_pattern(r.identifier)) for r in records]
    found: list[PossibleUsage] = []
    root = root.resolve()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in DENIED_DIRS)
        for name in sorted(files):
            if not (name.endswith(CONFIG_SUFFIXES) or name in CONFIG_NAMES):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if denied(relative) and name not in CONFIG_NAMES:
                continue
            try:
                if path.is_symlink() or path.stat().st_size > MAX_CONFIG_BYTES:
                    continue
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for identifier, pattern in patterns:
                    if pattern.search(line):
                        found.append(PossibleUsage(relative, number, identifier, "config file"))
    return found


def run_api(
    root: Path,
    source: ApiSource,
    records: Sequence[ApiRecord],
    surface: Surface,
    *,
    table: FamilyTable | None = None,
) -> ApiRun:
    """Scan, sort findings from possible usages, and build the patch (no bump)."""
    table = table or default_table()
    result = ApiRun(records=list(records))
    queries = [R.LiteralQuery(r.identifier, r.argument or "model", None) for r in records]
    scan = scan_repo(
        root, source.module, surface, [], literals=queries, families=list(table.methods)
    )
    result.scan = scan
    for finding in scan.findings:
        if finding.query is None:
            continue
        record = records[finding.query]
        if finding.family is not None and finding.family in record.families:
            result.findings.append(finding)
            result.finding_records.append(record)
        else:
            family = finding.family or "an unknown API"
            result.possible.append(
                PossibleUsage(
                    finding.file, finding.line, record.identifier, f"passed to a {family} method"
                )
            )
    result.possible += config_usages(root, records)

    if result.findings:
        replacements = {
            i: r.replacement for i, r in enumerate(records) if r.fixable and r.replacement
        }
        reasons = {i: r.reason for i, r in enumerate(records) if not r.fixable}
        result.patch = build_patch(
            LocalRepoFiles(root),
            result.findings,
            [],
            package=source.module,
            module=source.module,
            old="",
            new="",
            mode=MODE_AGENT,
            fixer=None,
            literal_replacements=replacements,
            bump_version=False,
            report_reasons=reasons,
            tier2=False,
        )
    return result


def latest_upcoming(
    ctx: AppContext, source: ApiSource, today: date | None = None
) -> list[ApiRecord]:
    return upcoming(records_at(ctx, source), today or datetime.now(UTC).date())


def record_api_run(
    sessions: SessionFactory,
    *,
    repo: str,
    installation_id: int,
    target: str,
    head: str,
    result: ApiRun,
    outcome: str,
) -> int:
    """Store the run, its API-level change records and findings (spec 04 F4.11)."""
    with session_scope(sessions) as session:
        repo_row = session.scalars(
            select(Repo).where(Repo.installation_id == installation_id, Repo.full_name == repo)
        ).first()
        if repo_row is None:
            repo_row = Repo(installation_id=installation_id, full_name=repo, default_branch="")
            session.add(repo_row)
            session.flush()
        run = session.scalars(
            select(Run).where(
                Run.repo_id == repo_row.id, Run.target == target, Run.head_sha == head
            )
        ).first()
        if run is None:
            run = Run(repo_id=repo_row.id, target=target, head_sha=head)
            session.add(run)
            session.flush()
        run.status, run.outcome = "scanned", outcome
        run.finished_at = datetime.now(UTC)
        session.execute(delete(Finding).where(Finding.run_id == run.id))

        stored: dict[tuple[str, str], ChangeRecord] = {}
        fixes = {fix.index: fix for fix in result.patch.fixes} if result.patch else {}
        for index, (finding, record) in enumerate(
            zip(result.findings, result.finding_records, strict=True)
        ):
            row = stored.get(record.key())
            if row is None:
                candidates = session.scalars(
                    select(ChangeRecord).where(
                        ChangeRecord.layer == "api",
                        ChangeRecord.identifier == record.identifier,
                        ChangeRecord.change_type == record.kind,
                    )
                )
                row = next(
                    (
                        c
                        for c in candidates
                        if (c.replacement, c.shutdown_date, c.fixable)
                        == (record.replacement, record.shutdown_date, record.fixable)
                    ),
                    None,
                )
                if row is None:
                    row = ChangeRecord(
                        layer="api",
                        origin="page",
                        package=None,
                        version=None,
                        change_type=record.kind,
                        qualified_symbol=None,
                        identifier=record.identifier,
                        affected_parameter=record.argument,
                        replacement=record.replacement,
                        fixable=record.fixable,
                        shutdown_date=record.shutdown_date,
                        evidence_quote=record.evidence_quote,
                        migration_hint=record.reason,
                    )
                    session.add(row)
                    session.flush()
                stored[record.key()] = row
            fix = fixes.get(index)
            session.add(
                Finding(
                    run_id=run.id,
                    change_record_id=row.id,
                    file=finding.file,
                    line=finding.line,
                    matched_symbol=finding.symbol,
                    match_tier=finding.tier,
                    confidence="medium",  # no API diff confirms a page record (F10.5)
                    fix_tier=fix.tier if fix else None,
                    fix_status=fix.status if fix else None,
                )
            )
        return run.id
