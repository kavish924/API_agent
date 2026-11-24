"""Which files a scan reads (spec 04 F4.6, F4.10).

Skipped by default: virtualenvs, installed packages, dependency and build output, VCS
and tool caches. On top of that, `.guardianignore` at the repo root, in gitignore
syntax. Files that never mention the package are dropped before parsing, which is
what keeps a large repo cheap: only the few files that use the SDK are parsed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pathspec

IGNORE_FILE = ".guardianignore"

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "site-packages",
        "dist-packages",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        ".guardian",
    }
)

MAX_FILE_BYTES = 2_000_000  # generated blobs this large are not hand-written SDK calls


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str  # forward slashes, from the repo root
    module: str  # dotted module name, e.g. app.clients


def module_names(relative: str) -> list[str]:
    """Dotted names a file can be imported as. `src/app/x.py` gives `src.app.x` and `app.x`."""
    parts = list(PurePosixPath(relative).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return []
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def _ignore_spec(root: Path) -> pathspec.GitIgnoreSpec | None:
    path = root / IGNORE_FILE
    if not path.is_file():
        return None
    return pathspec.GitIgnoreSpec.from_lines(path.read_text(encoding="utf-8").splitlines())


def _is_venv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file()


def python_files(root: Path) -> Iterator[SourceFile]:
    """Every Python file a scan may read, in a stable order."""
    root = root.resolve()
    spec = _ignore_spec(root)
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        rel_dir = here.relative_to(root).as_posix()
        kept = []
        for name in sorted(dirs):
            if name in SKIP_DIRS or name.endswith(".egg-info") or _is_venv(here / name):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            if spec is not None and spec.match_file(rel + "/"):
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            if spec is not None and spec.match_file(rel):
                continue
            path = here / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            names = module_names(rel)
            yield SourceFile(path=path, relative=rel, module=names[0] if names else "")


def mentions(text: str, words: set[str]) -> bool:
    """Cheap pre-filter: does the text contain any of these identifiers as a whole word?"""
    if not words:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(word) for word in sorted(words)) + r")\b"
    return re.search(pattern, text) is not None
