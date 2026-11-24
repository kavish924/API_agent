"""AST guard on model-written diffs (spec 05 F5.7–F5.10): an adversarial set."""

from __future__ import annotations

import pytest

from guardian.fix.guard import FileChange, GuardResult, check_patch

PATH = "app/ask.py"

BEFORE = """import openai
from app import helpers

client = openai.OpenAI()


@helpers.traced
def ask(question):
    # Ask the model.
    response = client.chat.completions.create(
        model="gpt-4", max_tokens=10, messages=question
    )
    return helpers.clean(response)


def other():
    return client.models.list()
"""

FINDING = 10  # the `create(...)` statement, where `max_tokens=` is passed
GOOD = BEFORE.replace("max_tokens=10", "max_completion_tokens=10")


def check(
    after: str, *, path: str = PATH, lines: tuple[int, ...] = (FINDING,), **kw: int
) -> GuardResult:
    return check_patch(
        [FileChange(path, BEFORE, after)], {PATH: list(lines)}, package="openai", **kw
    )


def test_the_fix_at_the_finding_is_accepted() -> None:
    result = check(GOOD)
    assert result.accepted, result.reasons
    assert result.changed_lines == 2
    assert '-        model="gpt-4", max_tokens=10' in result.diff


def test_a_multi_line_rewrite_of_the_matched_statement_is_accepted() -> None:
    after = BEFORE.replace(
        '        model="gpt-4", max_tokens=10, messages=question\n',
        '        model="gpt-4",\n        max_completion_tokens=10,\n        messages=question,\n',
    )
    assert check(after).accepted


def test_a_new_import_from_the_sdk_is_accepted() -> None:
    after = GOOD.replace("import openai\n", "import openai\nfrom openai.types import ChatModel\n")
    assert check(after).accepted


def test_a_name_bound_by_a_new_sdk_import_may_be_used() -> None:
    after = BEFORE.replace("import openai\n", "import openai\nfrom openai import NOT_GIVEN\n")
    after = after.replace("max_tokens=10", "max_tokens=NOT_GIVEN")
    assert check(after).accepted


@pytest.mark.parametrize(
    ("after", "reason"),
    [
        # F5.8: an import of any other module.
        (GOOD.replace("import openai\n", "import openai\nimport subprocess\n"), "subprocess"),
        (
            GOOD.replace("from app import helpers\n", "from app import helpers, secrets\n"),
            "no finding",
        ),
        # F5.7: a statement with no finding.
        (GOOD.replace("return helpers.clean(response)", "return response"), "no finding"),
        (GOOD.replace("return client.models.list()", "return []"), "no finding"),
        (GOOD.replace("@helpers.traced", "@helpers.logged"), "no finding"),
        (GOOD.replace("def ask(question):", "def ask(question, key=None):"), "no finding"),
        # A new statement next to the fix.
        (
            GOOD.replace("    # Ask the model.\n", "    # Ask the model.\n    print(question)\n"),
            "beyond the ones",
        ),
        # New names wrapped around the matched call (the eval rule).
        (BEFORE.replace("response = client", "response = eval('1') or client"), "eval"),
        (BEFORE.replace("messages=question", "messages=__import__('os').environ"), "__import__"),
        (BEFORE.replace("response = client.chat", "response = getattr(client, 'chat')"), "getattr"),
        (BEFORE.replace("messages=question", "messages=open('/etc/passwd').read()"), "open"),
        (BEFORE.replace("max_tokens=10", "max_tokens=exec('x=1')"), "exec"),
        # The result must parse.
        (BEFORE.replace("max_tokens=10,", "max_tokens=10,,"), "does not parse"),
    ],
)
def test_adversarial_diffs_are_rejected(after: str, reason: str) -> None:
    result = check(after)
    assert not result.accepted
    assert any(reason in r for r in result.reasons), result.reasons


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", "pyproject.toml", "requirements.txt", "app/conf.cfg"]
)
def test_non_python_files_are_rejected(path: str) -> None:
    result = check_patch([FileChange(path, "a: 1\n", "a: 2\n")], {path: [1]}, package="openai")
    assert not result.accepted
    assert result.reasons == [f"{path}: only Python files may change"]


def test_a_file_without_findings_is_rejected() -> None:
    other = FileChange("app/other.py", "x = 1\n", "x = 2\n")
    result = check_patch(
        [FileChange(PATH, BEFORE, GOOD), other], {PATH: [FINDING]}, package="openai"
    )
    assert not result.accepted
    assert result.reasons == ["app/other.py: touches a file that has no findings"]


def test_creating_or_deleting_files_is_rejected() -> None:
    created = check_patch(
        [FileChange("app/new.py", None, "x = 1\n")], {"app/new.py": [1]}, package="openai"
    )
    deleted = check_patch([FileChange(PATH, BEFORE, None)], {PATH: [FINDING]}, package="openai")
    assert created.reasons == ["app/new.py: creates a file"]
    assert deleted.reasons == [f"{PATH}: deletes a file"]


def test_the_line_cap_counts_per_finding() -> None:
    after = BEFORE.replace(
        '        model="gpt-4", max_tokens=10, messages=question\n',
        "".join(f"        a{i}=None,\n" for i in range(30)),
    )
    capped = check(after, line_cap=20)
    assert not capped.accepted
    assert any("over the cap of 20" in r for r in capped.reasons)
    # The same change addressing two findings has twice the room (names aside).
    assert not any("cap" in r for r in check(after, line_cap=20, findings=2).reasons)


def test_an_empty_change_is_not_an_accepted_fix() -> None:
    result = check(BEFORE)
    assert not result.accepted
    assert result.reasons == ["the change is empty"]


def test_rejection_reasons_are_capped() -> None:
    after = BEFORE + "".join(f"x{i} = {i}\n" for i in range(30))
    result = check(after)
    assert len(result.reasons) == 11
    assert result.reasons[-1].startswith("... and ")
