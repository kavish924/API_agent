"""Request and result shapes for the pipeline entry points."""

from __future__ import annotations

from pydantic import BaseModel

# Outcome used by every stage that is not built yet. Later phases replace it.
NOT_IMPLEMENTED = "not-implemented"
# The repository does not use anything that changed: the run ends quietly (spec 04 F4.8).
NO_FINDINGS = "no-findings"

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
    fix_mode: str | None = None  # overrides the `fix_mode` setting (spec 05 F5.5)


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
    # Grading (specs 02 and 03).
    extraction: str | None = None  # done | partial | unavailable
    notes_failed: int = 0
    records: int = 0  # changes extracted from the notes
    records_dropped: int = 0  # about symbols that never existed in the old version
    api_diff: str | None = None  # done | failed
    changes: int = 0  # graded changes, from the notes and the diff together
    changes_high: int = 0
    changes_fixable: int = 0
    changes_from_diff: int = 0  # breaks no release note mentions
    change_types: dict[str, int] = {}
    verification_level: str | None = None  # L0 | L1 | L2 (interim static; L3 with spec 06)
    verification: dict[str, list[str]] = {}  # errors, remaining work and notes, by kind
    # Scanning (spec 04).
    run_id: int | None = None
    findings: int = 0
    finding_sites: int = 0  # distinct places in the code
    findings_by_tier: dict[str, int] = {}
    files_scanned: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    scan_seconds: float = 0.0
    # Fix generation (spec 05).
    patch_status: str | None = None  # complete | partial | nothing-fixed | could-not-bump
    fix_mode: str | None = None
    fixed: int = 0
    report_only: int = 0
    fix_failed: int = 0
    patch_files: list[str] = []
    lock_files: list[str] = []  # regenerated in the sandbox install phase (spec 06)
    bump_problems: list[str] = []
    patch_ref: str | None = None  # the unified diff, in the blob store
    fix_cost: float = 0.0
    # API-level runs (spec 10).
    api_source: str | None = None
    api_records: int = 0  # upcoming records the repo was checked against
    possible_usages: list[str] = []  # other-family calls and config files, never edited
    shutdown_date: str | None = None  # the earliest shutdown among the findings
    behaviour_change: bool = False  # a model swap: output, cost and limits may differ
    # Delivery to GitHub (spec 07): pr-opened, pr-updated, issue-opened, issue-updated,
    # declined, skipped-by-config or paused. None for local CLI runs.
    delivery: str | None = None
    delivery_number: int | None = None
    delivery_url: str | None = None


class PollResult(BaseModel):
    outcome: str
    sources_checked: int = 0
    sources_unchanged: int = 0
    baselined: int = 0
    new_documents: int = 0
    edited_documents: int = 0
    gaps: int = 0
    gate: str = GATE_NOT_NEEDED
    assessed: int = 0  # notes the gate decided in this poll
    awaiting_gate: int = 0  # notes still waiting (no gate yet, or it failed)
    runs_enqueued: int = 0
    errors: list[str] = []
    api_pages_checked: int = 0
    api_pages_unchanged: int = 0
    api_records_new: int = 0


class EventResult(BaseModel):
    outcome: str
    activated: list[str] = []
    deactivated: list[str] = []
    runs_enqueued: int = 0
