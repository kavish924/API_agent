"""Dependency-file parsers and pin resolution. Covers F1.10, F1.11, F1.12."""

from __future__ import annotations

from pathlib import Path

import pytest

from guardian.watch.depfiles import (
    CONFLICT,
    NOT_PINNED,
    NOT_USED,
    PINNED,
    LocalRepoFiles,
    find_pin,
    parse_lock,
    parse_pyproject,
    parse_requirements,
)


def _repo(tmp_path: Path, **files: str) -> LocalRepoFiles:
    for name, text in files.items():
        path = tmp_path / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return LocalRepoFiles(tmp_path)


# --- requirements.txt ---------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "version"),
    [
        ("openai==1.30.0", "1.30.0"),
        ("openai == 1.30.0  # pinned for the migration", "1.30.0"),
        ("OpenAI==1.30.0", "1.30.0"),
        ("openai[datalib]==1.30.0", "1.30.0"),
        ('openai==1.30.0 ; python_version >= "3.9"', "1.30.0"),
        ("openai==1.30.0 --hash=sha256:abc123", "1.30.0"),
        ("openai===1.30.0", "1.30.0"),
        ("openai==1.30", "1.30"),
        ("openai>=1.0", None),
        ("openai~=1.30", None),
        ("openai==1.*", None),
        ("openai>=1.0,<2", None),
        ("openai", None),
    ],
)
def test_requirements_line_forms(line: str, version: str | None) -> None:
    (location,) = parse_requirements(line, "openai", "requirements.txt")
    assert location.version == version
    assert location.line == 1


def test_requirements_ignores_other_packages_options_and_junk() -> None:
    text = "\n".join(
        [
            "# comment",
            "--index-url https://pypi.org/simple",
            "-e .",
            "openai-agents==0.1.0",
            "stripe==9.0.0",
            "this is not a requirement !!",
            "",
        ]
    )
    assert parse_requirements(text, "openai", "requirements.txt") == []
    (stripe,) = parse_requirements(text, "stripe", "requirements.txt")
    assert (stripe.version, stripe.line) == ("9.0.0", 5)


def test_requirements_line_continuation_keeps_the_starting_line() -> None:
    text = "requests==2.0\nopenai==1.30.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\n"
    (location,) = parse_requirements(text, "openai", "requirements.txt")
    assert (location.version, location.line) == ("1.30.0", 2)


def test_requirements_url_install_is_not_a_pin() -> None:
    (location,) = parse_requirements(
        "openai @ git+https://github.com/openai/openai-python@main", "openai", "requirements.txt"
    )
    assert location.version is None
    assert location.spec.startswith("@ ")


def test_requirements_follows_includes_once(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{
            "requirements.txt": "-r requirements/base.txt\nrequests==2.0\n",
            "requirements__base.txt": "-r ../requirements.txt\nopenai==1.30.0\n",
        },
    )
    locations = parse_requirements(
        files.read_text("requirements.txt") or "", "openai", "requirements.txt", files
    )
    assert [(loc.file, loc.version) for loc in locations] == [("requirements/base.txt", "1.30.0")]


# --- pyproject.toml -----------------------------------------------------------


def test_pyproject_pep621_dependencies_and_groups() -> None:
    text = """
[project]
dependencies = ["openai==1.30.0", "httpx>=0.27"]

[project.optional-dependencies]
payments = ["stripe>=9"]

[dependency-groups]
dev = ["pytest", {include-group = "lint"}]
"""
    (openai,) = parse_pyproject(text, "openai")
    assert openai.version == "1.30.0"
    (stripe,) = parse_pyproject(text, "stripe")
    assert stripe.version is None and stripe.spec == ">=9"


@pytest.mark.parametrize(
    ("spec", "version"),
    [
        ('"1.30.0"', "1.30.0"),
        ('"==1.30.0"', "1.30.0"),
        ('"^1.30"', None),
        ('"~1.30.0"', None),
        ('">=1.0,<2.0"', None),
        ('"*"', None),
        ('{ version = "1.30.0", extras = ["datalib"] }', "1.30.0"),
        ('{ git = "https://github.com/openai/openai-python.git" }', None),
    ],
)
def test_pyproject_poetry_specs(spec: str, version: str | None) -> None:
    text = f"[tool.poetry.dependencies]\npython = '^3.11'\nopenai = {spec}\n"
    (location,) = parse_pyproject(text, "openai")
    assert location.version == version


