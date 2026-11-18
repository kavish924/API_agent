"""The change record an LLM extracts from a release note (spec 02 F2.1, F2.2).

Defined here ahead of the extractor because reconciliation (spec 03) consumes it.
The model reports no confidence: that is assigned per run from the API diff.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ChangeType(StrEnum):
    REMOVED = "removed"
    RENAMED = "renamed"
    SIGNATURE_CHANGED = "signature-changed"
    DEPRECATED = "deprecated"
    BEHAVIOR_CHANGED = "behavior-changed"
    RESPONSE_SHAPE_CHANGED = "response-shape-changed"


# Changes that no mechanical edit can fix; `fixable` is forced to False for them (F2.3).
NEVER_FIXABLE = frozenset({ChangeType.BEHAVIOR_CHANGED, ChangeType.RESPONSE_SHAPE_CHANGED})


class ExtractedChange(BaseModel):
    provider: str
    package: str
    version: str
    change_type: ChangeType
    qualified_symbol: str = Field(description="Dotted path, e.g. stripe.Customer.create")
    affected_parameter: str | None = None
    replacement: str | None = None
    fixable: bool = False
    evidence_quote: str
    migration_hint: str = ""

    def key(self) -> tuple[str, str, str | None, str | None]:
        """What makes two records the same change, for de-duplication and comparison."""
        return (
            self.change_type.value,
            self.qualified_symbol,
            self.affected_parameter,
            self.replacement,
        )


# --- what the model returns ------------------------------------------------------
#
# Narrower than `ExtractedChange`: provider, package and version are known from the
# document, so the model is not asked for them and could not get them wrong.


class NoteChange(BaseModel):
    change_type: ChangeType = Field(
        description=(
            "removed: a symbol, method, parameter or key no longer exists. "
            "renamed: it exists under a new name (put the new name in replacement). "
            "signature-changed: a callable's parameters changed (removed, renamed, now "
            "required, now positional-only). deprecated: still works but is scheduled to go. "
            "behavior-changed: same API, different runtime behaviour, or a dependency change "
            "that affects caller code. response-shape-changed: a returned field's type or "
            "shape changed."
        )
    )
    qualified_symbol: str = Field(
        min_length=1,
        description=(
            "Dotted path as the release note names it, including the package when the note "
            "does, e.g. stripe.Customer.create or openai.OpenAI. For a parameter or key "
            "change, the callable or params class that has it."
        ),
    )
    affected_parameter: str | None = Field(
        default=None, description="The parameter or key that changed, when the change is about one."
    )
    replacement: str | None = Field(
        default=None,
        description="What to use instead, exactly as the note names it, when it names one.",
    )
    fixable: bool = Field(
        description=(
            "True only if a mechanical code edit could fix callers (a rename, a moved import)."
        )
    )
    evidence_quote: str = Field(
        min_length=1,
        description="Copied word for word from the release note: the sentence stating this change.",
    )
    migration_hint: str = Field(
        default="", description="One sentence on how callers should update, from the note."
    )


class NoteExtraction(BaseModel):
    changes: list[NoteChange] = Field(
        default_factory=list,
        description="Every breaking or deprecating change in the note. Empty if there are none.",
    )
