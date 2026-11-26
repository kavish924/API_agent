"""Interim static verification, L1 and L2 (decision 2026-11-25, spec 07)."""

from __future__ import annotations

from pathlib import Path

from guardian.agent.typecheck import StaticTypeChecker
from guardian.verify.static import L0, L1, L2, TESTS_NOT_RUN, verify_static


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


BEFORE = "import fakesdk\n\nx = fakesdk.get(id='k')\ny = fakesdk.old()\n"


def checker(tmp_path: Path) -> StaticTypeChecker:
    old = tree(
        tmp_path / "old",
        {
            "fakesdk/__init__.py": "def get(id: str) -> str: ...\ndef old() -> int: ...\n",
            "fakesdk/py.typed": "",
        },
    )
    new = tree(
        tmp_path / "new",
        {"fakesdk/__init__.py": "def get(id: str, /) -> str: ...\n", "fakesdk/py.typed": ""},
    )
    repo = tree(
        tmp_path / "repo",
        {"app.py": BEFORE, "pyproject.toml": "[tool.ruff.lint]\nselect = ['ALL']\n"},
    )
    return StaticTypeChecker(repo, lambda: new, base_sdk_dir=lambda: old)


def test_a_correct_fix_reaches_l2_and_says_tests_did_not_run(tmp_path: Path) -> None:
    after = "import fakesdk\n\nx = fakesdk.get('k')\n"
    result = verify_static({"app.py": (BEFORE, after)}, checker(tmp_path))
    assert result.level == L2, result
    assert result.notes == [TESTS_NOT_RUN]


def test_a_syntax_error_is_l0(tmp_path: Path) -> None:
    result = verify_static({"app.py": (BEFORE, "x = (\n")}, checker(tmp_path))
    assert result.level == L0 and result.compile_errors and not result.verified


def test_a_new_undefined_name_is_l0(tmp_path: Path) -> None:
    after = BEFORE.replace("fakesdk.get(id='k')", "fakesdk.get(idx)")
    result = verify_static({"app.py": (BEFORE, after)}, checker(tmp_path))
    assert result.level == L0
    assert result.lint_errors == ["app.py: F821 Undefined name `idx`"]


def test_a_call_left_broken_by_the_new_sdk_stops_at_l1(tmp_path: Path) -> None:
    after = BEFORE.replace("y = fakesdk.old()\n", "")  # `get(id=...)` is still keyword
    result = verify_static({"app.py": (BEFORE, after)}, checker(tmp_path))
    assert result.level == L1
    assert any("app.py:3" in e for e in result.type_errors)


def test_errors_at_report_only_call_sites_are_remaining_work(tmp_path: Path) -> None:
    after = BEFORE.replace("fakesdk.get(id='k')", "fakesdk.get('k')")  # `old()` still gone
    result = verify_static(
        {"app.py": (BEFORE, after)}, checker(tmp_path), report_only_lines={"app.py": [4]}
    )
    assert result.level == L2
    assert len(result.remaining) == 1 and "app.py:4" in result.remaining[0]


def test_the_repo_ruff_config_is_not_used(tmp_path: Path) -> None:
    # The repo selects every rule; an isolated run only checks syntax and names.
    sloppy = "import fakesdk\n\nx=fakesdk.get( 'k' )\n"
    result = verify_static({"app.py": (BEFORE, sloppy)}, checker(tmp_path))
    assert result.lint_errors == [] and result.level == L2


def test_without_a_type_checker_l1_is_the_limit(tmp_path: Path) -> None:
    result = verify_static({"app.py": (BEFORE, BEFORE.replace("'k'", "'j'"))}, None)
    assert result.level == L1 and "L2 not checked" in result.notes[-1]


def test_report_only_lines_follow_the_edit() -> None:
    from guardian.verify.static import after_lines

    before = "a\nb\nc\nd\n"
    after = "a\nb1\nb2\nb3\nc\nd\n"  # line 2 became three lines
    assert after_lines(before, after, [1, 3, 4]) == {1, 5, 6}
    assert after_lines(before, after, [2]) == {2, 3, 4}
    assert after_lines("a\nb\n", "a\n", [2]) == set()


def test_a_new_error_above_a_shifted_report_only_line_is_not_hidden(tmp_path: Path) -> None:
    # `old()` (line 4) stays for a person; the fix grows line 3 into three lines, so it
    # sits on line 6 after the patch, and a new error on line 4 must still fail L2.
    after = BEFORE.replace(
        "x = fakesdk.get(id='k')\n", "x = fakesdk.get(\n    'k'\n)\nz: int = fakesdk.get('q')\n"
    )
    result = verify_static(
        {"app.py": (BEFORE, after)}, checker(tmp_path), report_only_lines={"app.py": [4]}
    )
    assert result.level == L1, result
    assert any("app.py:6" in e for e in result.type_errors)
    assert any("app.py:7" in e for e in result.remaining)
