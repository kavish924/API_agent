"""The scanner eval (spec 08 F8.2–F8.4).

Each case is a small repository that uses a real SDK in one pattern, with the lines
that should be found marked `# expect`. The cases live in `evals/scanner/cases.py`.
A case is either **in scope** (the scanner is meant to handle the pattern) or a
**known limit** (wrapped clients, dynamic calls), and the two are scored separately.
Negative cases use the SDK without touching anything that changed; any finding in one
counts against precision.

Three configurations are scored on the same cases and changes: the real scanner,
libcst matching by spelling only, and grep.

    python -m guardian.scan.evaluation [--wheels DIR]

`--wheels` points at already unpacked wheels named `<package>-<version>/`; without it
the wheels are fetched from PyPI once and cached in the blob store.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guardian.apidiff.compare import compare
from guardian.apidiff.reconcile import GradedChange, reconcile
from guardian.apidiff.surface import Surface, build_surface
from guardian.scan.baselines import Words, grep_lines, spelling_lines
from guardian.scan.rules import build_index
from guardian.scan.scanner import scan_repo

CASES_FILE = Path("evals/scanner/cases.py")
RESULTS_DIR = Path("evals/results")
EXPECT = "# expect"
EXPECT_MARKER = re.compile(r"#\s*expect\b")  # `# expect`, optionally followed by a note

TARGET_RECALL = 0.85
TARGET_PRECISION = 0.90

SCANNER, SPELLING, GREP = "scanner", "libcst-by-name", "grep"

Line = tuple[str, int]


@dataclass(frozen=True)
class Case:
    id: str
    sdk: str  # key into SDKS in the cases file
    pattern: str
    in_scope: bool
    files: dict[str, str]
    negative: bool = False

    def expected(self) -> set[Line]:
        found: set[Line] = set()
        for name, text in self.files.items():
            for number, line in enumerate(text.splitlines(), 1):
                if EXPECT_MARKER.search(line):
                    found.add((name, number))
        return found


@dataclass
class Counts:
    expected: int = 0
    true_positives: int = 0
    predicted: int = 0
    found: int = 0

    def add(self, expected: set[Line], predicted: set[Line]) -> None:
        self.expected += len(expected)
        self.predicted += len(predicted)
        self.found += len(expected & predicted)
        self.true_positives += len(expected & predicted)

    @property
    def recall(self) -> float | None:
        return self.found / self.expected if self.expected else None

    @property
    def precision(self) -> float | None:
        return self.true_positives / self.predicted if self.predicted else None


@dataclass
class CaseScore:
    id: str
    pattern: str
    in_scope: bool
    negative: bool
    expected: list[Line]
    predicted: dict[str, list[Line]] = field(default_factory=dict)


def load_cases(path: Path = CASES_FILE) -> tuple[dict[str, dict[str, str]], list[Case]]:
    spec = importlib.util.spec_from_file_location("guardian_scanner_cases", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SDKS, module.CASES


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


SurfaceLoader = Callable[[str, str], Surface]


def wheel_loader(wheels: Path) -> SurfaceLoader:
    def load(package: str, version: str) -> Surface:
        return build_surface(wheels / f"{package}-{version}", package, version)

    return load


def run_eval(
    sdks: dict[str, dict[str, str]], cases: Sequence[Case], load_surface: SurfaceLoader
) -> dict[str, Any]:
    graded: dict[str, tuple[Surface, list[GradedChange]]] = {}
    for key, sdk in sdks.items():
        old = load_surface(sdk["package"], sdk["old"])
        new = load_surface(sdk["package"], sdk["new"])
        graded[key] = (old, reconcile([], old, new, compare(old, new)).changes)

    groups = {
        name: {SCANNER: Counts(), SPELLING: Counts(), GREP: Counts()}
        for name in ("in_scope", "known_limit")
    }
    scores: list[CaseScore] = []
    for case in cases:
        sdk = sdks[case.sdk]
        old, changes = graded[case.sdk]
        words = Words.from_index(build_index(changes, old))
        expected = case.expected()
        with tempfile.TemporaryDirectory(prefix="guardian-case-") as temp:
            root = Path(temp)
            _write(root, case.files)
            predicted = {
                SCANNER: {
                    (f.file, f.line) for f in scan_repo(root, sdk["package"], old, changes).findings
                },
                SPELLING: spelling_lines(root, words),
                GREP: grep_lines(root, words),
            }
        group = groups["in_scope" if case.in_scope else "known_limit"]
        for config, lines in predicted.items():
            group[config].add(expected, lines)
        scores.append(
            CaseScore(
                id=case.id,
                pattern=case.pattern,
                in_scope=case.in_scope,
                negative=case.negative,
                expected=sorted(expected),
                predicted={config: sorted(lines) for config, lines in predicted.items()},
            )
        )

    def summary(counts: Counts) -> dict[str, Any]:
        return {**asdict(counts), "recall": counts.recall, "precision": counts.precision}

    in_scope = groups["in_scope"][SCANNER]
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sdks": sdks,
        "cases": len(cases),
        "negative_cases": sum(case.negative for case in cases),
        "targets": {"recall": TARGET_RECALL, "precision": TARGET_PRECISION},
        "results": {
            group: {config: summary(c) for config, c in configs.items()}
            for group, configs in groups.items()
        },
        "meets_targets": (in_scope.recall or 0.0) >= TARGET_RECALL
        and (in_scope.precision or 0.0) >= TARGET_PRECISION,
        "per_case": [asdict(score) for score in scores],
    }


def _table(report: dict[str, Any]) -> str:
    rows = [
        "| Cases | Configuration | Recall | Precision | Found / expected | Flagged |",
        "|---|---|---|---|---|---|",
    ]
    for group, configs in report["results"].items():
        for config, s in configs.items():
            recall = "n/a" if s["recall"] is None else f"{s['recall']:.2f}"
            precision = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
            name = group.replace("_", " ")
            found = f"{s['found']} / {s['expected']}"
            rows.append(
                f"| {name} | {config} | {recall} | {precision} | {found} | {s['predicted']} |"
            )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    sdks, cases = load_cases()
    if "--wheels" in args:
        loader = wheel_loader(Path(args[args.index("--wheels") + 1]))
    else:
        from guardian.apidiff.service import ApiDiffService
        from guardian.common.config import get_settings
        from guardian.common.context import build_context
        from guardian.watch.sources import source_for_package

        ctx = build_context(get_settings())
        service = ApiDiffService.from_context(ctx)

        def loader(package: str, version: str) -> Surface:
            source = source_for_package(package)
            assert source is not None, package
            return service.surface(source, version)

    report = run_eval(sdks, cases, loader)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"scanner-{stamp}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(_table(report))
    print(f"\nmeets targets: {report['meets_targets']}\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
