"""The public API surface of one SDK version (spec 03 F3.1, F3.2, F3.12, F3.13).

griffe loads the package **statically**: nothing is imported or executed. The result is
reduced to a small, serialisable model, so a surface is built once per version, cached
as JSON, and compared or queried without griffe.

Two kinds of path appear here:

- a **public path** is how user code reaches a name: `stripe.Customer`;
- a **canonical path** is where it is defined: `stripe._customer.Customer`.

Public names are followed through aliases, including into underscore-private modules.
That is the whole reason this module exists: Stripe exports every public name as an
alias into a private module, and griffe's own breaking-change check skips both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import griffe

FORMAT = 1  # bump when the model changes, so cached surfaces are rebuilt

POSITIONAL_ONLY = "positional-only"
POSITIONAL_OR_KEYWORD = "positional or keyword"
KEYWORD_ONLY = "keyword-only"
VAR_POSITIONAL = "variadic positional"
VAR_KEYWORD = "variadic keyword"
VARIADIC = (VAR_POSITIONAL, VAR_KEYWORD)

MODULE, CLASS, FUNCTION, ATTRIBUTE, KEY = "module", "class", "function", "attribute", "key"

DOC_LIMIT = 4000


class SurfaceError(Exception):
    """The package could not be loaded."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    required: bool
    default: str | None = None
    annotation: str | None = None
    unpack: str | None = None  # for `**kwargs: Unpack[X]`: canonical path of the TypedDict X


@dataclass(frozen=True)
class Function:
    path: str  # canonical path of the declaring class or module, plus the name
    params: tuple[Param, ...]
    returns: str | None = None  # canonical path, when the return annotation is one name
    is_property: bool = False
    doc: str | None = None

    def signature(self) -> str:
        parts: list[str] = []
        seen_star = False
        for index, param in enumerate(self.params):
            text = param.name
            if param.kind == VAR_POSITIONAL:
                text, seen_star = "*" + text, True
            elif param.kind == VAR_KEYWORD:
                text = "**" + text
            elif param.kind == KEYWORD_ONLY and not seen_star:
                parts.append("*")
                seen_star = True
            if param.annotation:
                text += f": {param.annotation}"
            if param.default is not None:
                text += f" = {param.default}"
            parts.append(text)
            following = self.params[index + 1].kind if index + 1 < len(self.params) else None
            if param.kind == POSITIONAL_ONLY and following != POSITIONAL_ONLY:
                parts.append("/")
        return f"{self.path.rsplit('.', 1)[-1]}({', '.join(parts)})"


@dataclass(frozen=True)
class Class:
    path: str
    bases: tuple[str, ...]  # canonical paths of base classes inside the package
    methods: dict[str, Function]  # declared on this class (not inherited)
    attributes: dict[str, str | None]  # name -> canonical path of its type, if known
    classes: dict[str, str]  # nested class name -> canonical path
    typed_dict: bool = False
    keys: dict[str, bool] = field(default_factory=dict)  # TypedDict key -> required
    doc: str | None = None


@dataclass(frozen=True)
class Resolved:
    """What a dotted symbol refers to. `symbol` is canonical and names the declaring class."""

    kind: str
    symbol: str
    owner: str | None = None  # canonical path of the declaring class, for members
    type: str | None = None  # for attributes: canonical path of the annotated type


@dataclass(frozen=True)
class ApiSymbol:
    """Answer to `lookup`: what the agent fixer sees for a symbol (spec 11 F11.6)."""

    symbol: str
    kind: str
    signature: str | None
    doc: str | None