def test_pyproject_poetry_groups_are_read() -> None:
    text = '[tool.poetry.group.dev.dependencies]\nstripe = "9.0.0"\n'
    (location,) = parse_pyproject(text, "stripe")
    assert location.version == "9.0.0"


def test_broken_toml_is_treated_as_no_declarations() -> None:
    assert parse_pyproject("[project\ndependencies = [", "openai") == []
    assert parse_lock("not toml at all [[[", "openai", "uv.lock") == []


# --- lock files ---------------------------------------------------------------


def test_lock_files_give_the_resolved_version() -> None:
    lock = """
version = 1

[[package]]
name = "httpx"
version = "0.28.1"

[[package]]
name = "OpenAI"
version = "1.30.0"
source = { registry = "https://pypi.org/simple" }
"""
    (location,) = parse_lock(lock, "openai", "uv.lock")
    assert (location.version, location.file) == ("1.30.0", "uv.lock")
    assert parse_lock(lock, "stripe", "uv.lock") == []


# --- find_pin -----------------------------------------------------------------


def test_exact_pin_in_requirements(tmp_path: Path) -> None:
    result = find_pin(_repo(tmp_path, **{"requirements.txt": "openai==1.30.0\n"}), "openai")
    assert (result.status, result.version, result.decided_by) == (
        PINNED,
        "1.30.0",
        "requirements.txt",
    )


def test_lock_file_wins_over_a_range_in_the_manifest(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{
            "pyproject.toml": '[project]\ndependencies = ["openai>=1.0"]\n',
            "uv.lock": '[[package]]\nname = "openai"\nversion = "1.30.0"\n',
        },
    )
    result = find_pin(files, "openai")
    assert (result.status, result.version, result.decided_by) == (PINNED, "1.30.0", "uv.lock")


def test_poetry_lock_is_read_too(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{
            "pyproject.toml": '[tool.poetry.dependencies]\nstripe = "^9.0"\n',
            "poetry.lock": '[[package]]\nname = "stripe"\nversion = "9.4.0"\n',
        },
    )
    assert find_pin(files, "stripe").version == "9.4.0"


def test_range_without_a_lock_is_not_pinned(tmp_path: Path) -> None:
    files = _repo(tmp_path, **{"pyproject.toml": '[project]\ndependencies = ["openai>=1.0"]\n'})
    result = find_pin(files, "openai")
    assert result.status == NOT_PINNED
    assert result.version is None
    assert result.locations[0].spec == ">=1.0"


def test_package_absent_everywhere_is_not_used(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{
            "requirements.txt": "requests==2.0\n",
            "uv.lock": '[[package]]\nname = "requests"\nversion = "2.0"\n',
            "README.md": "we talk about openai==1.0 here",
        },
    )
    assert find_pin(files, "openai").status == NOT_USED


def test_several_requirements_files_that_agree(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{"requirements.txt": "openai==1.30.0\n", "requirements-dev.txt": "openai==1.30.0\n"},
    )
    assert find_pin(files, "openai").status == PINNED


def test_disagreeing_pins_are_a_conflict(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{"requirements.txt": "openai==1.30.0\n", "requirements-dev.txt": "openai==1.31.0\n"},
    )
    result = find_pin(files, "openai")
    assert result.status == CONFLICT
    assert {loc.version for loc in result.locations} == {"1.30.0", "1.31.0"}


def test_an_exact_pin_beats_a_range_elsewhere(tmp_path: Path) -> None:
    files = _repo(
        tmp_path,
        **{
            "requirements.txt": "openai==1.30.0\n",
            "pyproject.toml": '[project]\ndependencies = ["openai>=1.0"]\n',
        },
    )
    assert find_pin(files, "openai").version == "1.30.0"


def test_local_files_cannot_be_read_outside_the_root(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("openai==9.9.9", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    files = LocalRepoFiles(repo)
    assert files.read_text("../secret.txt") is None
    assert files.read_text("missing.txt") is None
