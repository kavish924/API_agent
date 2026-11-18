"""The extraction eval (spec 08 F8.1): run the extractor on labelled notes and score it.

Labels live in `evals/extraction/labels.jsonl`, one note per line. A label lists the
changes the note must yield (`expected`) and changes it must not (`forbidden`, for
example an item marked with a warning sign that only adds values). A note labelled
`complete` lists every change, so it counts towards precision; a partial label counts
towards recall only.

A prediction matches a label when the symbols agree (with or without the package
prefix, or one being a dotted suffix of the other), the change type is one the label
allows, and the parameter agrees when the label names one.

Run it with a model configured:

    python -m guardian.extract.evaluation [labels.jsonl]
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guardian.common.llm import LLMClient
from guardian.extract.extractor import EXTRACTED, extract_note
from guardian.extract.prompt import PROMPT_VERSION
from guardian.extract.schema import ExtractedChange

DEFAULT_LABELS = Path("evals/extraction/labels.jsonl")
RESULTS_DIR = Path("evals/results")

TARGET_PRECISION = 0.85
TARGET_RECALL = 0.80


@dataclass(frozen=True)
class Expected:
    symbols: tuple[str, ...]
    types: tuple[str, ...] = ()  # empty: any change type
    parameter: str | None = None
    replacement: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class Label:
    id: str
    note: Path
    provider: str
    package: str
    version: str
    complete: bool
    expected: tuple[Expected, ...]
    forbidden: tuple[Expected, ...] = ()
    format: str = ""


def load_labels(path: Path = DEFAULT_LABELS) -> list[Label]:
    labels: list[Label] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw: dict[str, Any] = json.loads(line)

        def entries(raw: dict[str, Any], key: str) -> tuple[Expected, ...]:
            return tuple(
                Expected(
                    symbols=tuple(item["symbols"]),
                    types=tuple(item.get("types", ())),
                    parameter=item.get("parameter"),
                    replacement=item.get("replacement"),
                    evidence=item.get("evidence"),
                )
                for item in raw.get(key, [])
            )

        labels.append(
            Label(
                id=raw["id"],
                note=Path(raw["note"]),
                provider=raw["provider"],
                package=raw["package"],
                version=raw["version"],
                complete=raw["complete"],
                expected=entries(raw, "expected"),
                forbidden=entries(raw, "forbidden"),
                format=raw.get("format", ""),
            )
        )
    return labels


def _strip(symbol: str, package: str) -> str:
    text = symbol.strip().strip("`").removesuffix("()")
    return text.removeprefix(package + ".")


def symbols_match(predicted: str, wanted: str, package: str) -> bool:
    a, b = _strip(predicted, package), _strip(wanted, package)
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def matches(record: ExtractedChange, label: Expected, package: str) -> bool:
    if label.types and record.change_type.value not in label.types:
        return False
    symbol_ok = any(symbols_match(record.qualified_symbol, s, package) for s in label.symbols)
    if label.parameter is None:
        return symbol_ok
    # The parameter may be named separately, or written into the symbol itself
    # (`PaymentIntentCreateParams.payment_method_types`).
    named = symbol_ok and record.affected_parameter == label.parameter
    suffix = "." + label.parameter
    stripped = _strip(record.qualified_symbol, package)
    in_symbol = stripped.endswith(suffix) and any(
        symbols_match(stripped.removesuffix(suffix), s, package) for s in label.symbols
    )
    return named or in_symbol


@dataclass
class NoteScore:
    id: str
    complete: bool
    status: str
    predicted: int = 0
    true_positives: int = 0  # predictions that match an expected change
    expected: int = 0
    found: int = 0  # expected changes some prediction matched
    forbidden_hits: int = 0
    rejected: int = 0  # dropped by the evidence check or as duplicates
    missed: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def score_note(
    label: Label, status: str, records: list[ExtractedChange], rejected: int = 0
) -> NoteScore:
    score = NoteScore(
        id=label.id,
        complete=label.complete,
        status=status,
        predicted=len(records),
        expected=len(label.expected),
        rejected=rejected,
    )
    for wanted in label.expected:
        if any(matches(record, wanted, label.package) for record in records):
            score.found += 1
        else:
            score.missed.append(wanted.symbols[0])
    for record in records:
        if any(matches(record, wanted, label.package) for wanted in label.expected):
            score.true_positives += 1
        else:
            score.unmatched.append(f"{record.change_type.value} {record.qualified_symbol}")
        if any(matches(record, bad, label.package) for bad in label.forbidden):
            score.forbidden_hits += 1
    return score


@dataclass
class Summary:
    notes: int
    failed: int
    recall: float | None
    precision: float | None  # over completely labelled notes only
    forbidden_hits: int
    rejected: int
    meets_targets: bool


def summarise(scores: list[NoteScore]) -> Summary:
    expected = sum(s.expected for s in scores)
    found = sum(s.found for s in scores)
    complete = [s for s in scores if s.complete]
    predicted = sum(s.predicted for s in complete)
    true_positives = sum(s.true_positives for s in complete)
    recall = found / expected if expected else None
    precision = true_positives / predicted if predicted else None
    forbidden = sum(s.forbidden_hits for s in scores)
    return Summary(
        notes=len(scores),
        failed=sum(s.status != EXTRACTED for s in scores),
        recall=recall,
        precision=precision,
        forbidden_hits=forbidden,
        rejected=sum(s.rejected for s in scores),
        meets_targets=(recall or 0.0) >= TARGET_RECALL
        and (precision is None or precision >= TARGET_PRECISION)
        and forbidden == 0,
    )


def run_eval(llm: LLMClient, labels: list[Label], root: Path = Path(".")) -> dict[str, Any]:
    """Extract every labelled note and score it. Makes real model calls."""
    scores: list[NoteScore] = []
    for label in labels:
        note = (root / label.note).read_text(encoding="utf-8")
        result = extract_note(llm, label.provider, label.package, label.version, note)
        scores.append(score_note(label, result.status, result.records, len(result.rejected)))
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": llm.default_model,
        "provider": llm.provider.name,
        "prompt_version": PROMPT_VERSION,
        "dataset": str(DEFAULT_LABELS),
        "dataset_notes": len(labels),
        "targets": {"precision": TARGET_PRECISION, "recall": TARGET_RECALL},
        "summary": asdict(summarise(scores)),
        "notes": [asdict(score) for score in scores],
    }


def main(argv: list[str] | None = None) -> int:
    from guardian.common.config import get_settings
    from guardian.common.context import build_context
    from guardian.extract.extractor import llm_available

    args = argv if argv is not None else sys.argv[1:]
    labels = load_labels(Path(args[0]) if args else DEFAULT_LABELS)
    ctx = build_context(get_settings())
    if not llm_available(ctx.llm):
        print("No LLM provider is configured (GUARDIAN_LLM_PROVIDER); the eval needs a real model.")
        return 2
    report = run_eval(ctx.llm, labels)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"extraction-{stamp}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=1))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
