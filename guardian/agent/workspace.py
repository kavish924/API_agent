"""The agent's read-only view of the scanned checkout (spec 11 F11.4, F11.5, F11.11–F11.14).

Every path goes through `resolve`: it is normalised, must stay inside the checkout
(absolute paths, `..` escapes and symlinks leading out are refused), and secret-looking
files are denied by name. Nothing here writes to the checkout; candidate edits live in
memory, as text.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from guardian.scan.files import SKIP_DIRS

MAX_RESULT_CHARS = 8_000  # every tool result is capped (F11.14)
MAX_READ_LINES = 200
MAX_PATTERN_CHARS = 200
MAX_SEARCH_MATCHES = 50
MAX_SEARCH_SECONDS = 3.0
MAX_SEARCH_LINE_CHARS = 1_000
MAX_SEARCH_FILE_BYTES = 1_000_000

# Secret-looking files, denied by name in every tool (F11.13).
DENIED_NAMES = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "*credentials*.json",
    "service-account*.json",
    "secrets.*",
    "*.secret",
    "*.secrets",
    ".htpasswd",
)
DENIED_DIRS = (".git", ".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config")


class Refused(Exception):
    """A tool request the workspace will not serve. The message goes back to the model."""


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated: {len(text) - limit} more characters]"


def denied(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if any(part in DENIED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return any(fnmatch.fnmatch(name, pattern) for pattern in DENIED_NAMES)


@dataclass
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def resolve(self, path: str) -> tuple[Path, str]:
        """(absolute path, repo-relative posix path) for a path the model gave."""
        if not path or "\x00" in path:
            raise Refused("refused: empty or invalid path")
        posix = path.replace("\\", "/")
        if posix.startswith("/") or re.match(r"^[A-Za-z]:", posix) or posix.startswith("~"):
            raise Refused(f"refused: {path!r} is absolute; use a path inside the repository")
        normal = os.path.normpath(posix).replace("\\", "/")
        if normal == ".." or normal.startswith("../"):
            raise Refused(f"refused: {path!r} leads outside the repository")
        target = (self.root / normal).resolve()  # follows symlinks
        if not target.is_relative_to(self.root):
            raise Refused(f"refused: {path!r} leads outside the repository")
        relative = target.relative_to(self.root).as_posix()
        if denied(relative):
            raise Refused(f"refused: {relative!r} may hold secrets and is never read")
        return target, relative

    def read_text(self, path: str) -> tuple[str, str]:
        target, relative = self.resolve(path)
        if not target.is_file():
            raise Refused(f"no such file: {relative}")
        return relative, target.read_bytes().decode("utf-8", errors="replace")

    # --- tools -----------------------------------------------------------------------

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        """Numbered lines `start`..`end` of a file (F11.4)."""
        relative, text = self.read_text(path)
        lines = text.splitlines()
        start = max(1, int(start))
        last = start + MAX_READ_LINES - 1
        end = min(int(end) if end is not None else last, last, len(lines))
        if start > len(lines):
            return f"{relative} has {len(lines)} lines"
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n[{relative}: lines {start}-{end} of {len(lines)}]"
        return truncate(body + more)

    def search(self, pattern: str, *, glob: str = "*.py") -> str:
        """Regex search over the checkout: file:line: excerpt (F11.5)."""
        if not pattern or len(pattern) > MAX_PATTERN_CHARS:
            raise Refused(f"refused: the pattern must be 1-{MAX_PATTERN_CHARS} characters")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise Refused(f"invalid regular expression: {exc}") from exc
        started = time.monotonic()
        matches: list[str] = []
        timed_out = False
        for path, relative in self._files(glob):
            if time.monotonic() - started > MAX_SEARCH_SECONDS:
                timed_out = True
                break
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line[:MAX_SEARCH_LINE_CHARS]):
                    matches.append(f"{relative}:{number}: {line.strip()[:160]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
        if not matches:
            result = "no matches"
        else:
            result = "\n".join(matches)
            if len(matches) >= MAX_SEARCH_MATCHES:
                result += f"\n[stopped at {MAX_SEARCH_MATCHES} matches]"
        if timed_out:
            result += f"\n[search stopped after {MAX_SEARCH_SECONDS:.0f} s]"
        return truncate(result)

    def _files(self, glob: str) -> list[tuple[Path, str]]:
        found: list[tuple[Path, str]] = []
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            here = Path(directory)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in DENIED_DIRS)
            for name in sorted(files):
                path = here / name
                relative = path.relative_to(self.root).as_posix()
                if path.is_symlink() or denied(relative):
                    continue
                if fnmatch.fnmatch(name, glob):
                    found.append((path, relative))
        return found
