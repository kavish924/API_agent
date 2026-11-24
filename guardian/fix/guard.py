"""AST guard for every model-written diff (spec 05 F5.7–F5.10).

The guard compares parsed trees, not diff text. Each file is split into statement
units: a simple statement line, or the header of a compound statement or clause (`def
f(...):`, `with ...:`, `else:`), each with its line span and nesting depth. The before
and after unit sequences are aligned, and every unit that changed must pass:

- a removed or replaced unit holds a finding (a finding's line is in its span) or is
  an import from the watched SDK (F5.7);
- an added unit is an SDK import, or replaces a unit that held a finding: a fix never
  adds more statements than it replaces;
- an added import of any other module is rejected (F5.8);
- no **new names**: a free name in a new unit (not a keyword-argument name, attribute
  name or parameter) must already appear in the units it replaces, or be bound by an
  SDK import. This rejects `eval(...)`, `exec`, `getattr`, `__import__`, `open` and
  similar wrapped around the matched call (spec 05 decision, 2026-11-24).

Whole-file rules: only existing `.py` files that hold findings may change, nothing is
created or deleted, the result must parse, and the diff stays under a size cap (F5.9).
A rejected diff is never partly applied: the caller gets the reasons (F5.10).

The guard narrows what a diff can do; it does not prove the diff correct.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

DEFAULT_LINE_CAP = 40  # changed lines per finding (F5.9)

_CONSTANTS = frozenset({"True", "False", "None"})
_MAX_REASONS = 10  # the rest is summarised, so a feedback message stays small

_EMPTY = cst.Module([])


@dataclass(frozen=True)
class FileChange:
    path: str
    before: str | None  # None: the file does not exist yet
    after: str | None  # None: the change deletes it


@dataclass
class GuardResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    changed_lines: int = 0
    diff: str = ""


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    depth: int
    code: str  # the unit's source lines, indentation stripped
    node: cst.CSTNode
    header_only: bool  # compound statement header: names are read from the header only

    @property
    def key(self) -> tuple[int, str]:
        return (self.depth, self.code)

    def holds(self, lines: Iterable[int]) -> bool:
        return any(self.start <= line <= self.end for line in lines)


class _Units(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, lines: Sequence[str]) -> None:
        super().__init__()
        self.lines = lines
        self.units: list[_Unit] = []
        self.depth = 0

    def _code(self, start: int, end: int) -> str:
        return "\n".join(line.strip() for line in self.lines[start - 1 : end])

    def _add(self, node: cst.CSTNode, start: int, end: int, header_only: bool) -> None:
        unit = _Unit(start, end, self.depth, self._code(start, end), node, header_only)
        self.units.append(unit)

    def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine) -> bool:
        position = self.get_metadata(PositionProvider, node)
        self._add(node, position.start.line, position.end.line, header_only=False)
        return False

    def visit_IndentedBlock(self, node: cst.IndentedBlock) -> None:
        self.depth += 1

    def leave_IndentedBlock(self, original_node: cst.IndentedBlock) -> None:
        self.depth -= 1

    def on_visit(self, node: cst.CSTNode) -> bool:
        body = getattr(node, "body", None)
        if isinstance(body, cst.IndentedBlock | cst.SimpleStatementSuite) and not isinstance(
            node, cst.Module
        ):
            position = self.get_metadata(PositionProvider, node)
            start = position.start.line
            decorators = getattr(node, "decorators", ())
            if decorators:
                start = min(start, self.get_metadata(PositionProvider, decorators[0]).start.line)
            if isinstance(body, cst.SimpleStatementSuite):
                self._add(node, start, position.end.line, header_only=False)
                return False  # `if x: y()` is one unit
            first = body.body[0]
            first_line = self.get_metadata(PositionProvider, first).start.line
            leading = getattr(first, "leading_lines", ())
            header_end = max(start, first_line - len(leading) - 1)
            self._add(node, start, header_end, header_only=True)
        return super().on_visit(node)


class _Names(cst.CSTVisitor):
    """Free names in a unit: what the code reads or calls, not what it labels."""

    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        node.value.visit(self)
        return False  # `.attr` is not a free name

    def visit_Arg(self, node: cst.Arg) -> bool:
        node.value.visit(self)
        return False  # `keyword=` is not a free name

    def visit_Param(self, node: cst.Param) -> bool:
        for part in (node.annotation, node.default):
            if part is not None:
                part.visit(self)
        return False

    def visit_IndentedBlock(self, node: cst.IndentedBlock) -> bool:
        return False  # a header unit's body belongs to other units

    def visit_ImportAlias(self, node: cst.ImportAlias) -> bool:
        return False


def _owns_block(node: cst.CSTNode) -> bool:
    return isinstance(node, cst.IndentedBlock) or isinstance(
        getattr(node, "body", None), cst.IndentedBlock | cst.SimpleStatementSuite
    )


def _names(unit: _Unit) -> set[str]:
    visitor = _Names()
    if unit.header_only:
        # The header only: not the body, and not `elif`/`else`/`except` clauses, which
        # are units of their own.
        for child in unit.node.children:
            if not _owns_block(child):
                child.visit(visitor)
    else:
        unit.node.visit(visitor)
    return visitor.names


def _module_of(node: cst.Import | cst.ImportFrom) -> list[str]:
    if isinstance(node, cst.Import):
        return [_EMPTY.code_for_node(alias.name) for alias in node.names]
    if node.relative or node.module is None:
        return ["." * len(node.relative)]
    return [_EMPTY.code_for_node(node.module)]


def _imports(unit: _Unit) -> list[cst.Import | cst.ImportFrom]:
    if not isinstance(unit.node, cst.SimpleStatementLine):
        return []
    return [s for s in unit.node.body if isinstance(s, cst.Import | cst.ImportFrom)]


def _in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _is_sdk_import(unit: _Unit, package: str) -> bool:
    if not isinstance(unit.node, cst.SimpleStatementLine):
        return False
    statements = unit.node.body
    if not statements or len(_imports(unit)) != len(statements):
        return False
    return all(
        _in_package(module, package)
        for statement in _imports(unit)
        for module in _module_of(statement)
    )


def _sdk_bound_names(units: Sequence[_Unit], package: str) -> set[str]:
    bound: set[str] = set()
    for unit in units:
        if not _is_sdk_import(unit, package):
            continue
        for statement in _imports(unit):
            if isinstance(statement.names, cst.ImportStar):
                continue
            for alias in statement.names:
                if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                    bound.add(alias.asname.name.value)
                elif isinstance(statement, cst.Import):
                    bound.add(_EMPTY.code_for_node(alias.name).split(".")[0])
                else:
                    bound.add(_EMPTY.code_for_node(alias.name))
    return bound


def _units(source: str) -> list[_Unit]:
    module = cst.parse_module(source)
    visitor = _Units(source.splitlines())
    MetadataWrapper(module, unsafe_skip_copy=True).visit(visitor)
    return visitor.units


def _diff(path: str, before: str, after: str) -> tuple[str, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    changed = sum(
        1
        for line in lines
        if line[:1] in "+-" and not line.startswith("+++") and not line.startswith("---")
    )
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines)
    return text, changed


def _check_file(
    path: str, before: str, after: str, findings: Sequence[int], package: str
) -> list[str]:
    try:
        old_units = _units(before)
    except cst.ParserSyntaxError:
        return [f"{path}: the original file does not parse"]
    try:
        new_units = _units(after)
    except cst.ParserSyntaxError as exc:
        return [f"{path}: the patched file does not parse: {exc.message}"]

    reasons: list[str] = []
    sdk_names = _sdk_bound_names(new_units, package)
    matcher = difflib.SequenceMatcher(
        None, [u.key for u in old_units], [u.key for u in new_units], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = old_units[i1:i2], new_units[j1:j2]
        holding = [u for u in removed if u.holds(findings)]
        for unit in removed:
            if not unit.holds(findings) and not _is_sdk_import(unit, package):
                reasons.append(f"{path}:{unit.start}: changes a statement that has no finding")
        allowed = set().union(*(_names(u) for u in removed)) | sdk_names | _CONSTANTS
        statements = 0
        for unit in added:
            imports = _imports(unit)
            if imports:
                for statement in imports:
                    for module in _module_of(statement):
                        if not _in_package(module, package):
                            reasons.append(
                                f"{path}:{unit.start}: adds an import of {module or '?'}, "
                                f"which is not {package}"
                            )
                if _is_sdk_import(unit, package):
                    continue
            statements += 1
            new_names = sorted(_names(unit) - allowed)
            if new_names:
                reasons.append(
                    f"{path}:{unit.start}: uses names the matched code did not: "
                    + ", ".join(new_names)
                )
        if statements > len(holding):
            line = added[0].start if added else 0
            reasons.append(
                f"{path}:{line}: adds {statements - len(holding)} statement(s) "
                "beyond the ones that hold findings"
            )
    return reasons


def check_patch(
    changes: Sequence[FileChange],
    finding_lines: Mapping[str, Sequence[int]],
    *,
    package: str,
    findings: int = 1,
    line_cap: int = DEFAULT_LINE_CAP,
) -> GuardResult:
    """Accept or reject one model-written change set (F5.7–F5.10).

    `finding_lines` maps a repo-relative path to the lines of its findings, in the
    `before` text. `package` is the watched SDK's import name. `findings` is how many
    findings the change addresses; the size cap is `line_cap` per finding.
    """
    reasons: list[str] = []
    diffs: list[str] = []
    changed = 0
    for change in changes:
        path = change.path
        if change.before == change.after:
            continue
        if not path.endswith(".py"):
            reasons.append(f"{path}: only Python files may change")
            continue
        if change.before is None:
            reasons.append(f"{path}: creates a file")
            continue
        if change.after is None:
            reasons.append(f"{path}: deletes a file")
            continue
        lines = finding_lines.get(path, ())
        if not lines:
            reasons.append(f"{path}: touches a file that has no findings")
            continue
        diff, count = _diff(path, change.before, change.after)
        diffs.append(diff)
        changed += count
        reasons += _check_file(path, change.before, change.after, lines, package)

    cap = line_cap * max(1, findings)
    if changed > cap:
        reasons.append(f"changes {changed} lines, over the cap of {cap}")
    if not reasons and changed == 0:
        reasons.append("the change is empty")
    if len(reasons) > _MAX_REASONS:
        extra = len(reasons) - _MAX_REASONS
        reasons = reasons[:_MAX_REASONS] + [f"... and {extra} more"]
    return GuardResult(
        accepted=not reasons, reasons=reasons, changed_lines=changed, diff="".join(diffs)
    )
