"""The scanner eval harness (spec 08 F8.2–F8.4).

The real eval needs the real SDK wheels, so it runs only when GUARDIAN_TEST_WHEELS
points at unpacked wheels (as for tests/test_apidiff_real.py). The harness itself,
the expectation markers and the scoring are tested here without them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from guardian.scan.evaluation import (
    GREP,
    SCANNER,
    SPELLING,
    Case,
    Counts,
    load_cases,
    run_eval,
    wheel_loader,
)

ROOT = Path(__file__).parent.parent
WHEELS = os.environ.get("GUARDIAN_TEST_WHEELS")


def test_expect_markers_are_read_per_line() -> None:
    case = Case(
        "x",
        "stripe",
        "p",
        True,
        {"a.py": "x = 1  # expect\ny = 2\nz = 3  # expect: the call\n", "b.py": "# expect\n"},
    )
    assert case.expected() == {("a.py", 1), ("a.py", 3), ("b.py", 1)}


def test_counts() -> None:
    counts = Counts()
    counts.add({("a", 1), ("a", 2)}, {("a", 1), ("a", 9)})
    counts.add(set(), {("b", 1)})  # a negative case with one false finding
    assert (counts.found, counts.expected, counts.predicted) == (1, 2, 3)
    assert counts.recall == 0.5 and counts.precision == pytest.approx(1 / 3)
    assert Counts().recall is None and Counts().precision is None


def test_the_case_file_is_well_formed() -> None:
    sdks, cases = load_cases(ROOT / "evals/scanner/cases.py")
    assert len(cases) >= 25
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.sdk in sdks
        if case.negative:
            assert case.expected() == set(), case.id
        else:
            assert case.expected(), f"{case.id} marks no line"
    assert sum(case.negative for case in cases) >= 5
    assert any(not case.in_scope for case in cases)


@pytest.mark.skipif(not WHEELS, reason="GUARDIAN_TEST_WHEELS is not set")
def test_scanner_eval_meets_its_targets_on_real_wheels() -> None:
    sdks, cases = load_cases(ROOT / "evals/scanner/cases.py")
    report = run_eval(sdks, cases, wheel_loader(Path(WHEELS or "")))
    in_scope = report["results"]["in_scope"]
    print("\n", {config: (s["recall"], s["precision"]) for config, s in in_scope.items()})
    assert report["meets_targets"]
    # The real scanner beats both baselines on precision.
    assert in_scope[SCANNER]["precision"] > in_scope[SPELLING]["precision"]
    assert in_scope[SCANNER]["precision"] > in_scope[GREP]["precision"]
