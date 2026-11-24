"""What each graded change looks like in code (spec 04 F4.5, F4.12, F4.13).

A graded change (spec 03) names a canonical symbol in the old version. This module
turns it into the code shapes that break:

- REFERENCE: any use of a removed or renamed name (a class, function, method, field);
- MODULE: an import of a removed module;
- KEYWORD: a call to a function that passes a changed parameter by keyword;
- MISSING: a call that does not pass a newly required parameter;
- DICT_KEY / DICT_MISSING: a dict literal of a `TypedDict` params type that has a
  removed key, or lacks a newly required one.

Literal queries (spec 10's model retirements) are a separate input: a string passed to
a named argument of an SDK method in a given API family.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from guardian.apidiff import compare as cmp
from guardian.apidiff.reconcile import GradedChange
from guardian.apidiff.surface import CLASS, FUNCTION, VAR_KEYWORD, Surface

REFERENCE = "reference"
MODULE = "module"
KEYWORD = "keyword"
MISSING = "missing"
DICT_KEY = "dict-key"
DICT_MISSING = "dict-missing"
LITERAL = "literal"

_DIFF_RULE = {
    cmp.NAME_REMOVED: REFERENCE,
    cmp.METHOD_REMOVED: REFERENCE,
    cmp.KIND_CHANGED: REFERENCE,
    cmp.MODULE_REMOVED: MODULE,
    cmp.PARAM_RENAMED: KEYWORD,
    cmp.PARAM_REMOVED: KEYWORD,
    cmp.PARAM_KIND: KEYWORD,
    cmp.KWARG_REMOVED: KEYWORD,
    cmp.PARAM_REQUIRED: MISSING,
    cmp.KWARG_REQUIRED: MISSING,
    cmp.KEY_REMOVED: DICT_KEY,
    cmp.KEY_REQUIRED: DICT_MISSING,
}


@dataclass(frozen=True)
class LiteralQuery:
    """A string passed to a named argument, e.g. `model="gpt-5"` (spec 10 F10.8)."""

    value: str
    argument: str
    family: str | None = None  # an API family from `families`; None matches any


@dataclass
class ChangeIndex:
    """Graded changes indexed by the code shape that would break."""

    references: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    modules: list[tuple[str, int]] = field(default_factory=list)
    keywords: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    missing: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    dict_keys: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    dict_missing: dict[str, list[tuple[str, int]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    unmatchable: list[int] = field(default_factory=list)  # nothing in code can show them

    @property
    def empty(self) -> bool:
        return not (
            self.references
            or self.modules
            or self.keywords
            or self.missing
            or self.dict_keys
            or self.dict_missing
        )

    def words(self) -> set[str]:
        """Identifiers whose presence in a file could mean a finding (for the pre-filter)."""
        out = {symbol.rsplit(".", 1)[-1] for symbol in self.references}
        out |= {module.rsplit(".", 1)[-1] for module, _ in self.modules}
        for table in (self.keywords, self.missing, self.dict_keys, self.dict_missing):
            out |= {param for entries in table.values() for param, _ in entries}
        return out


def _unpack_users(surface: Surface) -> dict[str, list[str]]:
    """TypedDict class -> functions that take it as `**params: Unpack[...]`."""
    users: dict[str, list[str]] = defaultdict(list)
    functions = list(surface.functions.values())
    for cls in surface.classes.values():
        functions.extend(cls.methods.values())
    for function in functions:
        for param in function.params:
            if param.kind == VAR_KEYWORD and param.unpack is not None:
                users[param.unpack].append(function.path)
    return users


def build_index(changes: Sequence[GradedChange], surface: Surface) -> ChangeIndex:
    index = ChangeIndex()
    users: dict[str, list[str]] | None = None

    for position, change in enumerate(changes):
        symbol, param = change.symbol, change.affected_parameter
        rules = {_DIFF_RULE[kind] for kind in change.diff_kinds if kind in _DIFF_RULE}
        if not rules:  # a release-note record the diff did not confirm
            kind = surface.kind_of(symbol)
            if symbol in surface.modules:
                rules = {MODULE}
            elif param is None:
                rules = {REFERENCE}
            elif kind in (FUNCTION, CLASS):
                rules = {KEYWORD}
            else:
                rules = {REFERENCE}

        matched = False
        for rule in rules:
            if rule == REFERENCE:
                index.references[symbol].append(position)
                matched = True
            elif rule == MODULE:
                index.modules.append((symbol, position))
                matched = True
            elif param is None:
                continue
            elif rule in (DICT_KEY, DICT_MISSING) or (
                surface.kind_of(symbol) == CLASS and surface.classes[symbol].typed_dict
            ):
                table = index.dict_missing if rule == DICT_MISSING else index.dict_keys
                table[symbol].append((param, position))
                # A params TypedDict is also what methods take as **params (Stripe).
                if users is None:
                    users = _unpack_users(surface)
                target = index.missing if rule == DICT_MISSING else index.keywords
                for function in users.get(symbol, []):
                    # The diff often reports the same break on the method itself
                    # (kwarg-removed); one finding per call site is enough.
                    if all(existing != param for existing, _ in target.get(function, [])):
                        target[function].append((param, position))
                matched = True
            else:
                function = symbol
                if surface.kind_of(symbol) == CLASS:
                    init = surface.init_path(symbol)
                    if init is None:
                        continue
                    function = init  # a parameter of a class is a parameter of its __init__
                table = index.missing if rule == MISSING else index.keywords
                table[function].append((param, position))
                matched = True
        if not matched:
            index.unmatchable.append(position)
    return index


# --- API families (spec 10 F10.15) -------------------------------------------------
#
# A reviewed map from where an SDK method is defined to the API it calls. Longest
# prefix wins. Used to tell `tts-1` passed to the speech API from the same string
# passed anywhere else.

FAMILIES: dict[str, tuple[tuple[str, str], ...]] = {
    "openai": (
        ("openai.resources.chat.", "chat"),
        ("openai.resources.responses.", "responses"),
        ("openai.resources.completions.", "completions"),
        ("openai.resources.embeddings.", "embeddings"),
        ("openai.resources.images.", "images"),
        ("openai.resources.audio.speech.", "audio.speech"),
        ("openai.resources.audio.transcriptions.", "audio.transcriptions"),
        ("openai.resources.audio.translations.", "audio.transcriptions"),
        ("openai.resources.realtime.", "realtime"),
        ("openai.resources.beta.realtime.", "realtime"),
        ("openai.resources.moderations.", "moderations"),
    ),
}


def family_of(path: str, families: Sequence[tuple[str, str]]) -> str | None:
    best: tuple[int, str] | None = None
    for prefix, family in families:
        if path.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), family)
    return best[1] if best else None
