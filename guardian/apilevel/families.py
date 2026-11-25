"""API families of model IDs and SDK methods (spec 10 F10.15, F10.16).

The rules live in `families.toml` next to this file, as reviewed config.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

UNKNOWN = "unknown"
CONFIG = Path(__file__).with_name("families.toml")


@dataclass(frozen=True)
class ModelRule:
    glob: str
    families: frozenset[str]


@dataclass(frozen=True)
class FamilyTable:
    models: tuple[ModelRule, ...]
    methods: tuple[tuple[str, str], ...]  # (canonical path prefix, family)

    def of_model(self, model_id: str) -> tuple[frozenset[str], str | None]:
        """The model's families and the glob that decided it (None: `unknown`)."""
        lowered = model_id.lower()
        for rule in self.models:
            if fnmatch.fnmatchcase(lowered, rule.glob):
                return rule.families, rule.glob
        return frozenset({UNKNOWN}), None

    def of_method(self, canonical: str) -> str | None:
        best: tuple[int, str] | None = None
        for prefix, family in self.methods:
            if canonical.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), family)
        return best[1] if best else None


def parse_table(text: str) -> FamilyTable:
    data = tomllib.loads(text)
    models = tuple(
        ModelRule(str(rule["glob"]).lower(), frozenset(str(f) for f in rule["families"]))
        for rule in data.get("models", [])
    )
    methods = tuple((str(k), str(v)) for k, v in data.get("methods", {}).items())
    return FamilyTable(models, methods)


@cache
def default_table() -> FamilyTable:
    return parse_table(CONFIG.read_text(encoding="utf-8"))
