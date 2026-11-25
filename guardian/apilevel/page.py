"""Parse OpenAI's deprecations page into records, with plain code (spec 10 F10.3, F10.4, F10.16).

The page is Markdown with regular tables. Each row of "Upcoming deprecations" gives a
shutdown date, the affected model IDs (in backticks), a status, a recommended
replacement and a notice date; it becomes one record per model ID. No model is used:
the rows are regular, and parsing them as code is cheap, repeatable and cannot be
steered by text on the page.

A replacement cell is classified by shape:

- `single`: exactly one model ID and nothing else;
- `choice`: several model IDs joined by "or";
- `with-settings`: one model ID with settings in backticks (`reasoning.mode: pro`) or
  qualifiers such as "in the Responses API";
- `none`: no model ID (prose such as "The most capable model available to you").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from guardian.apilevel.families import UNKNOWN, FamilyTable, default_table

MODEL_RETIRED = "model-retired"
ENDPOINT_SUNSET = "endpoint-sunset"

SINGLE = "single"
CHOICE = "choice"
WITH_SETTINGS = "with-settings"
NONE = "none"

UPCOMING = "Upcoming deprecations"
FEATURES = "Feature and endpoint deprecations"

# Why a record is report-only (F10.16), in words the PR or issue can show.
WHY = {
    CHOICE: "the page recommends a choice of models; a person has to pick one",
    WITH_SETTINGS: "the replacement needs extra settings or another API",
    NONE: "the page names no replacement model",
    "family": "the replacement belongs to a different API than the retired model",
    UNKNOWN: "the model's API family is unknown to the reviewed family table",
    ENDPOINT_SUNSET: "endpoint and feature sunsets are listed, not fixed, in this version",
}

_ID = re.compile(r"`([^`]+)`")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)


@dataclass(frozen=True)
class Row:
    section: str
    shutdown: date | None
    affected: str
    status: str
    replacement: str
    notice: date | None
    line: str  # the raw table row: the evidence quote (F10.4)


@dataclass(frozen=True)
class Replacement:
    kind: str  # SINGLE | CHOICE | WITH_SETTINGS | NONE
    models: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApiRecord:
    provider: str
    kind: str  # MODEL_RETIRED | ENDPOINT_SUNSET
    identifier: str  # one model ID, or the endpoint cell's text
    families: frozenset[str]
    argument: str | None  # the argument the identifier is passed in, e.g. "model"
    replacement: str | None
    replacement_kind: str
    shutdown_date: date | None
    evidence_quote: str
    fixable: bool
    reason: str = ""  # why it is report-only, when it is
    decided_by: tuple[str, ...] = field(default=())  # family rules that decided the case

    def key(self) -> tuple[str, str]:
        return (self.kind, self.identifier)

    def content(self) -> tuple[object, ...]:
        """What makes a record changed when it reappears on a later snapshot."""
        return (self.replacement, self.replacement_kind, self.shutdown_date, self.fixable)


def _date(cell: str) -> date | None:
    text = re.sub(r"[*_]", "", cell).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_rows(markdown: str) -> list[Row]:
    """Rows of the tables under the upcoming and feature deprecation headings."""
    rows: list[Row] = []
    section = ""
    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if heading:
            section = heading.group(2)
            continue
        if section not in (UPCOMING, FEATURES):
            continue
        cells = _cells(line)
        if cells is None or len(cells) < 5 or set("".join(cells)) <= set("-: "):
            continue
        if cells[0].lower().startswith("shutdown"):
            continue  # the header row
        rows.append(
            Row(
                section,
                _date(cells[0]),
                cells[1],
                cells[2],
                cells[3],
                _date(cells[4]),
                line.strip(),
            )
        )
    return rows


def model_ids(cell: str) -> list[str]:
    return [m for m in _ID.findall(cell) if _MODEL_ID.match(m)]


def classify_replacement(cell: str) -> Replacement:
    ticks = _ID.findall(cell)
    models = [t for t in ticks if _MODEL_ID.match(t)]
    settings = [t for t in ticks if not _MODEL_ID.match(t)]
    if not models:
        return Replacement(NONE)
    rest = _ID.sub("", cell)
    rest = re.sub(r"\[[^\]]*\]\([^)]*\)", "", rest)  # links
    words = re.sub(r"[\s(),.]+", " ", rest).strip().lower()
    if len(models) > 1 and set(words.split()) <= {"or"}:
        return Replacement(CHOICE, tuple(models))
    if len(models) == 1 and not words and not settings:
        return Replacement(SINGLE, tuple(models))
    if len(models) == 1:
        return Replacement(WITH_SETTINGS, tuple(models), tuple(settings) or (words,))
    return Replacement(CHOICE, tuple(models))  # several models in prose: still a choice


def records(
    rows: Iterable[Row], *, provider: str = "openai", table: FamilyTable | None = None
) -> list[ApiRecord]:
    table = table or default_table()
    out: list[ApiRecord] = []
    for row in rows:
        if row.section == FEATURES:
            out.append(
                ApiRecord(
                    provider,
                    ENDPOINT_SUNSET,
                    re.sub(r"\s+", " ", row.affected),
                    frozenset(),
                    None,
                    None,
                    classify_replacement(row.replacement).kind,
                    row.shutdown,
                    row.line,
                    False,
                    WHY[ENDPOINT_SUNSET],
                )
            )
            continue
        replacement = classify_replacement(row.replacement)
        for identifier in model_ids(row.affected):
            families, rule = table.of_model(identifier)
            decided = [rule] if rule else []
            target = replacement.models[0] if replacement.models else None
            fixable, reason = False, ""
            if UNKNOWN in families:
                reason = WHY[UNKNOWN]
            elif replacement.kind != SINGLE:
                reason = WHY[replacement.kind]
            else:
                assert target is not None
                new_families, new_rule = table.of_model(target)
                if new_rule:
                    decided.append(new_rule)
                if new_families == families:
                    fixable = True
                else:
                    reason = WHY["family"]
            out.append(
                ApiRecord(
                    provider=provider,
                    kind=MODEL_RETIRED,
                    identifier=identifier,
                    families=families,
                    argument="model",
                    replacement=target if replacement.kind == SINGLE else None,
                    replacement_kind=replacement.kind,
                    shutdown_date=row.shutdown,
                    evidence_quote=row.line,
                    fixable=fixable,
                    reason=reason,
                    decided_by=tuple(decided),
                )
            )
    return out


def parse_page(markdown: str, *, provider: str = "openai") -> list[ApiRecord]:
    return records(parse_rows(markdown), provider=provider)


def changed_records(
    previous: Sequence[ApiRecord] | None, current: Sequence[ApiRecord], *, today: date
) -> list[ApiRecord]:
    """Records that are new or changed since `previous` (F10.3).

    On the first ingestion (`previous` is None) every record whose shutdown date is in
    the future counts as new (F10.7).
    """
    if previous is None:
        return [r for r in current if r.shutdown_date is None or r.shutdown_date >= today]
    before = {r.key(): r.content() for r in previous}
    return [r for r in current if before.get(r.key()) != r.content()]
