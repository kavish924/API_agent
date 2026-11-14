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
