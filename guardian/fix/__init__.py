"""Fix generation: bump editor, codemods, single-shot fixer, AST guard (spec 05).

`build_patch` is the entry point: one patch set with the version bump and every fix
that passed, plus a per-finding result. The agent fixer (spec 11) plugs in as a
`Tier2Fixer`.
"""

from guardian.fix.guard import FileChange, GuardResult, check_patch
from guardian.fix.patch import (
    COMPLETE,
    COULD_NOT_BUMP,
    NOTHING_FIXED,
    PARTIAL,
    PatchSet,
    build_patch,
)
from guardian.fix.single_shot import SingleShotFixer
from guardian.fix.types import (
    FIX_FAILED,
    FIXED,
    MODE_AGENT,
    MODE_SINGLE_SHOT,
    MODES,
    REPORT_ONLY,
    Tier2Fixer,
)

__all__ = [
    "COMPLETE",
    "COULD_NOT_BUMP",
    "FIXED",
    "FIX_FAILED",
    "MODES",
    "MODE_AGENT",
    "MODE_SINGLE_SHOT",
    "NOTHING_FIXED",
    "PARTIAL",
    "REPORT_ONLY",
    "FileChange",
    "GuardResult",
    "PatchSet",
    "SingleShotFixer",
    "Tier2Fixer",
    "build_patch",
    "check_patch",
]
