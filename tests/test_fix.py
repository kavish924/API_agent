"""Codemods, the single-shot fixer and patch assembly (spec 05 F5.3–F5.6, F5.10–F5.14).

Findings come from the real scanner over the fixture SDK from the API-diff tests, whose
v1 -> v2 changes include a class rename (`Reversal` -> `TransferReversal`), a keyword
rename (`engine` -> `model`), a parameter made positional-only (`retrieve(id)`) and a
removed parameter (`helper(b)`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian.apidiff.compare import compare
from guardian.apidiff.reconcile import FROM_DIFF, HIGH, GradedChange, reconcile
from guardian.apidiff.surface import Surface, build_surface
from guardian.common.context import AppContext
from guardian.common.llm import LLMResponse, StubProvider
from guardian.extract.schema import ChangeType
from guardian.fix import (
    COMPLETE,
    COULD_NOT_BUMP,
    FIX_FAILED,
    FIXED,
    MODE_AGENT,
    MODE_SINGLE_SHOT,
    NOTHING_FIXED,
    PARTIAL,
    REPORT_ONLY,
    SingleShotFixer,
    build_patch,
)
from guardian.fix.codemods import apply_codemods
from guardian.fix.patch import AGENT_NOT_BUILT, PatchSet, region_of
from guardian.fix.single_shot import splice
from guardian.fix.types import TIER_CODEMOD, TIER_SINGLE_SHOT
from guardian.scan import rules as R
from guardian.scan.analyzer import DIRECT, Finding
from guardian.scan.scanner import ScanResult, scan_repo
from guardian.watch.depfiles import LocalRepoFiles
from tests.apifixtures import V1, V2, write_tree


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory: pytest.TempPathFactory) -> tuple[Surface, Surface]:
    root = tmp_path_factory.mktemp("sdk")
    return (
        build_surface(write_tree(root / "v1", V1), "pkg", "1.0.0"),
        build_surface(write_tree(root / "v2", V2), "pkg", "2.0.0"),
    )


@pytest.fixture(scope="module")
def changes(surfaces: tuple[Surface, Surface]) -> list[GradedChange]:
    old, new = surfaces
    return reconcile([], old, new, compare(old, new)).changes


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def scan(root: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]) -> ScanResult:
    return scan_repo(root, "pkg", surfaces[0], changes)


def codemod(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    code: str,
) -> tuple[str, set[int], ScanResult]:
    root = write(tmp_path / "repo", {"app.py": code})
    result = scan(root, surfaces, changes)
    findings = [f for f in result.findings if f.file == "app.py"]
    out = apply_codemods(code, findings, changes)
    return out.text, out.handled, result


# --- codemods --------------------------------------------------------------------------


def test_keyword_rename_changes_only_the_argument_name(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = (
        "from pkg import Client as C\n\n"
        "# Create one.\n"
        'C("k").create(  "x",   engine = "e" ,retries=3)  # odd spacing kept\n'
        "engine = 'not a keyword argument'\n"
    )
    text, handled, _ = codemod(tmp_path, surfaces, changes, code)
    assert text == code.replace('engine = "e"', 'model = "e"')
    assert handled == {0}


def test_symbol_rename_through_an_import_renames_every_use(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = (
        "import pkg\n"
        "from pkg import Reversal\n\n"
        "r = Reversal()  # keep this comment\n"
        "x = pkg.Reversal()\n"
        "def f(v: Reversal) -> Reversal:\n"
        "    return v\n"
    )
    text, handled, result = codemod(tmp_path, surfaces, changes, code)
    assert text == code.replace("Reversal", "TransferReversal")
    assert handled == set(range(len(result.findings)))


def test_symbol_rename_through_an_aliased_import_keeps_the_alias(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = "from pkg import Reversal as R\n\nR().retrieve('x')\n"
    text, handled, result = codemod(tmp_path, surfaces, changes, code)
    assert text == "from pkg import TransferReversal as R\n\nR().retrieve('x')\n"
    assert handled == set(range(len(result.findings)))


def test_a_change_without_a_codemod_is_left_for_tier_2(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = 'import pkg\n\npkg.Client("k").retrieve(id="x")\n'
    text, handled, result = codemod(tmp_path, surfaces, changes, code)
    assert text == code
    assert handled == set()
    assert [f.rule for f in result.findings] == [R.KEYWORD]


def _finding(
    line: int,
    column: int,
    rule: str,
    change: int | None = None,
    query: int | None = None,
    param: str | None = None,
) -> Finding:
    return Finding("app.py", line, column, "", "sym", rule, DIRECT, "high", change, query, param)


def test_literal_replacement_keeps_quotes_and_follows_a_constant() -> None:
    code = "MODEL = 'gpt-old'\nclient.create(model=\"gpt-old\")\nclient.create(model=MODEL)\n"
    findings = [
        _finding(2, 15, R.LITERAL, query=0, param="model"),
        _finding(1, 1, R.LITERAL, query=0, param="model"),
    ]
    out = apply_codemods(code, findings, [], literal_replacements={0: "gpt-new"})
    assert out.text == code.replace("gpt-old", "gpt-new")
    assert out.handled == {0, 1}


def test_typed_dict_key_rename_in_a_dict_literal() -> None:
    change = GradedChange(
        FROM_DIFF,
        ChangeType.RENAMED,
        "pkg.P",
        "pkg.P",
        HIGH,
        True,
        affected_parameter="old_key",
        replacement="new_key",
        replacement_verified=True,
    )
    code = "call(params={'old_key': 1, \"other\": 2})\n"
    out = apply_codemods(code, [_finding(1, 14, R.DICT_KEY, change=0, param="old_key")], [change])
    assert out.text == "call(params={'new_key': 1, \"other\": 2})\n"


def test_moved_module_import_path() -> None:
    change = GradedChange(
        FROM_DIFF,
        ChangeType.RENAMED,
        "pkg.old",
        "pkg.old",
        HIGH,
        True,
        replacement="pkg.new",
        replacement_verified=True,
    )
    code = "from pkg.old import Thing  # moved\n"
    out = apply_codemods(code, [_finding(1, 1, R.MODULE, change=0)], [change])
    assert out.text == "from pkg.new import Thing  # moved\n"


# --- splicing and regions --------------------------------------------------------------


def test_splice_restores_the_base_indentation() -> None:
    source = "class A:\n    def f(self):\n        return 1\n\nx = 2\n"
    after = splice(source, 2, 3, "def f(self):\n    return 2\n")
    assert after == "class A:\n    def f(self):\n        return 2\n\nx = 2\n"


def test_region_is_the_innermost_function_with_decorators() -> None:
    source = "import x\n\n\nclass A:\n    @dec\n    def f(self):\n        return x.y()\n"
    assert region_of(source, 7) == (5, 7)
    assert region_of(source, 1) == (1, 1)


# --- patch assembly ----------------------------------------------------------------------

APP = (
    "import pkg\n"
    "from pkg import Reversal\n"
    "\n"
    'client = pkg.Client("k")\n'
    "\n"
    "\n"
    "def get(x):\n"
    "    return client.retrieve(id=x)\n"
    "\n"
    "\n"
    "def make():\n"
    '    client.create("n", engine="e")\n'
    "    return Reversal()\n"
)


def patch_for(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    files: dict[str, str],
    **kwargs: object,
) -> tuple[PatchSet, ScanResult]:
    root = write(tmp_path / "repo", files)
    result = scan(root, surfaces, changes)
    patch = build_patch(
        LocalRepoFiles(root),
        result.findings,
        changes,
        package="pkg",
        module="pkg",
        old="1.0.0",
        new="2.0.0",
        **kwargs,  # type: ignore[arg-type]
    )
    return patch, result


def test_bump_and_two_codemods_in_one_file_make_one_patch(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    app = APP.replace("    return client.retrieve(id=x)\n", "    return client.retrieve(x)\n")
    files = {"requirements.txt": "pkg==1.0.0  # the SDK\n", "app.py": app}
    patch, result = patch_for(tmp_path, surfaces, changes, files)
    assert patch.status == COMPLETE
    assert set(patch.files) == {"requirements.txt", "app.py"}
    assert patch.files["requirements.txt"][1] == "pkg==2.0.0  # the SDK\n"
    assert patch.files["app.py"][1] == app.replace("Reversal", "TransferReversal").replace(
        'engine="e"', 'model="e"'
    )
    assert all(f.status == FIXED and f.tier == TIER_CODEMOD for f in patch.fixes)
    assert len(patch.fixes) == len(result.findings)
    diff = patch.diff()
    assert "+pkg==2.0.0  # the SDK" in diff and '+    client.create("n", model="e")' in diff


def test_single_shot_fixes_what_no_codemod_can(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    ctx: AppContext,
    stub: StubProvider,
) -> None:
    stub.push(
        LLMResponse(
            data={"status": "fixed", "code": "def get(x):\n    return client.retrieve(x)\n"},
            tokens_in=10,
        )
    )
    files = {"requirements.txt": "pkg==1.0.0\n", "app.py": APP}
    patch, _ = patch_for(
        tmp_path, surfaces, changes, files, mode=MODE_SINGLE_SHOT, fixer=SingleShotFixer(ctx.llm)
    )
    assert patch.status == COMPLETE
    after = patch.files["app.py"][1]
    assert "    return client.retrieve(x)\n" in after
    assert 'client.create("n", model="e")' in after and "TransferReversal()" in after
    tiers = {f.tier for f in patch.fixes}
    assert tiers == {TIER_CODEMOD, TIER_SINGLE_SHOT}
    (request,) = stub.requests
    prompt = request.messages[0].content
    assert '<region lines="7-8">' in prompt and "retrieve(id=x)" in prompt
    assert json.loads(prompt.split("<changes>\n")[1].split("</changes>")[0])[0]["parameter"] == "id"
    attempt = next(a for a in patch.attempts if a.tier == TIER_SINGLE_SHOT)
    assert attempt.accepted and "-    return client.retrieve(id=x)" in attempt.diff


def test_a_rejected_model_diff_is_dropped_whole(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    ctx: AppContext,
    stub: StubProvider,
) -> None:
    hostile = "def get(x):\n    import subprocess\n    return client.retrieve(x)\n"
    stub.push(LLMResponse(data={"status": "fixed", "code": hostile}))
    files = {"requirements.txt": "pkg==1.0.0\n", "app.py": APP}
    patch, _ = patch_for(
        tmp_path, surfaces, changes, files, mode=MODE_SINGLE_SHOT, fixer=SingleShotFixer(ctx.llm)
    )
    assert patch.status == PARTIAL
    failed = [f for f in patch.fixes if f.status == FIX_FAILED]
    assert len(failed) == 1 and "subprocess" in failed[0].reason
    after = patch.files["app.py"][1]
    assert "subprocess" not in after and "retrieve(id=x)" in after  # nothing of it applied
    rejected = next(a for a in patch.attempts if a.tier == TIER_SINGLE_SHOT)
    assert not rejected.accepted and rejected.reasons


def test_cannot_fix_is_a_failed_fix(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    ctx: AppContext,
    stub: StubProvider,
) -> None:
    stub.push(LLMResponse(data={"status": "cannot-fix", "reason": "id is computed"}))
    code = 'import pkg\n\n\ndef get(x):\n    return pkg.Client("k").retrieve(id=x)\n'
    patch, _ = patch_for(
        tmp_path,
        surfaces,
        changes,
        {"requirements.txt": "pkg==1.0.0\n", "app.py": code},
        mode=MODE_SINGLE_SHOT,
        fixer=SingleShotFixer(ctx.llm),
    )
    assert patch.status == NOTHING_FIXED
    assert [(f.status, f.reason) for f in patch.fixes] == [(FIX_FAILED, "id is computed")]
    assert set(patch.files) == {"requirements.txt"}  # the bump alone; spec 07 decides


def test_agent_mode_without_the_agent_fails_tier_2_with_the_reason(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    files = {"requirements.txt": "pkg==1.0.0\n", "app.py": APP}
    patch, _ = patch_for(tmp_path, surfaces, changes, files, mode=MODE_AGENT)
    failed = [f for f in patch.fixes if f.status == FIX_FAILED]
    assert [f.reason for f in failed] == [AGENT_NOT_BUILT]
    assert patch.status == PARTIAL


def test_both_modes_give_results_of_the_same_shape(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    ctx: AppContext,
    stub: StubProvider,
) -> None:
    stub.push(LLMResponse(data={"status": "cannot-fix", "reason": "no"}))
    files = {"requirements.txt": "pkg==1.0.0\n", "app.py": APP}
    agent, _ = patch_for(tmp_path / "a", surfaces, changes, files, mode=MODE_AGENT)
    single, _ = patch_for(
        tmp_path / "s",
        surfaces,
        changes,
        files,
        mode=MODE_SINGLE_SHOT,
        fixer=SingleShotFixer(ctx.llm),
    )
    assert [f.index for f in agent.fixes] == [f.index for f in single.fixes]
    assert {f.status for f in agent.fixes} == {f.status for f in single.fixes}


def test_unfixable_changes_are_report_only_and_make_the_patch_partial(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = "import pkg\nfrom pkg import Reversal\n\npkg.helper(1, b=2)\nReversal()\n"
    patch, _ = patch_for(
        tmp_path, surfaces, changes, {"requirements.txt": "pkg==1.0.0\n", "app.py": code}
    )
    statuses = sorted(f.status for f in patch.fixes)
    assert REPORT_ONLY in statuses and FIXED in statuses
    assert patch.status == PARTIAL and patch.partial
    assert "pkg.helper(1, b=2)" in patch.files["app.py"][1]  # never edited


def test_response_shape_change_gets_no_diff(tmp_path: Path) -> None:
    change = GradedChange(
        FROM_DIFF, ChangeType.RESPONSE_SHAPE_CHANGED, "pkg.R", "pkg.R", HIGH, fixable=False
    )
    root = write(tmp_path, {"requirements.txt": "pkg==1.0.0\n", "app.py": "r.id\n"})
    patch = build_patch(
        LocalRepoFiles(root),
        [_finding(1, 1, R.REFERENCE, change=0)],
        [change],
        package="pkg",
        module="pkg",
        old="1.0.0",
        new="2.0.0",
    )
    assert [(f.status, f.reason) for f in patch.fixes] == [(REPORT_ONLY, "response-shape-changed")]
    assert patch.status == NOTHING_FIXED and set(patch.files) == {"requirements.txt"}


def test_a_failed_bump_means_no_patch_at_all(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    files = {"requirements.txt": "pkg>=1,<2\n", "app.py": "from pkg import Reversal\n"}
    patch, _ = patch_for(tmp_path, surfaces, changes, files)
    assert patch.status == COULD_NOT_BUMP
    assert patch.files == {}
    assert {f.status for f in patch.fixes} == {FIX_FAILED}


def test_api_level_runs_have_no_bump(tmp_path: Path) -> None:
    root = write(tmp_path, {"app.py": 'client.create(model="gpt-old")\n'})
    patch = build_patch(
        LocalRepoFiles(root),
        [_finding(1, 15, R.LITERAL, query=0, param="model")],
        [],
        package="openai",
        module="openai",
        old="",
        new="",
        literal_replacements={0: "gpt-new"},
        bump_version=False,
    )
    assert patch.status == COMPLETE
    assert patch.files == {
        "app.py": ('client.create(model="gpt-old")\n', 'client.create(model="gpt-new")\n')
    }
