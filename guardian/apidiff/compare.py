"""Compare two API surfaces and list what would break callers (spec 03 F3.3, F3.4, F3.14, F3.15).

This is the project's own comparison, not `griffe.find_breaking_changes`. The spike
showed why: griffe's check reported nothing for Stripe 15.6.2 → 16.0.0 (a release with
a class rename, a removed method and a signature change on every service method), and
on openai it flagged 42 changes of which about 2 break callers.

Identity is the **public path**: a class that moves between private modules but keeps
its public name is unchanged. Each change is reported once, on the canonical symbol
that declares it, with every public path it shows up under.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field

from guardian.apidiff.surface import (
    ATTRIBUTE,
    CLASS,
    FORMAT,
    FUNCTION,
    KEYWORD_ONLY,
    POSITIONAL_ONLY,
    POSITIONAL_OR_KEYWORD,
    VAR_KEYWORD,
    VARIADIC,
    Function,
    Surface,
)

# Reported kinds: each breaks some caller.
MODULE_REMOVED = "module-removed"
NAME_REMOVED = "name-removed"
KIND_CHANGED = "kind-changed"
METHOD_REMOVED = "method-removed"
PARAM_REMOVED = "param-removed"
PARAM_RENAMED = "param-renamed"
PARAM_REQUIRED = "param-required"  # was optional, or is new, and must now be passed
PARAM_KIND = "param-kind"  # e.g. keyword-capable became positional-only
KEY_REMOVED = "typeddict-key-removed"
KEY_REQUIRED = "typeddict-key-required"
KWARG_REMOVED = "kwarg-removed"  # a key of the TypedDict a method takes as **kwargs
KWARG_REQUIRED = "kwarg-required"

# Ignored kinds: counted, never reported (F3.15).
IGNORE_PLACEHOLDER_DEFAULT = "placeholder-default"
IGNORE_DEFAULT_CHANGED = "default-changed"
IGNORE_CLASS_TO_ALIAS = "class-to-alias"
IGNORE_ATTRIBUTE_REMOVED = "attribute-removed"

# Defaults that only mean "not passed". Moving between them changes nothing for callers.
PLACEHOLDER_DEFAULTS = frozenset({"NOT_GIVEN", "not_given", "NotGiven()", "omit", "Omit()"})

# Kind changes that only allow more call styles.
_WIDENING = {
    (POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD),
    (KEYWORD_ONLY, POSITIONAL_OR_KEYWORD),
}


@dataclass(frozen=True)
class Change:
    kind: str
    symbol: str  # canonical, in the old version; names the declaring class for members
    public: tuple[str, ...]  # public paths this shows up under
    param: str | None = None  # parameter or TypedDict key
    new_name: str | None = None  # where it went, for a rename or a move
    candidate: bool = False  # the rename is inferred from names, not certain
    detail: str = ""

    @property
    def display(self) -> str:
        """The shortest public path: how a person would write this symbol."""
        return min(self.public, key=lambda path: (len(path), path)) if self.public else self.symbol


@dataclass
class ApiDiff:
    package: str
    old: str
    new: str
    changes: list[Change] = field(default_factory=list)
    ignored: dict[str, int] = field(default_factory=dict)
    unresolved_old: int = 0
    unresolved_new: int = 0

    @property
    def breaking(self) -> bool:
        return bool(self.changes)

    def for_symbol(self, symbol: str) -> list[Change]:
        return [change for change in self.changes if change.symbol == symbol]

    def to_json(self) -> str:
        data = asdict(self)
        data["format"] = FORMAT
        return json.dumps(data, indent=1)

    @classmethod
    def from_json(cls, text: str) -> ApiDiff:
        data = json.loads(text)
        data.pop("format", None)
        changes = [Change(**{**raw, "public": tuple(raw["public"])}) for raw in data.pop("changes")]
        return cls(changes=changes, **data)


class _Accumulator:
    """Merges the same change seen under several public paths into one entry."""

    def __init__(self) -> None:
        self.changes: dict[tuple[str, str, str | None], Change] = {}
        self.ignored: Counter[str] = Counter()

    def add(
        self,
        kind: str,
        symbol: str,
        public: str,
        *,
        param: str | None = None,
        new_name: str | None = None,
        candidate: bool = False,
        detail: str = "",
    ) -> None:
        key = (kind, symbol, param)
        existing = self.changes.get(key)
        if existing is None:
            self.changes[key] = Change(kind, symbol, (public,), param, new_name, candidate, detail)
        elif public not in existing.public:
            merged = tuple(sorted({*existing.public, public}))
            self.changes[key] = Change(
                kind, symbol, merged, param, existing.new_name, existing.candidate, existing.detail
            )


# One finding about a pair of functions, before it is tied to a public path.
_Finding = tuple[str, str | None, str | None, bool, str]  # kind, param, new_name, candidate, detail


def _function_findings(
    old: Surface, new: Surface, before: Function, after: Function
) -> list[_Finding]:
    findings: list[_Finding] = []
    old_params = {p.name: p for p in before.params}
    new_params = {p.name: p for p in after.params}
    new_kwargs = next((p for p in after.params if p.kind == VAR_KEYWORD), None)
    typed_kwargs = new_kwargs is not None and new_kwargs.unpack in new.classes
    accepted_keys = (
        new.all_keys(new_kwargs.unpack) if new_kwargs and new_kwargs.unpack and typed_kwargs else {}
    )

    def still_accepted(name: str, kind: str) -> bool:
        """A named parameter that disappeared may still be accepted through **kwargs."""
        if new_kwargs is None or kind == POSITIONAL_ONLY:
            return False
        return name in accepted_keys if typed_kwargs else True

    removed = [
        p
        for p in before.params
        if p.name not in new_params
        and p.kind not in VARIADIC
        and not still_accepted(p.name, p.kind)
    ]
    added = [p for p in after.params if p.name not in old_params and p.kind not in VARIADIC]

    # One parameter out and one in, same kind and same position: a rename.
    if len(removed) == 1 and len(added) == 1:
        gone, came = removed[0], added[0]
        if gone.kind == came.kind and before.params.index(gone) == after.params.index(came):
            findings.append((PARAM_RENAMED, gone.name, came.name, True, ""))
            removed, added = [], []
    findings += [(PARAM_REMOVED, p.name, None, False, "") for p in removed]
    findings += [
        (PARAM_REQUIRED, p.name, None, False, "new required parameter") for p in added if p.required
    ]

    for name, was in old_params.items():
        now = new_params.get(name)
        if now is None or was.kind in VARIADIC or now.kind in VARIADIC:
            continue
        if was.kind != now.kind and (was.kind, now.kind) not in _WIDENING:
            findings.append((PARAM_KIND, name, None, False, f"{was.kind} -> {now.kind}"))
        if not was.required and now.required:
            findings.append((PARAM_REQUIRED, name, None, False, "was optional"))
        elif was.default != now.default:
            placeholders = (
                was.default in PLACEHOLDER_DEFAULTS and now.default in PLACEHOLDER_DEFAULTS
            )
            rule = IGNORE_PLACEHOLDER_DEFAULT if placeholders else IGNORE_DEFAULT_CHANGED
            findings.append((rule, name, None, False, ""))

    # Keys of the TypedDict taken as **kwargs (Stripe's `**params: Unpack[...]`).
    old_kwargs = next((p for p in before.params if p.kind == VAR_KEYWORD), None)
    if old_kwargs is not None and old_kwargs.unpack in old.classes and old_kwargs.unpack:
        old_keys = old.all_keys(old_kwargs.unpack)
        if typed_kwargs:
            now_keys = accepted_keys
        elif new_kwargs is not None:
            now_keys = None  # untyped **kwargs still accepts anything
        else:
            now_keys = {p.name: p.required for p in after.params if p.kind != POSITIONAL_ONLY}
        if now_keys is not None:
            for key in old_keys:
                if key not in now_keys:
                    findings.append((KWARG_REMOVED, key, None, False, ""))
            for key, required in now_keys.items():
                if typed_kwargs and required and not old_keys.get(key, False):
                    findings.append((KWARG_REQUIRED, key, None, False, ""))
    return findings


class _Comparison:
    def __init__(self, old: Surface, new: Surface) -> None:
        self.old, self.new = old, new
        self.acc = _Accumulator()
        self._function_cache: dict[tuple[str, str], list[_Finding]] = {}
        self._classes_done: set[tuple[str, str]] = set()

    def functions(self, before: Function, after: Function, public: str) -> None:
        pair = (before.path, after.path)
        findings = self._function_cache.get(pair)
        if findings is None:
            findings = _function_findings(self.old, self.new, before, after)
            self._function_cache[pair] = findings
            for kind, *_ in findings:
                if kind in (IGNORE_PLACEHOLDER_DEFAULT, IGNORE_DEFAULT_CHANGED):
                    self.acc.ignored[kind] += 1  # counted once per function pair
        for kind, param, new_name, candidate, detail in findings:
            if kind in (IGNORE_PLACEHOLDER_DEFAULT, IGNORE_DEFAULT_CHANGED):
                continue
            self.acc.add(
                kind,
                before.path,
                public,
                param=param,
                new_name=new_name,
                candidate=candidate,
                detail=detail,
            )

    def classes(self, old_path: str, new_path: str, public: str) -> None:
        old_methods = self.old.all_methods(old_path)
        new_methods = self.new.all_methods(new_path)
        for name, before in old_methods.items():
            after = new_methods.get(name)
            if after is not None:
                self.functions(before, after, f"{public}.{name}")
            elif name != "__init__":
                # Reported on the class that declared it, once, however many inherit it.
                self.acc.add(METHOD_REMOVED, before.path, f"{public}.{name}")

        pair = (old_path, new_path)
        first_time = pair not in self._classes_done
        self._classes_done.add(pair)
        old_class, new_class = self.old.classes[old_path], self.new.classes[new_path]
        if old_class.typed_dict and new_class.typed_dict:
            old_keys, new_keys = self.old.all_keys(old_path), self.new.all_keys(new_path)
            for key in old_keys:
                if key not in new_keys:
                    self.acc.add(KEY_REMOVED, old_path, public, param=key)
            for key, required in new_keys.items():
                if required and not old_keys.get(key, False):
                    self.acc.add(KEY_REQUIRED, old_path, public, param=key)
        elif first_time:
            # Fields of response models. A removed one breaks code that reads it, but a
            # static scan cannot find those reads, so it is counted, not reported.
            gone = set(old_class.attributes) - self.new.all_attributes(new_path)
            self.acc.ignored[IGNORE_ATTRIBUTE_REMOVED] += len(gone)

    def run(self) -> None:
        old, new, acc = self.old, self.new, self.acc

        removed_modules = sorted(old.modules - new.modules)
        outermost = [
            m for m in removed_modules if not any(m.startswith(o + ".") for o in removed_modules)
        ]
        for module in outermost:
            acc.add(MODULE_REMOVED, module, module)

        added_by_parent: dict[str, list[str]] = {}
        for path in set(new.public) - set(old.public):
            added_by_parent.setdefault(path.rpartition(".")[0], []).append(path)
        new_by_canonical: dict[str, list[str]] = {}
        for path, canonical in new.public.items():
            new_by_canonical.setdefault(canonical, []).append(path)

        for public, canonical in sorted(old.public.items()):
            if any(public.startswith(module + ".") for module in outermost):
                continue  # covered by the module's own entry
            old_kind = old.kind_of(canonical)
            if public not in new.public:
                moved = sorted(new_by_canonical.get(canonical, []))
                if moved:  # the same object, now only under another public path
                    acc.add(NAME_REMOVED, canonical, public, new_name=moved[0], detail="moved")
                    continue
                parent, _, name = public.rpartition(".")
                lowered = name.lower()
                candidates = [
                    path
                    for path in added_by_parent.get(parent, [])
                    if new.kind_of(new.public[path]) == old_kind
                    and (
                        lowered in path.rpartition(".")[2].lower()
                        or path.rpartition(".")[2].lower() in lowered
                    )
                ]
                rename = candidates[0] if len(candidates) == 1 else None
                acc.add(
                    NAME_REMOVED, canonical, public, new_name=rename, candidate=rename is not None
                )
                continue

            new_canonical = new.public[public]
            new_kind = new.kind_of(new_canonical)
            if old_kind != new_kind:
                if old_kind == CLASS and new_kind == ATTRIBUTE and "types" in public.split("."):
                    acc.ignored[IGNORE_CLASS_TO_ALIAS] += 1  # generated class became a type alias
                else:
                    acc.add(KIND_CHANGED, canonical, public, detail=f"{old_kind} -> {new_kind}")
            elif old_kind == FUNCTION:
                self.functions(old.functions[canonical], new.functions[new_canonical], public)
            elif old_kind == CLASS:
                self.classes(canonical, new_canonical, public)


def compare(old: Surface, new: Surface) -> ApiDiff:
    """Everything in `old`'s public API that `new` breaks for callers."""
    comparison = _Comparison(old, new)
    comparison.run()
    changes = sorted(
        comparison.acc.changes.values(), key=lambda c: (c.kind, c.symbol, c.param or "")
    )
    return ApiDiff(
        package=old.package,
        old=old.version,
        new=new.version,
        changes=changes,
        ignored=dict(sorted(comparison.acc.ignored.items())),
        unresolved_old=len(old.unresolved),
        unresolved_new=len(new.unresolved),
    )
