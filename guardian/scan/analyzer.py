"""Analyse one Python file: infer which SDK objects expressions refer to, and match changes.

Types are inferred from what the code itself says, without running it:

- imports and aliases (libcst's `QualifiedNameProvider`);
- assignments, `with ... as`, annotated parameters and `self.x = ...` attributes;
- what an SDK call returns, and what a property returns, from the API surface;
- a module-level client imported from another module of the repo (one hop).

Every name is resolved through the old version's API surface, so inheritance and the
client's property chains (`client.chat.completions.create`) resolve exactly as the API
diff sees them. A name is never matched by spelling alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
    ScopeProvider,
)

from guardian.apidiff.surface import ATTRIBUTE, CLASS, FUNCTION, MODULE, Resolved, Surface
from guardian.scan import rules as R

# Match tiers, from most to least certain (spec 04 technical notes).
DIRECT = "direct"  # `import stripe; stripe.Customer.create(...)`
ALIASED = "aliased"  # `from openai import OpenAI as OAI`, `import openai as oa`
CLIENT_VARIABLE = (
    "client-variable"  # `client = OpenAI(); client.chat...`, `self.client`, typed params
)
CROSS_MODULE = "cross-module"  # `from app.clients import client` (F4.4)
SHAPE_MATCH = "shape-match"  # a dict literal whose keys look like a params TypedDict

TIERS = (DIRECT, ALIASED, CLIENT_VARIABLE, CROSS_MODULE, SHAPE_MATCH)
TIER_CONFIDENCE = {
    DIRECT: "high",
    ALIASED: "high",
    CLIENT_VARIABLE: "high",
    CROSS_MODULE: "medium",
    SHAPE_MATCH: "medium",
}

_INSTANCE, _CLASS, _FUNCTION, _MODULE, _REPO_MODULE, _ATTRIBUTE = (
    "instance",
    "class",
    "function",
    "module",
    "repo-module",
    "attribute",
)

_EMPTY = cst.Module([])


def worse(a: str, b: str) -> str:
    return a if TIERS.index(a) >= TIERS.index(b) else b


def lower_confidence(a: str, b: str) -> str:
    return "medium" if "medium" in (a, b) else "high"


@dataclass(frozen=True)
class Ty:
    """What an expression refers to. `path` is canonical, except for modules."""

    kind: str
    path: str
    tier: str


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    column: int
    code: str  # the source of the matched node, shortened
    symbol: str  # canonical symbol the code resolved to
    rule: str
    tier: str
    confidence: str
    change: int | None = None  # index into the graded changes
    query: int | None = None  # index into the literal queries
    param: str | None = None
    family: str | None = None


@dataclass
class Context:
    """Everything a file analysis needs that is the same for the whole repo."""

    package: str  # the SDK's import name, e.g. openai
    surface: Surface
    index: R.ChangeIndex
    change_confidence: Sequence[str]
    literals: Sequence[R.LiteralQuery] = ()
    families: Sequence[tuple[str, str]] = ()
    # module -> name -> type of a module-level SDK client defined there (F4.4)
    exports: dict[str, dict[str, Ty]] = field(default_factory=dict)


class _Bindings(cst.CSTVisitor):
    """First pass: where each local name, parameter and `self.x` gets its value."""

    def __init__(self) -> None:
        self.values: dict[int, cst.BaseExpression] = {}  # id(target or Param) -> value
        self.annotations: set[int] = set()  # ids whose value is an annotation (an instance)
        self.self_attrs: dict[tuple[int, str], list[tuple[cst.BaseExpression, bool]]] = {}
        self.module_level: dict[str, list[cst.BaseExpression]] = {}
        self._classes: list[cst.ClassDef] = []
        self._functions: list[cst.FunctionDef] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._classes.append(node)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self._classes.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._functions.append(node)
        for param in [*node.params.posonly_params, *node.params.params, *node.params.kwonly_params]:
            if param.annotation is not None:
                self.values[id(param)] = param.annotation.annotation
                self.annotations.add(id(param))

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self._functions.pop()

    def _self_name(self) -> str | None:
        if not self._functions or not self._classes:
            return None
        params = self._functions[-1].params
        first = [*params.posonly_params, *params.params]
        return first[0].name.value if first else None

    def _target(
        self, target: cst.BaseExpression, value: cst.BaseExpression, annotation: bool
    ) -> None:
        if isinstance(target, cst.Name):
            self.values[id(target)] = value
            if annotation:
                self.annotations.add(id(target))
            if not self._functions and not self._classes:
                self.module_level.setdefault(target.value, []).append(value)
        elif (
            isinstance(target, cst.Attribute)
            and isinstance(target.value, cst.Name)
            and target.value.value == self._self_name()
        ):
            key = (id(self._classes[-1]), target.attr.value)
            self.self_attrs.setdefault(key, []).append((value, annotation))

    def visit_Assign(self, node: cst.Assign) -> None:
        for target in node.targets:
            self._target(target.target, node.value, annotation=False)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if node.value is not None:
            self._target(node.target, node.value, annotation=False)
        elif self._classes and not self._functions and isinstance(node.target, cst.Name):
            # A class-level declaration `client: OpenAI` types `self.client`.
            key = (id(self._classes[-1]), node.target.value)
            self.self_attrs.setdefault(key, []).append((node.annotation.annotation, True))
        else:
            self._target(node.target, node.annotation.annotation, annotation=True)

    def visit_WithItem(self, node: cst.WithItem) -> None:
        if node.asname is not None:
            self._target(node.asname.name, node.item, annotation=False)


class FileAnalyzer(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (
        QualifiedNameProvider,
        ScopeProvider,
        PositionProvider,
        ParentNodeProvider,
    )

    def __init__(
        self, ctx: Context, relative: str, modules: Sequence[str], is_package: bool
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.relative = relative
        self.modules = list(modules)
        self.is_package = is_package
        self.findings: list[Finding] = []
        self._bindings = _Bindings()
        self._types: dict[int, Ty | None] = {}
        self._busy: set[int] = set()
        self._seen: set[tuple[int, int, int | None, int | None, str | None]] = set()
        self._in_import = 0
        self._export_types: dict[str, Ty] = {}

    # --- entry points -------------------------------------------------------------

    @classmethod
    def parse(
        cls, ctx: Context, relative: str, modules: Sequence[str], source: str
    ) -> tuple[FileAnalyzer, MetadataWrapper]:
        """Parse a file. Raises `cst.ParserSyntaxError` for code libcst cannot read."""
        wrapper = MetadataWrapper(cst.parse_module(source))
        analyzer = cls(ctx, relative, modules, is_package=relative.endswith("__init__.py"))
        wrapper.module.visit(analyzer._bindings)
        return analyzer, wrapper

    def exports(self, wrapper: MetadataWrapper) -> dict[str, Ty]:
        """Module-level names bound to SDK clients or classes, for other modules (F4.4)."""
        wrapper.visit(_ExportsVisitor(self))
        return self._export_types

    def run(self, wrapper: MetadataWrapper) -> list[Finding]:
        wrapper.visit(self)
        return self.findings

    # --- type inference -----------------------------------------------------------

    def type_of(self, node: cst.BaseExpression) -> Ty | None:
        key = id(node)
        if key in self._types:
            return self._types[key]
        if key in self._busy:
            return None  # a cycle such as `x = x.y`
        self._busy.add(key)
        try:
            result = self._infer(node)
        finally:
            self._busy.discard(key)
        self._types[key] = result
        return result

    def _infer(self, node: cst.BaseExpression) -> Ty | None:
        if isinstance(node, cst.Name):
            return self._name(node)
        if isinstance(node, cst.Attribute):
            return self._attribute(node)
        if isinstance(node, cst.Call):
            return self._call(node)
        if isinstance(node, cst.Await):
            return self.type_of(node.expression)
        return None

    def _from_resolved(self, resolved: Resolved | None, tier: str) -> Ty | None:
        if resolved is None:
            return None
        surface = self.ctx.surface
        if resolved.kind == CLASS:
            return Ty(_CLASS, resolved.symbol, tier)
        if resolved.kind == FUNCTION:
            function = surface.function(resolved.symbol)
            if function is not None and function.is_property:
                if function.returns in surface.classes:
                    return Ty(_INSTANCE, function.returns, tier)
                return None
            return Ty(_FUNCTION, resolved.symbol, tier)
        if resolved.kind == ATTRIBUTE:
            if resolved.type in surface.classes:
                return Ty(_INSTANCE, resolved.type, tier)
            return Ty(_ATTRIBUTE, resolved.symbol, tier)
        if resolved.kind == MODULE:
            return Ty(_MODULE, resolved.symbol, tier)
        return None

    def _absolute(self, qualified: str) -> str:
        """Turn a relative qualified name (`.clients.shared`) into an absolute one."""
        dots = len(qualified) - len(qualified.lstrip("."))
        if dots == 0 or not self.modules:
            return qualified
        parts = self.modules[0].split(".")
        package = parts if self.is_package else parts[:-1]
        base = package[: len(package) - (dots - 1)] if dots > 1 else package
        rest = qualified[dots:]
        return ".".join([*base, rest]) if rest else ".".join(base)

    def _repo_name(self, qualified: str) -> Ty | None:
        """A name from another module of this repo: an exported client, or the module itself."""
        exports = self.ctx.exports
        module, _, name = qualified.rpartition(".")
        exported = exports.get(module, {}).get(name)
        if exported is not None:
            return Ty(exported.kind, exported.path, worse(exported.tier, CROSS_MODULE))
        if qualified in exports:
            return Ty(_REPO_MODULE, qualified, CROSS_MODULE)
        return None

    def _name(self, node: cst.Name) -> Ty | None:
        names = self.get_metadata(QualifiedNameProvider, node, set())
        package = self.ctx.package
        for qualified in names:
            if qualified.source == QualifiedNameSource.IMPORT:
                name = self._absolute(qualified.name)
                if name == package or name.startswith(package + "."):
                    tier = DIRECT if name == node.value else ALIASED
                    return self._from_resolved(self.ctx.surface.resolve(name), tier)
                return self._repo_name(name)
        return self._local(node)

    def _local(self, node: cst.Name) -> Ty | None:
        scope = self.get_metadata(ScopeProvider, node, None)
        if scope is None:
            return None
        try:
            assignments = scope[node.value]
        except Exception:  # names libcst cannot look up (e.g. in comprehensions)
            return None
        types: list[Ty | None] = []
        for assignment in assignments:
            target = getattr(assignment, "node", None)
            value = self._bindings.values.get(id(target))
            if value is None or value is node or _is_none(value):
                continue  # not an assignment we track, or a `None` placeholder
            ty = self.type_of(value)
            if ty is not None and (id(target) in self._bindings.annotations) and ty.kind == _CLASS:
                ty = Ty(_INSTANCE, ty.path, ty.tier)  # `c: OpenAI` means an instance
            types.append(ty)
        return self._unify(types, CLIENT_VARIABLE)

    @staticmethod
    def _unify(types: list[Ty | None], tier: str) -> Ty | None:
        """One type for a name bound in several places, or None when that is not certain.

        `None` entries are bindings to something unknown (another library, a value
        computed at runtime). Any of them makes the name ambiguous: guessing would turn
        into false findings. A plain `x = None` placeholder is dropped before this.
        """
        if not types or any(t is None for t in types):
            return None
        known = [t for t in types if t is not None]
        if len({(t.kind, t.path) for t in known}) != 1:
            return None  # bound to different things: do not guess
        first = known[0]
        if first.kind in (_INSTANCE, _FUNCTION, _ATTRIBUTE):
            return Ty(
                first.kind, first.path, worse(max((t.tier for t in known), key=TIERS.index), tier)
            )
        return first

    def _enclosing_class(self, node: cst.CSTNode) -> cst.ClassDef | None:
        current: cst.CSTNode | None = node
        while current is not None:
            current = self.get_metadata(ParentNodeProvider, current, None)
            if isinstance(current, cst.ClassDef):
                return current
        return None

    def _attribute(self, node: cst.Attribute) -> Ty | None:
        attr = node.attr.value
        if isinstance(node.value, cst.Name) and node.value.value in ("self", "cls"):
            cls = self._enclosing_class(node)
            if cls is not None:
                values = self._bindings.self_attrs.get((id(cls), attr))
                if values:
                    types: list[Ty | None] = []
                    for value, annotation in values:
                        if _is_none(value):
                            continue
                        ty = self.type_of(value)
                        if ty is not None and annotation and ty.kind == _CLASS:
                            ty = Ty(_INSTANCE, ty.path, ty.tier)
                        types.append(ty)
                    return self._unify(types, CLIENT_VARIABLE)

        base = self.type_of(node.value)
        if base is None:
            return None
        surface = self.ctx.surface
        if base.kind == _MODULE:
            return self._from_resolved(surface.resolve(f"{base.path}.{attr}"), base.tier)
        if base.kind == _REPO_MODULE:
            return self._repo_name(f"{base.path}.{attr}")
        if base.kind in (_CLASS, _INSTANCE):
            return self._from_resolved(surface.member(base.path, attr), base.tier)
        return None

    def _call(self, node: cst.Call) -> Ty | None:
        callee = self.type_of(node.func)
        if callee is None:
            return None
        if callee.kind == _CLASS:
            return Ty(_INSTANCE, callee.path, callee.tier)
        if callee.kind == _FUNCTION:
            function = self.ctx.surface.function(callee.path)
            if function is not None and function.returns in self.ctx.surface.classes:
                return Ty(_INSTANCE, function.returns, callee.tier)
        return None

    # --- findings -------------------------------------------------------------------

    def _add(
        self,
        node: cst.CSTNode,
        symbol: str,
        rule: str,
        tier: str,
        *,
        change: int | None = None,
        query: int | None = None,
        param: str | None = None,
        family: str | None = None,
    ) -> None:
        position = self.get_metadata(PositionProvider, node).start
        key = (position.line, position.column, change, query, param)
        if key in self._seen:
            return
        self._seen.add(key)
        confidence = TIER_CONFIDENCE[tier]
        if change is not None:
            confidence = lower_confidence(confidence, self.ctx.change_confidence[change])
        if isinstance(node, cst.Arg) and node.keyword is not None:
            code = f"{node.keyword.value}={_EMPTY.code_for_node(node.value)}"
        else:
            code = _EMPTY.code_for_node(node)
        code = code.strip().replace("\n", " ")
        self.findings.append(
            Finding(
                file=self.relative,
                line=position.line,
                column=position.column + 1,
                code=code[:120],
                symbol=symbol,
                rule=rule,
                tier=tier,
                confidence=confidence,
                change=change,
                query=query,
                param=param,
                family=family,
            )
        )

    def _check_reference(self, node: cst.CSTNode, ty: Ty) -> None:
        for change in self.ctx.index.references.get(ty.path, []):
            self._add(node, ty.path, R.REFERENCE, ty.tier, change=change)

    def _check_module(self, node: cst.CSTNode, module: str) -> None:
        for removed, change in self.ctx.index.modules:
            if module == removed or module.startswith(removed + "."):
                self._add(node, removed, R.MODULE, ALIASED, change=change)

    def visit_Import(self, node: cst.Import) -> None:
        self._in_import += 1
        for alias in node.names:
            name = _EMPTY.code_for_node(alias.name)
            if name == self.ctx.package or name.startswith(self.ctx.package + "."):
                self._check_module(node, name)

    def leave_Import(self, original_node: cst.Import) -> None:
        self._in_import -= 1

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._in_import += 1
        if node.module is None:
            return
        dots = "." * len(node.relative)
        module = self._absolute(dots + _EMPTY.code_for_node(node.module))
        package = self.ctx.package
        if not (module == package or module.startswith(package + ".")):
            return
        self._check_module(node, module)
        if isinstance(node.names, cst.ImportStar):
            return
        for alias in node.names:
            name = _EMPTY.code_for_node(alias.name)
            # `from pkg.types import some_module` imports a module: check it as one.
            self._check_module(alias.name, f"{module}.{name}")
            ty = self._from_resolved(self.ctx.surface.resolve(f"{module}.{name}"), ALIASED)
            if ty is not None:
                self._check_reference(alias.name, ty)

    def leave_ImportFrom(self, original_node: cst.ImportFrom) -> None:
        self._in_import -= 1

    def _expression(self, node: cst.Name | cst.Attribute) -> None:
        if self._in_import:
            return
        ty = self.type_of(node)
        if ty is not None and ty.kind in (_CLASS, _FUNCTION, _ATTRIBUTE):
            self._check_reference(node, ty)

    def visit_Name(self, node: cst.Name) -> None:
        self._expression(node)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self._expression(node)
        # A property used as a value (`client.chat`) resolves to an instance; check the
        # property itself too, in case it is what was removed.
        if not self._in_import:
            base = self.type_of(node.value)
            if base is not None and base.kind in (_CLASS, _INSTANCE):
                resolved = self.ctx.surface.member(base.path, node.attr.value)
                if resolved is not None and resolved.kind == FUNCTION:
                    function = self.ctx.surface.function(resolved.symbol)
                    if function is not None and function.is_property:
                        self._check_reference(node, Ty(_FUNCTION, resolved.symbol, base.tier))

    def visit_Call(self, node: cst.Call) -> None:
        callee = self.type_of(node.func)
        if callee is None:
            return
        surface = self.ctx.surface
        function_path: str | None = None
        if callee.kind == _CLASS:
            function_path = surface.init_path(callee.path)
        elif callee.kind == _FUNCTION:
            function_path = callee.path
        if function_path is None:
            return
        index = self.ctx.index
        keywords = {arg.keyword.value: arg for arg in node.args if arg.keyword is not None}
        splat = any(arg.star == "**" for arg in node.args)

        for param, change in index.keywords.get(function_path, []):
            if param in keywords:
                self._add(
                    keywords[param],
                    function_path,
                    R.KEYWORD,
                    callee.tier,
                    change=change,
                    param=param,
                )
        for param, change in index.missing.get(function_path, []):
            if param not in keywords and not splat:
                self._add(node, function_path, R.MISSING, callee.tier, change=change, param=param)

        if index.dict_keys or index.dict_missing:
            self._dict_literals(node, function_path, callee.tier)
        if self.ctx.literals:
            self._literals(node, function_path, callee.tier, keywords)

    # --- dict literals of TypedDict params (F4.13b) -----------------------------------

    def _dict_literals(self, node: cst.Call, function_path: str, tier: str) -> None:
        function = self.ctx.surface.function(function_path)
        annotations = {p.name: p.annotation or "" for p in function.params} if function else {}
        for arg in node.args:
            if arg.keyword is None:
                continue
            for literal in _dicts_in(arg.value):
                keys = {k: n for k, n in _string_keys(literal)}
                annotation = annotations.get(arg.keyword.value, "")
                self._dict_against(literal, keys, annotation, function_path, tier)

    def _dict_against(
        self,
        literal: cst.Dict,
        keys: dict[str, cst.CSTNode],
        annotation: str,
        function_path: str,
        tier: str,
    ) -> None:
        surface, index = self.ctx.surface, self.ctx.index
        candidates = set(index.dict_keys) | set(index.dict_missing)
        for typed in candidates:
            name = typed.rsplit(".", 1)[-1]
            if re.search(rf"\b{re.escape(name)}\b", annotation):
                match_tier = tier  # the parameter is declared with this TypedDict
            else:
                old_keys = surface.all_keys(typed)
                if len(keys) < 2 or not set(keys) <= set(old_keys):
                    continue
                match_tier = worse(tier, SHAPE_MATCH)  # the dict only looks like one
            for key, change in index.dict_keys.get(typed, []):
                if key in keys:
                    self._add(keys[key], typed, R.DICT_KEY, match_tier, change=change, param=key)
            for key, change in index.dict_missing.get(typed, []):
                if key not in keys:
                    self._add(literal, typed, R.DICT_MISSING, match_tier, change=change, param=key)

    # --- literal arguments (F4.12) ------------------------------------------------------

    def _literals(
        self, node: cst.Call, function_path: str, tier: str, keywords: dict[str, cst.Arg]
    ) -> None:
        family = R.family_of(function_path, self.ctx.families)
        for number, query in enumerate(self.ctx.literals):
            if query.family is not None and query.family != family:
                continue
            arg = keywords.get(query.argument)
            if arg is None:
                continue
            value = arg.value
            if _string_value(value) == query.value:
                self._add(
                    arg,
                    function_path,
                    R.LITERAL,
                    tier,
                    query=number,
                    param=query.argument,
                    family=family,
                )
            elif isinstance(value, cst.Name):
                constant = self._module_constant(value)
                if constant is not None and _string_value(constant[1]) == query.value:
                    self._add(
                        constant[0],
                        function_path,
                        R.LITERAL,
                        tier,
                        query=number,
                        param=query.argument,
                        family=family,
                    )

    def _module_constant(self, name: cst.Name) -> tuple[cst.CSTNode, cst.BaseExpression] | None:
        """A module-level `NAME = "..."` that `name` refers to, one step away."""
        scope = self.get_metadata(ScopeProvider, name, None)
        if scope is None:
            return None
        try:
            assignments = list(scope[name.value])
        except Exception:
            return None
        values = self._bindings.module_level.get(name.value, [])
        if len(assignments) != 1 or len(values) != 1:
            return None  # reassigned: not a constant
        target = getattr(assignments[0], "node", None)
        if target is None or self._bindings.values.get(id(target)) is not values[0]:
            return None
        return target, values[0]


class _ExportsVisitor(cst.CSTVisitor):
    """Collect module-level names bound to SDK clients (exported to other modules)."""

    METADATA_DEPENDENCIES = FileAnalyzer.METADATA_DEPENDENCIES

    def __init__(self, analyzer: FileAnalyzer) -> None:
        super().__init__()
        self.analyzer = analyzer
        analyzer._export_types = {}

    def visit_Module(self, node: cst.Module) -> None:
        # Borrow this visitor's metadata for the analyzer's type inference.
        self.analyzer.metadata = self.metadata

    def leave_Module(self, original_node: cst.Module) -> None:
        analyzer = self.analyzer
        for name, values in analyzer._bindings.module_level.items():
            types = [analyzer.type_of(value) for value in values]
            ty = analyzer._unify(types, CLIENT_VARIABLE)
            if ty is not None and ty.kind in (_INSTANCE, _CLASS, _FUNCTION):
                analyzer._export_types[name] = ty
        analyzer._types.clear()  # computed without other modules' exports


def _is_none(value: cst.BaseExpression) -> bool:
    return isinstance(value, cst.Name) and value.value == "None"


def _dicts_in(value: cst.BaseExpression) -> list[cst.Dict]:
    """A dict literal, or the dict literals directly inside a list or tuple literal."""
    if isinstance(value, cst.Dict):
        return [value]
    if isinstance(value, cst.List | cst.Tuple):
        return [e.value for e in value.elements if isinstance(e.value, cst.Dict)]
    return []


def _string_keys(literal: cst.Dict) -> list[tuple[str, cst.CSTNode]]:
    out: list[tuple[str, cst.CSTNode]] = []
    for element in literal.elements:
        if isinstance(element, cst.DictElement):
            text = _string_value(element.key)
            if text is not None:
                out.append((text, element.key))
    return out


def _string_value(node: Any) -> str | None:
    if isinstance(node, cst.SimpleString | cst.ConcatenatedString):
        value: object = node.evaluated_value
        return value if isinstance(value, str) else None
    return None