@dataclass
class Surface:
    package: str
    version: str
    public: dict[str, str] = field(default_factory=dict)  # public path -> canonical path
    modules: set[str] = field(default_factory=set)  # public module paths
    classes: dict[str, Class] = field(default_factory=dict)  # by canonical path
    functions: dict[str, Function] = field(default_factory=dict)  # module-level, canonical
    attributes: set[str] = field(default_factory=set)  # module-level, canonical
    unresolved: list[str] = field(default_factory=list)  # in-package aliases griffe lost
    _methods: dict[str, dict[str, Function]] = field(
        default_factory=dict, repr=False, compare=False
    )

    # --- queries -------------------------------------------------------------

    def kind_of(self, canonical: str) -> str | None:
        if canonical in self.classes:
            return CLASS
        if canonical in self.functions:
            return FUNCTION
        if canonical in self.attributes:
            return ATTRIBUTE
        return None

    def mro(self, class_path: str) -> list[Class]:
        """The class and its in-package bases, nearest first. Good enough for lookup."""
        order: list[Class] = []
        seen: set[str] = set()
        stack = [class_path]
        while stack:
            path = stack.pop(0)
            cls = self.classes.get(path)
            if cls is None or path in seen:
                continue
            seen.add(path)
            order.append(cls)
            stack = [*cls.bases, *stack]
        return order

    def all_methods(self, class_path: str) -> dict[str, Function]:
        """Methods including inherited ones. `Function.path` names the declaring class."""
        cached = self._methods.get(class_path)
        if cached is None:
            cached = {}
            for cls in self.mro(class_path):
                for name, function in cls.methods.items():
                    cached.setdefault(name, function)
            self._methods[class_path] = cached
        return cached

    def all_keys(self, class_path: str) -> dict[str, bool]:
        """TypedDict keys including inherited ones."""
        keys: dict[str, bool] = {}
        for cls in self.mro(class_path):
            for name, required in cls.keys.items():
                keys.setdefault(name, required)
        return keys

    def all_attributes(self, class_path: str) -> set[str]:
        return {name for cls in self.mro(class_path) for name in cls.attributes}

    def function(self, symbol: str) -> Function | None:
        if symbol in self.functions:
            return self.functions[symbol]
        owner, _, name = symbol.rpartition(".")
        cls = self.classes.get(owner)
        return cls.methods.get(name) if cls is not None else None

    # --- resolving dotted symbols ---------------------------------------------

    def resolve(self, symbol: str) -> Resolved | None:
        """Resolve a public or canonical dotted symbol, with or without the package prefix.

        Follows inheritance (`stripe.Customer.request` resolves to the base class that
        declares `request`) and typed properties (`openai.OpenAI.chat.completions.create`).
        """
        parts = [part for part in symbol.strip().split(".") if part]
        if not parts:
            return None
        attempts = [parts] if parts[0] == self.package else [parts, [self.package, *parts]]
        for attempt in attempts:
            for cut in range(len(attempt), 0, -1):
                start = self._start(".".join(attempt[:cut]))
                if start is None:
                    continue
                found = self._walk(start, attempt[cut:])
                if found is not None:
                    return found
        return None

    def _start(self, path: str) -> Resolved | None:
        canonical = self.public.get(path, path)
        kind = self.kind_of(canonical)
        if kind is not None:
            return Resolved(kind, canonical)
        if path in self.modules:
            return Resolved(MODULE, path)
        return None

    def _walk(self, current: Resolved, rest: list[str]) -> Resolved | None:
        for part in rest:
            owner = self._as_class(current)
            if owner is None:
                return None
            found = self._member(owner, part)
            if found is None:
                return None
            current = found
        return current

    def _as_class(self, resolved: Resolved) -> str | None:
        if resolved.kind == CLASS:
            return resolved.symbol
        if resolved.kind == FUNCTION:
            function = self.function(resolved.symbol)
            if function is not None and function.is_property and function.returns in self.classes:
                return function.returns
        if resolved.kind == ATTRIBUTE and resolved.type in self.classes:
            return resolved.type
        return None

    def _member(self, class_path: str, name: str) -> Resolved | None:
        for cls in self.mro(class_path):
            if name in cls.methods:
                return Resolved(FUNCTION, cls.methods[name].path, owner=cls.path)
            if name in cls.classes:
                return Resolved(CLASS, cls.classes[name])
            if name in cls.keys:
                return Resolved(KEY, f"{cls.path}.{name}", owner=cls.path)
            if name in cls.attributes:
                return Resolved(
                    ATTRIBUTE, f"{cls.path}.{name}", owner=cls.path, type=cls.attributes[name]
                )
        return None

    def lookup(self, symbol: str) -> ApiSymbol | None:
        """Signature and docstring of a symbol in this version (F3.12)."""
        resolved = self.resolve(symbol)
        if resolved is None:
            return None
        if resolved.kind == FUNCTION:
            function = self.function(resolved.symbol)
            if function is not None:
                return ApiSymbol(resolved.symbol, FUNCTION, function.signature(), function.doc)
        if resolved.kind == CLASS:
            cls = self.classes[resolved.symbol]
            init = self.all_methods(resolved.symbol).get("__init__")
            signature = None
            if init is not None:
                name = cls.path.rsplit(".", 1)[-1]
                signature = init.signature().replace("__init__", name, 1)
            return ApiSymbol(resolved.symbol, CLASS, signature, cls.doc)
        return ApiSymbol(resolved.symbol, resolved.kind, None, None)

    # --- serialisation ---------------------------------------------------------

    def to_json(self) -> str:
        def function(f: Function) -> dict[str, Any]:
            return {
                "path": f.path,
                "params": [
                    [p.name, p.kind, p.required, p.default, p.annotation, p.unpack]
                    for p in f.params
                ],
                "returns": f.returns,
                "is_property": f.is_property,
                "doc": f.doc,
            }

        return json.dumps(
            {
                "format": FORMAT,
                "package": self.package,
                "version": self.version,
                "public": self.public,
                "modules": sorted(self.modules),
                "attributes": sorted(self.attributes),
                "unresolved": self.unresolved,
                "functions": {path: function(f) for path, f in self.functions.items()},
                "classes": {
                    path: {
                        "bases": list(c.bases),
                        "methods": {name: function(f) for name, f in c.methods.items()},
                        "attributes": c.attributes,
                        "classes": c.classes,
                        "typed_dict": c.typed_dict,
                        "keys": c.keys,
                        "doc": c.doc,
                    }
                    for path, c in self.classes.items()
                },
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> Surface:
        data = json.loads(text)
        if data.get("format") != FORMAT:
            raise SurfaceError(f"unsupported surface format: {data.get('format')!r}")

        def function(raw: dict[str, Any]) -> Function:
            return Function(
                path=raw["path"],
                params=tuple(Param(*item) for item in raw["params"]),
                returns=raw["returns"],
                is_property=raw["is_property"],
                doc=raw["doc"],
            )

        return cls(
            package=data["package"],
            version=data["version"],
            public=data["public"],
            modules=set(data["modules"]),
            attributes=set(data["attributes"]),
            unresolved=data["unresolved"],
            functions={path: function(raw) for path, raw in data["functions"].items()},
            classes={
                path: Class(
                    path=path,
                    bases=tuple(raw["bases"]),
                    methods={name: function(f) for name, f in raw["methods"].items()},
                    attributes=raw["attributes"],
                    classes=raw["classes"],
                    typed_dict=raw["typed_dict"],
                    keys=raw["keys"],
                    doc=raw["doc"],
                )
                for path, raw in data["classes"].items()
            },
        )


# --- building a surface from source ---------------------------------------------
#
# griffe objects are typed as Any below: aliases and objects share most attributes but
# not a useful common type, and this code only reads from them.


def _doc(obj: Any) -> str | None:
    docstring = getattr(obj, "docstring", None)
    return docstring.value[:DOC_LIMIT] if docstring is not None else None


def _canonical(expr: Any) -> str | None:
    """Canonical path of an annotation that is a single name, else None."""
    if not isinstance(expr, griffe.ExprName | griffe.ExprAttribute):
        return None
    try:
        return str(expr.canonical_path)
    except Exception:  # an annotation griffe cannot resolve is simply unknown
        return None


class _Builder:
    def __init__(self, package: str) -> None:
        self.package = package
        self.surface = Surface(package=package, version="")
        self.root: Any = None
        self._walked: set[str] = set()
        self._typed_dict: dict[str, bool] = {}

    def _in_package(self, path: str) -> bool:
        return path == self.package or path.startswith(self.package + ".")

    def _target(self, member: Any, public_path: str | None) -> Any | None:
        """The object a member refers to, following aliases. None for anything external."""
        if not member.is_alias:
            return member
        try:
            target = member.final_target
        except (griffe.AliasResolutionError, griffe.CyclicAliasError):
            if public_path is not None and not self._leads_outside(member):
                self.surface.unresolved.append(public_path)
            return None
        return target if self._in_package(target.canonical_path) else None

    def _leads_outside(self, member: Any) -> bool:
        """Does this alias chain end in another package (`openai.Timeout` → `httpx.Timeout`)?

        Those are expected and not counted as unresolved. Only an alias that stays
        inside the package and still cannot be followed is a real gap.
        """
        seen: set[str] = set()
        current = member
        while getattr(current, "is_alias", False):
            path = str(current.target_path)
            if not self._in_package(path):
                return True
            if path in seen or self.root is None:
                return False
            seen.add(path)
            try:
                current = self.root.get_member(path[len(self.package) + 1 :].split("."))
            except Exception:  # the next link is missing: a genuine in-package gap
                return False
        return False

    def walk_module(self, module: Any, public_path: str) -> None:
        self.surface.modules.add(public_path)
        self._walked.add(module.canonical_path)
        # A name a module merely imports is not part of that module's API. It counts
        # only where re-exporting is the point: a package `__init__`, or `__all__`.
        reexports = bool(getattr(module, "is_init_module", False))
        exported = {str(item) for item in (getattr(module, "exports", None) or [])}
        for name, member in module.members.items():
            if name.startswith("_"):
                continue
            if member.is_alias and not reexports and name not in exported:
                continue
            path = f"{public_path}.{name}"
            target = self._target(member, path)
            if target is None:
                continue
            if target.is_module:
                # A real submodule, or a private module re-exported under a public name.
                # A public module imported somewhere else is walked under its own path.
                private = any(part.startswith("_") for part in target.canonical_path.split("."))
                if target.canonical_path not in self._walked and (not member.is_alias or private):
                    self.walk_module(target, path)
            elif target.is_class:
                self.surface.public[path] = self._class(target)
            elif target.is_function:
                self.surface.public[path] = target.canonical_path
                self.surface.functions[target.canonical_path] = self._function(
                    target, target.canonical_path, method=False
                )
            else:
                self.surface.public[path] = target.canonical_path
                self.surface.attributes.add(target.canonical_path)

    def _bases(self, cls: Any) -> list[Any]:
        try:
            resolved = list(cls.resolved_bases)
        except Exception:  # a base griffe cannot resolve is treated as external
            return []
        return [b for b in resolved if b.is_class and self._in_package(b.canonical_path)]

    def _is_typed_dict(self, cls: Any) -> bool:
        path = cls.canonical_path
        if path not in self._typed_dict:
            self._typed_dict[path] = False  # guards against cycles
            own = any(
                str(base).split("[")[0].rsplit(".", 1)[-1] == "TypedDict" for base in cls.bases
            )
            self._typed_dict[path] = own or any(self._is_typed_dict(b) for b in self._bases(cls))
        return self._typed_dict[path]

    def _class(self, cls: Any) -> str:
        path = str(cls.canonical_path)
        if path in self.surface.classes:
            return path
        # Reserve the slot first: classes can refer to each other.
        self.surface.classes[path] = Class(path, (), {}, {}, {})
        bases = tuple(self._class(base) for base in self._bases(cls))
        typed_dict = self._is_typed_dict(cls)
        total = str(getattr(cls, "keywords", {}).get("total", "True")) != "False"

        methods: dict[str, Function] = {}
        attributes: dict[str, str | None] = {}
        classes: dict[str, str] = {}
        keys: dict[str, bool] = {}
        for name, member in cls.members.items():
            if name.startswith("_") and name != "__init__":
                continue
            target = self._target(member, None)
            if target is None:
                continue
            if target.is_function:
                methods[name] = self._function(target, f"{path}.{name}", method=True)
            elif target.is_class:
                classes[name] = self._class(target)
            elif target.is_attribute:
                annotation = getattr(target, "annotation", None)
                attributes[name] = _canonical(annotation)
                if typed_dict and annotation is not None:
                    text = str(annotation)
                    if text.startswith("Required["):
                        keys[name] = True
                    elif text.startswith("NotRequired["):
                        keys[name] = False
                    else:
                        keys[name] = total
        self.surface.classes[path] = Class(
            path, bases, methods, attributes, classes, typed_dict, keys, _doc(cls)
        )
        return path

    def _function(self, function: Any, path: str, *, method: bool) -> Function:
        params: list[Param] = []
        for index, param in enumerate(function.parameters):
            if method and index == 0 and param.name in ("self", "cls"):
                continue
            kind = str(param.kind.value)
            annotation = param.annotation
            unpack = None
            if (
                kind == VAR_KEYWORD
                and isinstance(annotation, griffe.ExprSubscript)
                and str(annotation.left).rsplit(".", 1)[-1] == "Unpack"
            ):
                unpack = _canonical(annotation.slice)
            params.append(
                Param(
                    name=param.name,
                    kind=kind,
                    required=bool(param.required),
                    # griffe gives *args and **kwargs the defaults "()" and "{}".
                    default=None
                    if param.default is None or kind in VARIADIC
                    else str(param.default),
                    annotation=None if annotation is None else str(annotation),
                    unpack=unpack,
                )
            )
        decorators = [str(d.value).rsplit(".", 1)[-1] for d in function.decorators]
        doc = _doc(function)
        if doc is None:
            doc = next((d for d in map(_doc, function.overloads or []) if d), None)
        return Function(
            path=path,
            params=tuple(params),
            returns=_canonical(function.returns),
            is_property="property" in function.labels
            or any(name.endswith("property") for name in decorators),
            doc=doc,
        )


def build_surface(search_path: Path, package: str, version: str) -> Surface:
    """Build the surface of `package` from source under `search_path`, without importing it."""
    try:
        root = griffe.load(
            package,
            search_paths=[str(search_path)],
            allow_inspection=False,
            resolve_aliases=True,
            resolve_implicit=False,
            resolve_external=False,
        )
    except Exception as exc:
        raise SurfaceError(f"could not load {package} {version}: {exc}") from exc
    builder = _Builder(package)
    builder.root = root
    builder.walk_module(root, package)
    builder.surface.version = version
    builder.surface.unresolved.sort()
    return builder.surface
