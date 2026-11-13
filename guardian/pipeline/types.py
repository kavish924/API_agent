"""Request and result shapes for the pipeline entry points."""

from __future__ import annotations

from pydantic import BaseModel

# Outcome used by every stage that is not built yet. Later phases replace it.
NOT_IMPLEMENTED = "not-implemented"


class RunRequest(BaseModel):
    """One pipeline run: one repo, one target such as `openai@1.45.0`."""

    repo: str  # "owner/name" for an installed repo, or a local path for the CLI
    target: str
    package: str | None = None
    from_version: str | None = None  # overrides the pinned version read from the repo
    to_version: str | None = None  # overrides "latest release"
    head_sha: str | None = None


class RunResult(BaseModel):
    outcome: str
    reason: str | None = None
    verification_level: str | None = None
    findings: int = 0


class PollResult(BaseModel):
    outcome: str
    sources_checked: int = 0
    new_documents: int = 0
    runs_enqueued: int = 0


class EventResult(BaseModel):
    outcome: str
