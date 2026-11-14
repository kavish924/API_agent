"""Request and result shapes for the pipeline entry points."""

from __future__ import annotations

from pydantic import BaseModel

# Outcome used by every stage that is not built yet. Later phases replace it.
NOT_IMPLEMENTED = "not-implemented"

# Poll outcomes.
POLL_OK = "ok"
POLL_PARTIAL = "partial"  # at least one source failed, at least one worked
POLL_FAILED = "failed"  # every source failed

# How the release gate was applied to this poll's documents.
GATE_APPLIED = "applied"
GATE_API_DIFF_ONLY = "api-diff-only"  # the diff ran; extraction (spec 02) is not built yet
GATE_NOT_NEEDED = "not-needed"  # nothing new to assess
GATE_UNAVAILABLE = "unavailable"  # extraction and the API diff are not built yet


class RunRequest(BaseModel):
    """One pipeline run: one repo, one target such as `openai@1.45.0`."""

    repo: str  # "owner/name" for an installed repo, or a local path for the CLI
    target: str
    package: str | None = None
    from_version: str | None = None  # overrides the pinned version read from the repo
    to_version: str | None = None  # overrides "latest release"
    head_sha: str | None = None
    installation_id: int | None = None


class RunResult(BaseModel):
    outcome: str
    reason: str | None = None
    package: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    pinned_in: str | None = None  # the dependency file the old version came from
    notes: int = 0  # release notes in the range
    notes_stored: int = 0  # of those, how many this run had to store
    gaps: int = 0  # versions with no usable note
    verification_level: str | None = None
    findings: int = 0


class PollResult(BaseModel):
    outcome: str
    sources_checked: int = 0
    sources_unchanged: int = 0
    baselined: int = 0
    new_documents: int = 0
    edited_documents: int = 0
    gaps: int = 0
    gate: str = GATE_NOT_NEEDED
    runs_enqueued: int = 0
    errors: list[str] = []


class EventResult(BaseModel):
    outcome: str
