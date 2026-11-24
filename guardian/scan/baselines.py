"""Two simpler scanners to compare the real one against (spec 08 F8.3).

- **grep**: a line matches when it contains the changed name, `param=`, or the quoted
  key. This is what a person or a naive tool would do.
- **libcst without resolution**: parses the code but matches by spelling only: a call
  whose method name is the changed method's name, an attribute or name spelled like a
  removed symbol, a dict key in a dict literal passed to a call. No imports, no types.

Both return the (file, line) pairs they would flag, for the same changes the real
scanner gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from guardian.scan.files import python_files
from guardian.scan.rules import ChangeIndex

Line = tuple[str, int]


@dataclass
class Words:
    """What the baselines search for, taken from the change index."""

    names: set[str] = field(default_factory=set)  # removed classes, functions, methods, modules
    keyword_calls: set[tuple[str, str]] = field(default_factory=set)  # (method name, param)
    keys: set[str] = field(default_factory=set)  # removed TypedDict keys

    @classmethod
    def from_index(cls, index: ChangeIndex) -> Words:
        words = cls()
        words.names = {symbol.rsplit(".", 1)[-1] for symbol in index.references}
        words.names |= {module.rsplit(".", 1)[-1] for module, _ in index.modules}
        for function, entries in index.keywords.items():
            method = function.rsplit(".", 1)[-1]
            if method == "__init__":
                method = function.rsplit(".", 2)[-2]  # a constructor is called by class name
            words.keyword_calls |= {(method, param) for param, _ in entries}
        words.keys = {key for entries in index.dict_keys.values() for key, _ in entries}
        return words


def grep_lines(root: Path, words: Words) -> set[Line]:
    patterns = [rf"\b{re.escape(name)}\b" for name in words.names]
    patterns += [rf"\b{re.escape(param)}\s*=" for _, param in words.keyword_calls]
    patterns += [rf"[\"']{re.escape(key)}[\"']" for key in words.keys]
    if not patterns:
        return set()
    regex = re.compile("|".join(patterns))
    found: set[Line] = set()
    for source in python_files(root):
        text = source.path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                found.add((source.relative, number))
    return found


class _SpellingVisitor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, relative: str, words: Words) -> None:
        super().__init__()
        self.relative = relative
        self.words = words
        self.found: set[Line] = set()

    def _hit(self, node: cst.CSTNode) -> None:
        self.found.add((self.relative, self.get_metadata(PositionProvider, node).start.line))

    def visit_Name(self, node: cst.Name) -> None:
        if node.value in self.words.names:
            self._hit(node)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        if node.attr.value in self.words.names:
            self._hit(node)

    def visit_Call(self, node: cst.Call) -> None:
        func = node.func
        name = (
            func.attr.value
            if isinstance(func, cst.Attribute)
            else func.value
            if isinstance(func, cst.Name)
            else None
        )
        for arg in node.args:
            if arg.keyword is not None and (name, arg.keyword.value) in self.words.keyword_calls:
                self._hit(arg)
            for literal in _dicts(arg.value):
                for element in literal.elements:
                    key = getattr(element, "key", None)
                    if isinstance(key, cst.SimpleString) and key.evaluated_value in self.words.keys:
                        self._hit(key)


def _dicts(value: cst.BaseExpression) -> list[cst.Dict]:
    if isinstance(value, cst.Dict):
        return [value]
    if isinstance(value, cst.List | cst.Tuple):
        return [e.value for e in value.elements if isinstance(e.value, cst.Dict)]
    return []


def spelling_lines(root: Path, words: Words) -> set[Line]:
    found: set[Line] = set()
    for source in python_files(root):
        try:
            module = cst.parse_module(source.path.read_text(encoding="utf-8", errors="replace"))
        except cst.ParserSyntaxError:
            continue
        visitor = _SpellingVisitor(source.relative, words)
        MetadataWrapper(module).visit(visitor)
        found |= visitor.found
    return found
