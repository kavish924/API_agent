"""Read the version a repository pins for a package (spec 01 F1.10–F1.12).

Supports `requirements*.txt`, `pyproject.toml` (PEP 621 and Poetry), `poetry.lock` and
`uv.lock`. A lock file wins over a manifest. Files are reached through `RepoFiles`, so
the same code reads a local checkout now and the GitHub contents API later (spec 07).
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

LOCK_FILES = ("uv.lock", "poetry.lock")
PYPROJECT = "pyproject.toml"
REQUIREMENTS_GLOB = "requirements*.txt"

PINNED = "pinned"
NOT_USED = "package-not-used"
NOT_PINNED = "version-not-pinned"
CONFLICT = "version-conflict"


class RepoFiles(Protocol):
    def list_root(self) -> list[str]:
        """Names of the files in the repository root."""
        ...

    def read_text(self, path: str) -> str | None:
        """Text of a file by repo-relative path, or None if it does not exist."""
        ...


class LocalRepoFiles:
    """`RepoFiles` over a local checkout. Never reads outside the root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list_root(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_file())

    def read_text(self, path: str) -> str | None:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class PinLocation:
    """One place a package is declared. `version` is set only for an exact pin."""

    file: str
    line: int | None
    spec: str
    version: str | None


@dataclass(frozen=True)
class PinResult:
    status: str  # PINNED | NOT_USED | NOT_PINNED | CONFLICT
    version: str | None = None
    decided_by: str | None = None
    locations: tuple[PinLocation, ...] = ()


def _normalise(version: str) -> str:
    try:
        return str(Version(version))
    except InvalidVersion:
        return version


def _exact(specifier: SpecifierSet) -> str | None:
    items = list(specifier)
    if len(items) != 1:
        return None
    only = items[0]
    if only.operator not in ("==", "===") or only.version.endswith(".*"):
        return None
    return _normalise(only.version)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations; keep the line number where each one starts."""
    out: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not buffer:
            start = number
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        out.append((start, buffer + line))
        buffer = ""
    if buffer:
        out.append((start, buffer))
    return out


def parse_requirements(
    text: str,
    package: str,
    file: str,
    files: RepoFiles | None = None,
    _seen: set[str] | None = None,
) -> list[PinLocation]:
    """Declarations of `package` in a requirements file, following `-r` includes."""
    name = canonicalize_name(package)
    seen = _seen if _seen is not None else {file}
    out: list[PinLocation] = []
    for number, raw in _logical_lines(text):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        include = re.match(r"^(?:-r|--requirement)[=\s]\s*(\S+)", line)
        if include:
            # Normalised, so a cycle through `../` is recognised as already seen.
            included = posixpath.normpath(str(PurePosixPath(file).parent / include.group(1)))
            if files is not None and included not in seen:
                seen.add(included)
                sub = files.read_text(included)
                if sub is not None:
                    out.extend(parse_requirements(sub, package, included, files, seen))
            continue
        if line.startswith("-"):
            continue  # other pip options: --index-url, -e, -c ...
        line = re.split(r"\s--hash[=\s]", line)[0].strip()
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) != name:
            continue
        if requirement.url:
            out.append(PinLocation(file, number, f"@ {requirement.url}", None))
        else:
            spec = str(requirement.specifier)
            out.append(PinLocation(file, number, spec, _exact(requirement.specifier)))
    return out


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _poetry_exact(spec: str) -> str | None:
    """Poetry treats a bare version, or `==version`, as an exact pin."""
    candidate = spec.strip().removeprefix("==").strip()
    if not candidate or re.search(r"[\^~<>!*,|\s]", candidate):
        return None
    try:
        return str(Version(candidate))
    except InvalidVersion:
        return None


def parse_pyproject(text: str, package: str, file: str = PYPROJECT) -> list[PinLocation]:
    """Declarations of `package` in PEP 621 tables, dependency groups and Poetry tables."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    name = canonicalize_name(package)
    out: list[PinLocation] = []

    project = _table(data.get("project"))
    pep508: list[str] = _strings(project.get("dependencies"))
    for group in _table(project.get("optional-dependencies")).values():
        pep508 += _strings(group)
    for group in _table(data.get("dependency-groups")).values():
        pep508 += _strings(group)
    for item in pep508:
        try:
            requirement = Requirement(item)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) != name:
            continue
        if requirement.url:
            out.append(PinLocation(file, None, f"@ {requirement.url}", None))
        else:
            spec = str(requirement.specifier)
            out.append(PinLocation(file, None, spec, _exact(requirement.specifier)))

    poetry = _table(_table(data.get("tool")).get("poetry"))
    tables = [_table(poetry.get("dependencies")), _table(poetry.get("dev-dependencies"))]
    tables += [_table(_table(g).get("dependencies")) for g in _table(poetry.get("group")).values()]
    for table in tables:
        for key, value in table.items():
            if canonicalize_name(key) != name:
                continue
            declared = value if isinstance(value, str) else _table(value).get("version")
            if isinstance(declared, str):
                out.append(PinLocation(file, None, declared, _poetry_exact(declared)))
            else:
                out.append(PinLocation(file, None, "(no version)", None))
    return out


def parse_lock(text: str, package: str, file: str) -> list[PinLocation]:
    """The resolved version of `package` in `uv.lock` or `poetry.lock`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    name = canonicalize_name(package)
    out: list[PinLocation] = []
    packages = data.get("package")
    for entry in packages if isinstance(packages, list) else []:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if canonicalize_name(str(entry.get("name", ""))) == name and isinstance(version, str):
            out.append(PinLocation(file, None, f"=={version}", _normalise(version)))
    return out


def _decide(locations: list[PinLocation]) -> PinResult:
    exact = [loc for loc in locations if loc.version is not None]
    versions = {loc.version for loc in exact}
    if not exact:
        return PinResult(NOT_PINNED, locations=tuple(locations))
    if len(versions) > 1:
        return PinResult(CONFLICT, locations=tuple(locations))
    return PinResult(PINNED, exact[0].version, exact[0].file, tuple(locations))


def find_pin(files: RepoFiles, package: str) -> PinResult:
    """The version of `package` this repository pins, and where that was decided."""
    root = files.list_root()

    locked: list[PinLocation] = []
    for lock in LOCK_FILES:
        if lock in root:
            locked += parse_lock(files.read_text(lock) or "", package, lock)
    if locked:
        return _decide(locked)

    declared: list[PinLocation] = []
    for name in root:
        if fnmatch.fnmatch(name, REQUIREMENTS_GLOB):
            declared += parse_requirements(files.read_text(name) or "", package, name, files)
    if PYPROJECT in root:
        declared += parse_pyproject(files.read_text(PYPROJECT) or "", package)
    if not declared:
        return PinResult(NOT_USED)
    return _decide(declared)
