"""Interim static verification, L1 and L2 (decision 2026-11-25, spec 07; replaces nothing
in spec 06, which the Docker sandbox implements later).

No repository code runs:

- **L1**: every changed Python file compiles (`compile()` parses and builds bytecode;
  nothing is executed), and Ruff's syntax and undefined-name rules report nothing new
  compared with the unpatched files. Ruff runs isolated: the repo's config is not read.
- **L2**: the static mypy check (spec 11) reports no type error in the patched files,
  checked against the **new** SDK, that the unpatched files did not have against the
  **old** one (F6.4). Errors at call sites a partial patch leaves for a person are
  listed as remaining work, not as an L2 failure (F6.16).

There is no L3: tests are not run until the sandbox exists, and the PR says so.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from guardian.agent.typecheck import TypeChecker, TypecheckUnavailable, TypeIssue

L0 = "L0"  # nothing verified: the patch does not even compile, or could not be checked
L1 = "L1"
L2 = "L2"

UNVERIFIED = "unverified"
TESTS_NOT_RUN = "tests were not run: interim static verification (spec 06 sandbox not built)"

# Syntax errors and names that do not exist: what a broken edit looks like to Ruff.
RUFF_RULES = "E9,F63,F7,F82"
RUFF_TIMEOUT = 60
# What a subprocess needs to start, and nothing else.
_MINIMAL_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows needs it to start
}


@dataclass
class Verification:
    level: str
    compile_errors: list[str] = field(default_factory=list)
    lint_errors: list[str] = field(default_factory=list)  # new compared with base
    type_errors: list[str] = field(default_factory=list)  # new compared with base
    remaining: list[str] = field(default_factory=list)  # at report-only call sites (F6.16)
    notes: list[str] = field(default_factory=lambda: [TESTS_NOT_RUN])

    @property
    def verified(self) -> bool:
        return self.level != L0


def _compile_errors(files: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    for path, text in sorted(files.items()):
        try:
            compile(text, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: {exc.msg}")
        except ValueError as exc:  # null bytes
            errors.append(f"{path}: {exc}")
    return errors


def _ruff(files: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """(file, code, message) for every Ruff finding in `files`, from an isolated run."""
    if not files:
        return []
    with tempfile.TemporaryDirectory(prefix="guardian-ruff-") as temp:
        root = Path(temp)
        for path, text in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        command = [
            sys.executable,
            "-I",  # isolated: the folder of untrusted files is never on sys.path
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            RUFF_RULES,
            "--output-format",
            "json",
            *sorted(files),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=root,
                env=_MINIMAL_ENV,  # no secrets from the worker's environment
                capture_output=True,
                text=True,
                timeout=RUFF_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TypecheckUnavailable(f"ruff could not run: {exc}") from exc
        if completed.returncode not in (0, 1):
            raise TypecheckUnavailable("ruff failed: " + completed.stderr.strip()[-200:])
        # Paths are made relative while the files still exist, so `resolve()` sees the
        # same real path for the temp root and for Ruff's absolute file names.
        base = root.resolve()
        out: list[tuple[str, str, str]] = []
        for item in json.loads(completed.stdout or "[]"):
            name = Path(item["filename"])
            try:
                relative = name.resolve().relative_to(base).as_posix()
            except ValueError:  # not under the temp root: keep Ruff's name
                relative = name.as_posix()
            out.append((relative, str(item.get("code")), str(item.get("message"))))
    return out


def _new(
    before: Sequence[tuple[str, str, str]], after: Sequence[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    remaining: dict[tuple[str, str, str], int] = {}
    for key in before:
        remaining[key] = remaining.get(key, 0) + 1
    out: list[tuple[str, str, str]] = []
    for key in after:
        if remaining.get(key, 0):
            remaining[key] -= 1
        else:
            out.append(key)
    return out


def after_lines(before: str, after: str, lines: Sequence[int]) -> set[int]:
    """Where lines of `before` sit in `after`, through a line diff.

    An unchanged line maps one to one; a line inside a replaced block maps to every line
    of its replacement; a deleted line maps to nothing.
    """
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    opcodes = matcher.get_opcodes()
    out: set[int] = set()
    for line in lines:
        index = line - 1
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 <= index < i2:
                if tag == "equal":
                    out.add(j1 + (index - i1) + 1)
                else:
                    out.update(range(j1 + 1, j2 + 1))
                break
    return out


def verify_static(
    changed: Mapping[str, tuple[str, str]],
    typechecker: TypeChecker | None,
    *,
    report_only_lines: Mapping[str, Sequence[int]] | None = None,
) -> Verification:
    """Verify the Python files of a patch. `changed` maps path -> (before, after).

    `report_only_lines` are the lines, in the **before** text, of call sites the patch
    leaves for a person; type errors there are remaining work, not an L2 failure.
    """
    python = {path: texts for path, texts in changed.items() if path.endswith(".py")}
    after = {path: texts[1] for path, texts in python.items()}
    before = {path: texts[0] for path, texts in python.items()}

    result = Verification(level=L0)
    result.compile_errors = _compile_errors(after)
    if result.compile_errors:
        return result
    try:
        lint = _new(_ruff(before), _ruff(after))
    except TypecheckUnavailable as exc:
        result.notes.append(f"L1 lint not checked: {exc}")
        return result
    result.lint_errors = [f"{f}: {code} {message}" for f, code, message in lint]
    if result.lint_errors:
        return result
    result.level = L1

    if typechecker is None:
        result.notes.append("L2 not checked: no type checker in this run")
        return result
    try:
        issues: list[TypeIssue] = typechecker.new_errors(before, after) if python else []
    except TypecheckUnavailable as exc:
        result.notes.append(f"L2 not checked: {exc}")
        return result
    left: dict[str, set[int]] = {}
    for path, lines in (report_only_lines or {}).items():
        texts = changed.get(path)
        # Findings carry before-text lines; mypy reports after-text lines.
        left[path] = after_lines(texts[0], texts[1], lines) if texts else set(lines)
    for issue in issues:
        if issue.line in left.get(issue.file, set()):
            result.remaining.append(str(issue))
        else:
            result.type_errors.append(str(issue))
    if not result.type_errors:
        result.level = L2
    return result
