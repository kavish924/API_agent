"""Grade extracted change records against the API diff (spec 03 F3.4, F3.7–F3.10).

The release notes say what the provider claims changed; the diff says what the code
shows. Reconciliation joins them:

- a record about a symbol that never existed in the old version is dropped;
- a record the diff confirms is `high` confidence, one it is silent on is `medium`
  (silence never removes a record: the diff cannot see response shapes or behaviour);
- a break in the diff that no note mentions becomes a record of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from guardian.apidiff.compare import (
    KEY_REMOVED,
    KEY_REQUIRED,
    KIND_CHANGED,
    KWARG_REMOVED,
    KWARG_REQUIRED,
    METHOD_REMOVED,
    MODULE_REMOVED,
    NAME_REMOVED,
    PARAM_KIND,
    PARAM_REMOVED,
    PARAM_RENAMED,
    PARAM_REQUIRED,
    ApiDiff,
    Change,
)
from guardian.apidiff.surface import CLASS, FUNCTION, Surface
from guardian.extract.schema import NEVER_FIXABLE, ChangeType, ExtractedChange

HIGH, MEDIUM = "high", "medium"
FROM_NOTES, FROM_DIFF = "notes", "diff"

SYMBOL_NOT_IN_OLD_VERSION = "symbol-not-in-old-version"

_REMOVAL_KINDS = {NAME_REMOVED, METHOD_REMOVED, MODULE_REMOVED}

# How a diff entry reads as a change type when no release note describes it.
_DIFF_TYPE = {
    NAME_REMOVED: ChangeType.REMOVED,
    METHOD_REMOVED: ChangeType.REMOVED,
    MODULE_REMOVED: ChangeType.REMOVED,
    KIND_CHANGED: ChangeType.SIGNATURE_CHANGED,
    PARAM_REMOVED: ChangeType.SIGNATURE_CHANGED,
    PARAM_RENAMED: ChangeType.SIGNATURE_CHANGED,
    PARAM_REQUIRED: ChangeType.SIGNATURE_CHANGED,
    PARAM_KIND: ChangeType.SIGNATURE_CHANGED,
    KEY_REMOVED: ChangeType.SIGNATURE_CHANGED,
    KEY_REQUIRED: ChangeType.SIGNATURE_CHANGED,
    KWARG_REMOVED: ChangeType.SIGNATURE_CHANGED,
    KWARG_REQUIRED: ChangeType.SIGNATURE_CHANGED,
}

# Diff-only changes a deterministic codemod can fix (F3.9).
_CODEMOD_KINDS = {PARAM_RENAMED, PARAM_KIND}


@dataclass(frozen=True)
class GradedChange:
    """One change for the scanner: what changed, how sure we are, and where it came from."""

    origin: str  # FROM_NOTES | FROM_DIFF
    change_type: ChangeType
    symbol: str  # canonical, in the old version
    public_symbol: str  # how a person would write it
    confidence: str  # HIGH | MEDIUM
    fixable: bool
    affected_parameter: str | None = None
    replacement: str | None = None
    replacement_verified: bool = False  # the replacement exists in the new version
    evidence_quote: str | None = None
    migration_hint: str = ""
    diff_kinds: tuple[str, ...] = ()  # the diff entries that confirm it


@dataclass(frozen=True)
class Dropped:
    record: ExtractedChange
    reason: str


@dataclass
class Reconciliation:
    changes: list[GradedChange] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)


# What the diff is able to confirm. It sees names and signatures, so it can never
# confirm a deprecation, a behaviour change or a response-shape change.
_CONFIRMABLE = {ChangeType.REMOVED, ChangeType.RENAMED, ChangeType.SIGNATURE_CHANGED}


def _matches(change: Change, record: ExtractedChange, symbol: str) -> bool:
    """Does this diff entry confirm this record?"""
    if change.symbol != symbol or record.change_type not in _CONFIRMABLE:
        return False
    if record.affected_parameter is not None:
        # A claim about one parameter: the same parameter, or the whole symbol going away.
        return change.param == record.affected_parameter or change.kind in _REMOVAL_KINDS
    if record.change_type == ChangeType.SIGNATURE_CHANGED:
        return change.param is not None or change.kind == KIND_CHANGED
    return change.kind in _REMOVAL_KINDS or change.kind == KIND_CHANGED


def _accepted_names(surface: Surface, symbol: str) -> set[str]:
    """Parameter names and TypedDict keys that `symbol` accepts in this version."""
    resolved = surface.resolve(symbol)
    if resolved is None:
        return set()
    if resolved.kind == CLASS:
        return set(surface.all_keys(resolved.symbol))
    function = surface.function(resolved.symbol) if resolved.kind == FUNCTION else None
    if function is None:
        return set()
    names = {param.name for param in function.params}
    for param in function.params:
        if param.unpack is not None and param.unpack in surface.classes:
            names |= set(surface.all_keys(param.unpack))
    return names


def _from_diff(change: Change) -> GradedChange:
    change_type = _DIFF_TYPE[change.kind]
    replacement = change.new_name
    confidence = HIGH
    fixable = change.kind in _CODEMOD_KINDS
    if change.kind == NAME_REMOVED and replacement is not None:
        change_type = ChangeType.RENAMED
        fixable = True
        if change.candidate:
            confidence = MEDIUM  # the rename is inferred from the names alone (F3.4)
    return GradedChange(
        origin=FROM_DIFF,
        change_type=change_type,
        symbol=change.symbol,
        public_symbol=change.display,
        confidence=confidence,
        fixable=fixable,
        affected_parameter=change.param,
        replacement=replacement,
        replacement_verified=replacement is not None,
        migration_hint=change.detail,
        diff_kinds=(change.kind,),
    )


def reconcile(
    records: Iterable[ExtractedChange], old: Surface, new: Surface, diff: ApiDiff
) -> Reconciliation:
    result = Reconciliation()
    confirmed: set[tuple[str, str, str | None]] = set()

    for record in records:
        resolved = old.resolve(record.qualified_symbol)
        if resolved is None:
            result.dropped.append(Dropped(record, SYMBOL_NOT_IN_OLD_VERSION))
            continue

        matched = [c for c in diff.changes if _matches(c, record, resolved.symbol)]
        confirmed.update((c.kind, c.symbol, c.param) for c in matched)

        change_type = record.change_type
        replacement = record.replacement
        verified = False
        if replacement is not None:
            if record.affected_parameter is not None:
                # A parameter's replacement is another parameter (or key) of the same symbol.
                verified = replacement in _accepted_names(new, record.qualified_symbol)
            else:
                verified = new.resolve(replacement) is not None
        removed = any(c.kind in _REMOVAL_KINDS for c in matched)
        if removed and verified and change_type in (ChangeType.REMOVED, ChangeType.RENAMED):
            change_type = ChangeType.RENAMED  # removed here, and its replacement exists (F3.4)

        fixable = record.fixable and change_type not in NEVER_FIXABLE
        if replacement is not None and not verified:
            fixable = False  # never rewrite code to a name that is not there

        result.changes.append(
            GradedChange(
                origin=FROM_NOTES,
                change_type=change_type,
                symbol=resolved.symbol,
                public_symbol=record.qualified_symbol,
                confidence=HIGH if matched else MEDIUM,
                fixable=fixable,
                affected_parameter=record.affected_parameter,
                replacement=replacement,
                replacement_verified=verified,
                evidence_quote=record.evidence_quote,
                migration_hint=record.migration_hint,
                diff_kinds=tuple(sorted({c.kind for c in matched})),
            )
        )

    # What the diff found that no release note mentioned (F3.9).
    for change in diff.changes:
        if (change.kind, change.symbol, change.param) not in confirmed:
            result.changes.append(_from_diff(change))
    return result
