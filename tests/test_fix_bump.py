"""Bump editor (spec 05 F5.1, F5.2)."""

from __future__ import annotations

from pathlib import Path

from guardian.fix import bump as B
from guardian.watch.depfiles import LocalRepoFiles


def repo(tmp_path: Path, files: dict[str, str]) -> LocalRepoFiles:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return LocalRepoFiles(tmp_path)


def only_edit(result: B.BumpResult, file: str) -> str:
    assert result.ok, result.problems
    assert set(result.edits) == {file}
    return result.edits[file][1]


def test_requirements_keeps_the_trailing_comment(tmp_path: Path) -> None:
    text = "httpx==0.27.0\nopenai==1.30.0  # pinned for the chat client\nrich>=13\n"
    result = B.bump(repo(tmp_path, {"requirements.txt": text}), "openai", "1.30.0", "1.45.0")
    after = only_edit(result, "requirements.txt")
    assert after == "httpx==0.27.0\nopenai==1.45.0  # pinned for the chat client\nrich>=13\n"


def test_requirements_keeps_spacing_extras_markers_and_crlf(tmp_path: Path) -> None:
    text = 'openai[datalib] == 1.30.0 ; python_version >= "3.9"\r\nstripe==9.0.0\r\n'
    result = B.bump(repo(tmp_path, {"requirements.txt": text}), "openai", "1.30.0", "1.45.0")
    after = only_edit(result, "requirements.txt")
    assert after == 'openai[datalib] == 1.45.0 ; python_version >= "3.9"\r\nstripe==9.0.0\r\n'


def test_requirements_does_not_touch_a_similar_package_or_version(tmp_path: Path) -> None:
    text = "openai-agents==1.30.0\nopenai==1.30.0\nother==1.30.0.post1\n"
    result = B.bump(repo(tmp_path, {"requirements.txt": text}), "openai", "1.30.0", "1.45.0")
    after = only_edit(result, "requirements.txt")
    assert after == "openai-agents==1.30.0\nopenai==1.45.0\nother==1.30.0.post1\n"


def test_requirements_followed_through_an_include(tmp_path: Path) -> None:
    files = {"requirements.txt": "-r requirements/base.txt\n", "requirements/base.txt":
             "stripe==9.0.0\n"}
    result = B.bump(repo(tmp_path, files), "stripe", "9.0.0", "13.0.0")
    assert only_edit(result, "requirements/base.txt") == "stripe==13.0.0\n"


def test_hash_pinned_requirements_are_refused(tmp_path: Path) -> None:
    text = "openai==1.30.0 \\\n    --hash=sha256:abc\n"
    result = B.bump(repo(tmp_path, {"requirements.txt": text}), "openai", "1.30.0", "1.45.0")
    assert not result.ok
    assert [p.reason for p in result.problems] == [B.HASH_PINNED]
    assert result.edits == {}


def test_a_range_that_allows_the_new_version_needs_only_the_lock(tmp_path: Path) -> None:
    lock = '[[package]]\nname = "openai"\nversion = "1.30.0"\n'
    files = {"pyproject.toml": '[project]\nname = "app"\ndependencies = ["openai>=1.0"]\n',
             "uv.lock": lock}
    result = B.bump(repo(tmp_path, files), "openai", "1.30.0", "1.45.0")
    assert result.ok
    assert result.edits == {}
    assert result.lock_files == ["uv.lock"]


def test_a_range_that_excludes_the_new_version_is_refused(tmp_path: Path) -> None:
    files = {"requirements.txt": "openai>=1.0,<2\n"}
    result = B.bump(repo(tmp_path, files), "openai", "1.30.0", "2.0.0")
    assert [p.reason for p in result.problems] == [B.RANGE_EXCLUDES]


def test_url_pins_are_refused(tmp_path: Path) -> None:
    files = {"requirements.txt": "openai @ https://example.com/openai-1.30.0.tar.gz\n"}
    result = B.bump(repo(tmp_path, files), "openai", "1.30.0", "1.45.0")
    assert [p.reason for p in result.problems] == [B.URL_PINNED]


def test_pyproject_pep621_and_groups(tmp_path: Path) -> None:
    text = (
        "[project]\n"
        'name = "app"\n'
        "dependencies = [\n"
        '    "httpx==0.27.0",\n'
        '    "openai==1.30.0",  # the SDK\n'
        "]\n\n"
        "[project.optional-dependencies]\n"
        "extra = ['openai[datalib]==1.30.0']\n\n"
        "[tool.other]\n"
        'note = "openai==1.30.0"\n'
    )
    result = B.bump(repo(tmp_path, {"pyproject.toml": text}), "openai", "1.30.0", "1.45.0")
    after = only_edit(result, "pyproject.toml")
    expected = text.replace('"openai==1.30.0",', '"openai==1.45.0",').replace(
        "'openai[datalib]==1.30.0'", "'openai[datalib]==1.45.0'"
    )
    assert after == expected  # [tool.other] is not a dependency table: untouched


def test_pyproject_poetry_string_and_table(tmp_path: Path) -> None:
    text = (
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'openai = "1.30.0"\n\n'
        "[tool.poetry.group.dev.dependencies]\n"
        'openai = { version = "==1.30.0", extras = ["datalib"] }\n'
    )
    result = B.bump(repo(tmp_path, {"pyproject.toml": text}), "openai", "1.30.0", "1.45.0")
    after = only_edit(result, "pyproject.toml")
    assert after == text.replace('"1.30.0"', '"1.45.0"').replace('"==1.30.0"', '"==1.45.0"')


def test_poetry_caret_range_is_left_alone_when_it_allows_the_version(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": '[tool.poetry.dependencies]\nopenai = "^1.30"\n',
        "poetry.lock": '[[package]]\nname = "openai"\nversion = "1.30.0"\n',
    }
    result = B.bump(repo(tmp_path, files), "openai", "1.30.0", "1.45.0")
    assert result.ok and result.edits == {} and result.lock_files == ["poetry.lock"]


def test_nothing_to_bump(tmp_path: Path) -> None:
    result = B.bump(repo(tmp_path, {"requirements.txt": "httpx\n"}), "openai", "1.0", "2.0")
    assert [p.reason for p in result.problems] == [B.NOTHING_TO_BUMP]
