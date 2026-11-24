"""Write a scan's findings to the database before anything else happens (spec 04 F4.11).

A run is keyed on (repo, target, head commit), so scanning the same code again
replaces that run's findings instead of adding to them. Each finding points at a
change record: the change as graded for this version range, stored once.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select

from guardian.apidiff.reconcile import GradedChange
from guardian.common.db import SessionFactory, session_scope
from guardian.common.models import ChangeRecord, Finding, Repo, Run
from guardian.scan.analyzer import Finding as ScanFinding
from guardian.scan.scanner import ScanResult

LOCAL_INSTALLATION = 0  # local checkouts scanned from the CLI have no GitHub installation
WORKING_TREE = "working-tree"


def local_identity(root: Path) -> tuple[str, str]:
    """(full name, head) for a local checkout. Head is the commit, marked dirty if edited."""
    name = f"local:{root.resolve().as_posix()}"[:255]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return name, WORKING_TREE
    return name, f"{head}-dirty" if dirty else head


def _change_record(
    session: object, package: str, versions: str, change: GradedChange
) -> ChangeRecord:
    query = select(ChangeRecord).where(
        ChangeRecord.document_id.is_(None),
        ChangeRecord.package == package,
        ChangeRecord.version == versions,
        ChangeRecord.origin == change.origin,
        ChangeRecord.change_type == change.change_type.value,
        ChangeRecord.qualified_symbol == change.symbol,
        ChangeRecord.affected_parameter.is_(None)
        if change.affected_parameter is None
        else ChangeRecord.affected_parameter == change.affected_parameter,
    )
    existing = session.scalars(query.limit(1)).first()  # type: ignore[attr-defined]
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    record = ChangeRecord(
        document_id=None,
        layer="sdk",
        origin=change.origin,
        package=package,
        version=versions,
        change_type=change.change_type.value,
        qualified_symbol=change.symbol,
        affected_parameter=change.affected_parameter,
        replacement=change.replacement,
        fixable=change.fixable,
        evidence_quote=change.evidence_quote,
        migration_hint=change.migration_hint,
    )
    session.add(record)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    return record


def record_scan(
    sessions: SessionFactory,
    *,
    repo: str,
    installation_id: int,
    target: str,
    head: str,
    package: str,
    from_version: str,
    to_version: str,
    changes: Sequence[GradedChange],
    result: ScanResult,
    outcome: str,
) -> int:
    """Store the run and its findings; return the run's id."""
    versions = f"{from_version}..{to_version}"
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
        run.from_version, run.to_version = from_version, to_version
        run.status, run.outcome = "scanned", outcome
        run.finished_at = datetime.now(UTC)
        session.execute(delete(Finding).where(Finding.run_id == run.id))

        records: dict[int, ChangeRecord] = {}
        for finding in result.findings:
            if finding.change is None:
                continue  # literal queries are stored with their API-level records (spec 10)
            if finding.change not in records:
                records[finding.change] = _change_record(
                    session, package, versions, changes[finding.change]
                )
            session.add(
                Finding(
                    run_id=run.id,
                    change_record_id=records[finding.change].id,
                    file=finding.file,
                    line=finding.line,
                    matched_symbol=finding.symbol,
                    match_tier=finding.tier,
                    confidence=finding.confidence,
                )
            )
        return run.id


def record_fixes(
    sessions: SessionFactory,
    run_id: int,
    findings: Sequence[ScanFinding],
    fixes: Sequence[tuple[int, str, str | None]],
) -> None:
    """Store each finding's fix result (spec 05 F5.12) on the rows `record_scan` wrote.

    `fixes` holds (finding index, status, tier). Rows were written in finding order,
    skipping literal-query findings, so they are matched up in that order.
    """
    by_index = {index: (status, tier) for index, status, tier in fixes}
    stored = [index for index, finding in enumerate(findings) if finding.change is not None]
    with session_scope(sessions) as session:
        rows = list(
            session.scalars(select(Finding).where(Finding.run_id == run_id).order_by(Finding.id))
        )
        for row, index in zip(rows, stored, strict=False):
            row.fix_status, row.fix_tier = by_index.get(index, (None, None))
        run = session.get(Run, run_id)
        if run is not None:
            run.status = "fixed"
