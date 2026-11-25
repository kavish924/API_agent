"""Type-check service behind the agent's `run_typecheck` tool (spec 11 F11.8).

Interim, static version until the spec 06 sandbox exists (decision 2026-11-25):

- the new SDK version's wheel is unpacked (never installed or imported) and put on
  `MYPYPATH`;
- the repo's Python files are copied to a temporary directory, the candidate edits are
  applied there, and the checkout itself is never touched;
- mypy runs as a subprocess with **no config file** (so no plugins the repo configures),
  no site-packages, missing imports ignored, and a timeout.

No repository code and no build script runs. The result is the errors present after
the edit that were not present before it, compared by file, code and message so a
shifted line number does not count as new (F6.4).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from guardian.common.log import get_logger
from guardian.scan.files import python_files

log = get_logger(__name__)

MAX_COPIED_FILES = 5_000
TIMEOUT_SECONDS = 90

_ERROR = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+): error: (?P<message>.*?)(?:  \[(?P<code>[\w-]+)\])?$"
)


@dataclass(frozen=True, order=True)
class TypeIssue:
    file: str
    line: int
    code: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.code, self.message)

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: error: {self.message}  [{self.code}]"


class TypecheckUnavailable(Exception):
    """The type checker could not run; the message goes back to the model."""


class TypeChecker(Protocol):
    def new_errors(self, before: Mapping[str, str], after: Mapping[str, str]) -> list[TypeIssue]:
        """Errors in `after` that `before` does not have. Keys are repo-relative paths."""
        ...


def parse_errors(output: str) -> list[TypeIssue]:
    errors: list[TypeIssue] = []
    for line in output.splitlines():
        match = _ERROR.match(line.strip())
        if match:
            errors.append(
                TypeIssue(
                    match["file"].replace("\\", "/"),
                    int(match["line"]),
                    match["code"] or "misc",
                    match["message"],
                )
            )
    return errors


def new_errors(before: list[TypeIssue], after: list[TypeIssue]) -> list[TypeIssue]:
    """Errors in `after` beyond those in `before`, counted per (file, code, message)."""
    remaining: dict[tuple[str, str, str], int] = {}
    for error in before:
        remaining[error.key] = remaining.get(error.key, 0) + 1
    out: list[TypeIssue] = []
    for error in sorted(after):
        if remaining.get(error.key, 0):
            remaining[error.key] -= 1
        else:
            out.append(error)
    return out


class StaticTypeChecker:
    """mypy over a temporary copy of the checkout, with the new SDK unpacked beside it."""

    def __init__(self, checkout: Path, sdk_dir: Callable[[], Path]) -> None:
        self.checkout = checkout.resolve()
        self._sdk_dir = sdk_dir  # lazy: the wheel is fetched on the first check only

    def new_errors(self, before: Mapping[str, str], after: Mapping[str, str]) -> list[TypeIssue]:
        try:
            import mypy  # noqa: F401
        except ImportError as exc:
            raise TypecheckUnavailable("mypy is not installed") from exc
        sdk = self._sdk_dir()
        with tempfile.TemporaryDirectory(prefix="guardian-typecheck-") as temp:
            work = Path(temp)
            copy = work / "repo"
            self._copy(copy)
            base = self._run(copy, work, sdk, before)
            fixed = self._run(copy, work, sdk, after)
        return new_errors(base, fixed)

    def _copy(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        for count, source in enumerate(python_files(self.checkout)):
            if count >= MAX_COPIED_FILES:
                break
            target = destination / source.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.path, target)

    def _run(self, copy: Path, work: Path, sdk: Path, files: Mapping[str, str]) -> list[TypeIssue]:
        for relative, text in files.items():
            target = copy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        command = [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            "",  # never the repo's config: it could name plugins, which are code
            "--no-site-packages",
            "--ignore-missing-imports",
            "--follow-imports",
            "silent",
            "--show-error-codes",
            "--no-error-summary",
            "--no-pretty",
            "--hide-error-context",
            "--no-color-output",
            "--cache-dir",
            str(work / "cache"),
            *sorted(files),
        ]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows needs it to start
            "MYPYPATH": str(sdk),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=copy,
                env=environment,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TypecheckUnavailable(
                f"the type checker timed out after {TIMEOUT_SECONDS} s"
            ) from exc
        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            raise TypecheckUnavailable("the type checker failed: " + " ".join(detail))
        return parse_errors(completed.stdout)
