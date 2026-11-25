"""Installation events in the worker (spec 07 F7.4, F7.5, F7.20).

`installation` and `installation_repositories` events register, reactivate or
deactivate rows in `repos`. A newly active repo gets a catch-up run per watched SDK
and per API-level source (F1.9, F7.5). An inactive repo is skipped by every fan-out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from guardian.apilevel.source import API_SOURCES
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.jobs import Job, JobType
from guardian.common.models import Repo
from guardian.watch.sources import SDK_SOURCES

ACTIVATE = frozenset(
    {
        ("installation", "created"),
        ("installation", "unsuspend"),
        ("installation", "new_permissions_accepted"),
    }
)
DEACTIVATE = frozenset({("installation", "deleted"), ("installation", "suspend")})


@dataclass
class InstallationChange:
    activated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    runs_enqueued: int = 0


def _names(items: Any) -> list[str]:
    return [str(item["full_name"]) for item in items or [] if isinstance(item, dict)]


def _activate(ctx: AppContext, installation_id: int, names: Iterable[str]) -> list[str]:
    """Upsert active rows; return the repos that were not active before."""
    fresh: list[str] = []
    with session_scope(ctx.sessions) as session:
        for name in names:
            row = session.scalars(
                select(Repo).where(Repo.installation_id == installation_id, Repo.full_name == name)
            ).first()
            if row is None:
                session.add(
                    Repo(installation_id=installation_id, full_name=name, default_branch="")
                )
                fresh.append(name)
            elif not row.active:
                row.active = True
                fresh.append(name)
    return fresh


def _deactivate(ctx: AppContext, installation_id: int, names: Iterable[str] | None) -> list[str]:
    """Deactivate the named repos, or every repo of the installation when names is None."""
    gone: list[str] = []
    with session_scope(ctx.sessions) as session:
        query = select(Repo).where(Repo.installation_id == installation_id, Repo.active)
        if names is not None:
            query = query.where(Repo.full_name.in_(list(names)))
        for row in session.scalars(query):
            row.active = False
            gone.append(row.full_name)
    return gone


def catch_up(ctx: AppContext, installation_id: int, repos: Iterable[str]) -> int:
    """One run per watched SDK and per API source, for each repo (F7.5)."""
    count = 0
    for repo in repos:
        for sdk in SDK_SOURCES:
            ctx.queue.publish(
                Job(
                    JobType.RUN,
                    {
                        "repo": repo,
                        "target": f"{sdk.package}@latest",
                        "package": sdk.package,
                        "installation_id": installation_id,
                    },
                )
            )
            count += 1
        for api in API_SOURCES:
            ctx.queue.publish(
                Job(
                    JobType.RUN,
                    {
                        "repo": repo,
                        "target": f"{api.id}@latest",
                        "installation_id": installation_id,
                    },
                )
            )
            count += 1
    return count


def apply_event(ctx: AppContext, event: str, payload: dict[str, Any]) -> InstallationChange:
    change = InstallationChange()
    action = str(payload.get("action", ""))
    installation = payload.get("installation") or {}
    installation_id = int(installation.get("id", 0))
    if not installation_id:
        return change
    if event == "installation" and (event, action) in ACTIVATE:
        names = _names(payload.get("repositories"))
        change.activated = _activate(ctx, installation_id, names)
        if action == "unsuspend" and not names:
            with session_scope(ctx.sessions) as session:
                for row in session.scalars(
                    select(Repo).where(Repo.installation_id == installation_id)
                ):
                    if not row.active:
                        row.active = True
                        change.activated.append(row.full_name)
    elif event == "installation" and (event, action) in DEACTIVATE:
        change.deactivated = _deactivate(ctx, installation_id, None)
    elif event == "installation_repositories":
        change.activated = _activate(
            ctx, installation_id, _names(payload.get("repositories_added"))
        )
        change.deactivated = _deactivate(
            ctx, installation_id, _names(payload.get("repositories_removed"))
        )
    change.runs_enqueued = catch_up(ctx, installation_id, change.activated)
    return change
