"""Tier 1 fixes: deterministic libcst codemods (spec 05 F5.3).

Each codemod rewrites exactly the node a finding points at (by the line and column the
scanner recorded) and nothing else, so formatting and comments are kept and the rest
of the file stays byte-identical. Four kinds:

- **parameter rename**: `create(old_name=x)` -> `create(new_name=x)`, and the same for
  a renamed key in a dict literal of a `TypedDict` params type;
- **symbol rename**: `stripe.TransferReversal` -> `stripe.Reversal`, including
  `from stripe import TransferReversal` and every use of that imported name;
- **import path**: `from pkg.old import X` -> `from pkg.new import X` for a moved
  symbol or module, when the import names only that symbol;
- **literal replacement**: `model="gpt-x"` -> `model="gpt-y"`, also through a
  module-level constant (spec 10).

A finding no codemod can handle is left for Tier 2 with the reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    ScopeProvider,
)

from guardian.apidiff.reconcile import GradedChange
from guardian.extract.schema import ChangeType
from guardian.scan import rules as R
from guardian.scan.analyzer import Finding

_EMPTY = cst.Module([])

# Why a finding was left for Tier 2.
NO_REPLACEMENT = "no-verified-replacement"
NOT_A_RENAME = "not-a-rename"
SHAPE_UNSUPPORTED = "code-shape-not-supported"
NODE_NOT_FOUND = "node-not-found"


@dataclass
class CodemodResult:
    text: str
    handled: set[int] = field(default_factory=set)  # finding indexes, as given
    skipped: dict[int, str] = field(default_factory=dict)  # index -> reason

    @property
    def changed(self) -> bool:
        return bool(self.handled)


@dataclass(frozen=True)
class _Rename:
    old: str  # last segment as written
    new: str
    old_parent: str  # public parent path, e.g. "stripe"
    new_parent: str


def _identifier_path(path: str) -> bool:
    return bool(path) and all(part.isidentifier() for part in path.split("."))


def _symbol_rename(change: GradedChange) -> _Rename | None:
    if change.replacement is None or not change.replacement_verified:
        return None
    if change.change_type not in (ChangeType.RENAMED, ChangeType.REMOVED):
        return None
    if not _identifier_path(change.replacement):
        return None
    old_parent, _, old = change.public_symbol.rpartition(".")
    new_parent, _, new = change.replacement.rpartition(".")
    return _Rename(old, new, old_parent, new_parent)


def _string_with(node: cst.SimpleString, value: str) -> cst.SimpleString | None:
    """The same string literal with new contents, keeping its prefix and quotes."""
    quote = node.quote
    if quote in value or "\\" in value or "\n" in value:
        return None
    return node.with_changes(value=f"{node.prefix}{quote}{value}{quote}")


def _plain_string(node: cst.CSTNode) -> bool:
    return isinstance(node, cst.SimpleString) and "b" not in node.prefix.lower()


class _Planner(cst.CSTVisitor):
    """Find the node each finding points at and decide the edit for it."""

    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider, ScopeProvider)

    def __init__(
        self,
        targets: Mapping[tuple[int, int], list[int]],
        findings: Sequence[Finding],
        changes: Sequence[GradedChange],
        literal_replacements: Mapping[int, str],
    ) -> None:
        super().__init__()
        self.targets = targets
        self.findings = findings
        self.changes = changes
        self.literals = literal_replacements
        # id(original node) -> replacement node
        self.replace: dict[int, cst.CSTNode] = {}
        self.handled: set[int] = set()
        self.skipped: dict[int, str] = {}
        self._name_findings: list[tuple[int, cst.Name]] = []

    # --- helpers ---------------------------------------------------------------------

    def _at(self, node: cst.CSTNode) -> list[int]:
        start = self.get_metadata(PositionProvider, node).start
        return self.targets.get((start.line, start.column + 1), [])

    def _change(self, index: int) -> GradedChange | None:
        number = self.findings[index].change
        return self.changes[number] if number is not None else None

    def _take(self, index: int, original: cst.CSTNode, updated: cst.CSTNode) -> None:
        self.replace[id(original)] = updated
        self.handled.add(index)
        self.skipped.pop(index, None)

    def _skip(self, index: int, reason: str) -> None:
        if index not in self.handled:
            self.skipped.setdefault(index, reason)

    # --- keyword arguments ----------------------------------------------------------

    def visit_Arg(self, node: cst.Arg) -> None:
        for index in self._at(node):
            finding = self.findings[index]
            if finding.rule == R.KEYWORD and node.keyword is not None:
                self._keyword(index, node)
            elif finding.rule == R.LITERAL and node.keyword is not None:
                self._literal_arg(index, node)

    def _keyword(self, index: int, node: cst.Arg) -> None:
        change = self._change(index)
        finding = self.findings[index]
        assert node.keyword is not None
        if node.keyword.value != finding.param:
            return
        if change is None or not change.replacement or not change.replacement_verified:
            self._skip(index, NO_REPLACEMENT)
            return
        if change.affected_parameter != finding.param or not change.replacement.isidentifier():
            self._skip(index, NOT_A_RENAME)
            return
        updated = node.with_changes(keyword=node.keyword.with_changes(value=change.replacement))
        self._take(index, node, updated)

    def _literal_arg(self, index: int, node: cst.Arg) -> None:
        finding = self.findings[index]
        replacement = self.literals.get(finding.query) if finding.query is not None else None
        if replacement is None:
            self._skip(index, NO_REPLACEMENT)
            return
        if not isinstance(node.value, cst.SimpleString) or not _plain_string(node.value):
            self._skip(index, SHAPE_UNSUPPORTED)
            return
        string = _string_with(node.value, replacement)
        if string is None:
            self._skip(index, SHAPE_UNSUPPORTED)
            return
        self._take(index, node.value, string)

    # --- dict keys and module constants ------------------------------------------------

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        for index in self._at(node):
            finding = self.findings[index]
            if finding.rule != R.DICT_KEY:
                continue
            change = self._change(index)
            if change is None or not change.replacement or not change.replacement_verified:
                self._skip(index, NO_REPLACEMENT)
                continue
            if change.affected_parameter != finding.param or node.evaluated_value != finding.param:
                self._skip(index, NOT_A_RENAME)
                continue
            string = _string_with(node, change.replacement)
            if string is None:
                self._skip(index, SHAPE_UNSUPPORTED)
                continue
            self._take(index, node, string)

    def visit_Name(self, node: cst.Name) -> None:
        for index in self._at(node):
            finding = self.findings[index]
            if finding.rule == R.LITERAL:
                self._literal_constant(index, node)
            elif finding.rule == R.REFERENCE and not self._in_import(node):
                self._name_findings.append((index, node))

    def _literal_constant(self, index: int, node: cst.Name) -> None:
        finding = self.findings[index]
        replacement = self.literals.get(finding.query) if finding.query is not None else None
        if replacement is None:
            self._skip(index, NO_REPLACEMENT)
            return
        target = self.get_metadata(ParentNodeProvider, node, None)
        assign = self.get_metadata(ParentNodeProvider, target, None) if target else None
        value = assign.value if isinstance(assign, cst.Assign | cst.AnnAssign) else None
        if value is None or not _plain_string(value):
            self._skip(index, SHAPE_UNSUPPORTED)
            return
        assert isinstance(value, cst.SimpleString)
        string = _string_with(value, replacement)
        if string is None:
            self._skip(index, SHAPE_UNSUPPORTED)
            return
        self._take(index, value, string)

    # --- symbol references ------------------------------------------------------------

    def visit_Attribute(self, node: cst.Attribute) -> None:
        for index in self._at(node):
            finding = self.findings[index]
            if finding.rule != R.REFERENCE or self._in_import(node):
                continue
            change = self._change(index)
            rename = _symbol_rename(change) if change is not None else None
            if rename is None:
                self._skip(index, NO_REPLACEMENT)
                continue
            if node.attr.value != rename.old:
                continue  # another attribute that starts here; the right one is nested
            if rename.old_parent != rename.new_parent:
                self._skip(index, SHAPE_UNSUPPORTED)  # moved: the path before it changes too
                continue
            attr = node.attr.with_changes(value=rename.new)
            self._take(index, node, node.with_changes(attr=attr))

    def _in_import(self, node: cst.CSTNode) -> bool:
        current: cst.CSTNode | None = node
        while current is not None:
            if isinstance(current, cst.Import | cst.ImportFrom):
                return True
            if isinstance(current, cst.BaseStatement):
                return False
            current = self.get_metadata(ParentNodeProvider, current, None)
        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.relative or node.module is None:
            return
        module = _EMPTY.code_for_node(node.module)
        # A removed module that moved: `from pkg.old import X` (finding on the statement).
        for index in self._at(node):
            finding = self.findings[index]
            if finding.rule != R.MODULE:
                continue
            change = self._change(index)
            if change is None or not change.replacement or not change.replacement_verified:
                self._skip(index, NO_REPLACEMENT)
                continue
            if module != change.public_symbol or not _identifier_path(change.replacement):
                self._skip(index, SHAPE_UNSUPPORTED)
                continue
            self._take(index, node, node.with_changes(module=_dotted(change.replacement)))

        if isinstance(node.names, cst.ImportStar):
            return
        for alias in node.names:
            for index in self._at(alias.name):
                if self.findings[index].rule == R.REFERENCE:
                    self._import_alias(index, node, module, alias)

    def _import_alias(
        self, index: int, statement: cst.ImportFrom, module: str, alias: cst.ImportAlias
    ) -> None:
        change = self._change(index)
        rename = _symbol_rename(change) if change is not None else None
        name = _EMPTY.code_for_node(alias.name)
        if rename is None:
            self._skip(index, NO_REPLACEMENT)
            return
        if name != rename.old or module != rename.old_parent:
            self._skip(index, SHAPE_UNSUPPORTED)  # imported through another public path
            return
        new_alias = alias.with_changes(name=cst.Name(rename.new))
        if rename.old_parent != rename.new_parent:
            assert not isinstance(statement.names, cst.ImportStar)
            if len(statement.names) != 1:
                self._skip(index, SHAPE_UNSUPPORTED)  # would need the import split
                return
            if id(statement) in self.replace:
                self._skip(index, SHAPE_UNSUPPORTED)
                return
            updated = statement.with_changes(module=_dotted(rename.new_parent), names=[new_alias])
            self._take(index, statement, updated)
        else:
            self._take(index, alias, new_alias)
        if alias.asname is None:
            self._rename_uses(statement, rename)

    def _rename_uses(self, statement: cst.ImportFrom, rename: _Rename) -> None:
        """An unaliased import binds the old name: every use of it is renamed too."""
        scope = self.get_metadata(ScopeProvider, statement, None)
        if scope is None:
            return
        for assignment in scope.assignments[rename.old]:
            if getattr(assignment, "node", None) is not statement:
                continue
            for access in assignment.references:
                if isinstance(access.node, cst.Name) and access.node.value == rename.old:
                    self.replace[id(access.node)] = access.node.with_changes(value=rename.new)

    def leave_Module(self, original_node: cst.Module) -> None:
        # A use of an imported name is fixed by fixing the import (and its uses).
        for index, node in self._name_findings:
            if id(node) in self.replace:
                self.handled.add(index)
                self.skipped.pop(index, None)
            elif self._bound_by_fixed_import(node):
                self.handled.add(index)  # aliased import: the alias name still works
                self.skipped.pop(index, None)
            else:
                self._skip(index, SHAPE_UNSUPPORTED)

    def _bound_by_fixed_import(self, node: cst.Name) -> bool:
        scope = self.get_metadata(ScopeProvider, node, None)
        if scope is None:
            return False
        for assignment in scope[node.value]:
            statement = getattr(assignment, "node", None)
            if not isinstance(statement, cst.ImportFrom):
                continue
            if id(statement) in self.replace:
                return True
            if isinstance(statement.names, cst.ImportStar):
                continue
            if any(id(alias) in self.replace for alias in statement.names):
                return True
        return False


def _dotted(path: str) -> cst.Attribute | cst.Name:
    parts = path.split(".")
    node: cst.Attribute | cst.Name = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


class _Applier(cst.CSTTransformer):
    def __init__(self, replace: Mapping[int, cst.CSTNode]) -> None:
        super().__init__()
        self.replace = replace

    def on_leave(  # type: ignore[override]
        self, original_node: cst.CSTNode, updated_node: cst.CSTNode
    ) -> cst.CSTNode:
        replacement = self.replace.get(id(original_node))
        if replacement is None:
            return updated_node
        if isinstance(original_node, cst.ImportFrom):
            # Keep any alias renamed inside it; take the new module (and names, if the
            # replacement set them).
            assert isinstance(replacement, cst.ImportFrom)
            assert isinstance(updated_node, cst.ImportFrom)
            names = replacement.names if replacement.names is not original_node.names else None
            return updated_node.with_changes(
                module=replacement.module, names=names or updated_node.names
            )
        return replacement


def apply_codemods(
    source: str,
    findings: Sequence[Finding],
    changes: Sequence[GradedChange],
    *,
    literal_replacements: Mapping[int, str] | None = None,
) -> CodemodResult:
    """Apply every codemod that fits the findings of one file.

    `findings` are this file's findings; indexes in the result refer to this sequence.
    `literal_replacements` maps a literal query index to its replacement value.
    """
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return CodemodResult(source, skipped={i: SHAPE_UNSUPPORTED for i in range(len(findings))})
    targets: dict[tuple[int, int], list[int]] = {}
    for index, finding in enumerate(findings):
        targets.setdefault((finding.line, finding.column), []).append(index)

    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    planner = _Planner(targets, findings, changes, literal_replacements or {})
    wrapper.visit(planner)
    for index in range(len(findings)):
        if index not in planner.handled:
            planner.skipped.setdefault(index, NODE_NOT_FOUND)
    if not planner.replace:
        return CodemodResult(source, skipped=planner.skipped)
    updated = module.visit(_Applier(planner.replace))
    return CodemodResult(updated.code, handled=planner.handled, skipped=planner.skipped)
