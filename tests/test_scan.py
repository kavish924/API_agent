"""Repository scanning (spec 04), against the fixture SDK from the API-diff tests.

The fixture SDK's v1 → v2 change list has one change of every kind, so each test can
pick the change it needs by its kind and symbol.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import select

from guardian import pipeline
from guardian.apidiff.compare import compare
from guardian.apidiff.reconcile import GradedChange, reconcile
from guardian.apidiff.surface import Surface, build_surface
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.models import ChangeRecord, Finding, Run
from guardian.pipeline.types import NO_FINDINGS, NOT_IMPLEMENTED, RunRequest
from guardian.scan import rules as R
from guardian.scan.analyzer import ALIASED, CLIENT_VARIABLE, CROSS_MODULE, DIRECT, SHAPE_MATCH
from guardian.scan.files import module_names, python_files
from guardian.scan.scanner import ScanResult, scan_repo
from guardian.scan.store import LOCAL_INSTALLATION, record_scan
from tests.apifixtures import V1, V2, write_tree
from tests.fakes import OPENAI, FakeGitHub, FakePyPI

# The fixture SDK's public paths, as user code would write them.
SDK_CODE = "import pkg\n"


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


def scan(
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
    files: dict[str, str],
    **kwargs: object,
) -> ScanResult:
    repo = tmp_path / "repo"
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return scan_repo(repo, "pkg", surfaces[0], changes, **kwargs)  # type: ignore[arg-type]


def lines(result: ScanResult) -> list[tuple[str, int, str]]:
    return [(f.file, f.line, f.rule) for f in result.findings]


# --- names, aliases and clients ---------------------------------------------------


def test_through_an_aliased_class_import(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = 'from pkg import Client as C\nC("k").create("x", engine="e")\n'
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    (finding,) = result.findings
    assert (finding.line, finding.rule, finding.param, finding.tier) == (
        2,
        R.KEYWORD,
        "engine",
        ALIASED,
    )
    assert finding.code == 'engine="e"'


def test_through_an_aliased_module_import(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = 'import pkg as p\nc = p.Client("k")\nc.create("x", engine="e")\n'
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.tier) for f in result.findings] == [(3, CLIENT_VARIABLE)]


def test_only_calls_that_pass_the_changed_keyword(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + 'c = pkg.Client("k")\nc.create("x", engine="e")\nc.create("x", model="m")\n'
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert lines(result) == [("app.py", 3, R.KEYWORD)]


def test_never_matches_by_name_alone(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        "def create(name, engine): ...\n"
        'create("x", engine="e")\n'
        "class Reversal: ...\n"
        "Reversal()\n"
        "def helper(a, b=1): ...\n"
        "helper(1, b=2)\n"
        "# pkg.Reversal and engine= in a comment\n"
        '"""pkg.Client.request in a docstring"""\n'
    )
    assert scan(tmp_path, surfaces, changes, {"app.py": code}).findings == []


def test_direct_use_of_the_package(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + "pkg.helper(1, b=2)\nr = pkg.Reversal()\n"
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.rule, f.tier) for f in result.findings] == [
        (2, R.KEYWORD, DIRECT),
        (3, R.REFERENCE, DIRECT),
    ]


def test_removed_name_in_an_import_statement(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = "from pkg import Reversal, OldError\nfrom pkg import Client\n"
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert sorted(f.code for f in result.findings) == ["OldError", "Reversal"]
    assert {f.line for f in result.findings} == {1}


def test_inherited_method_and_positional_only_parameter(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = (
        SDK_CODE
        + 'c = pkg.Client("k")\nc.request("get", "/")\nc.retrieve(id="abc")\nc.retrieve("abc")\n'
    )
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.rule, f.param) for f in result.findings] == [
        (3, R.REFERENCE, None),  # request() removed from the base class
        (4, R.KEYWORD, "id"),  # id is positional-only now; passing it positionally is fine
    ]
    assert result.findings[0].symbol == "pkg._base.BaseResource.request"


def test_client_through_a_property_chain(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = (
        SDK_CODE
        + 'c = pkg.Client("k")\nc.chat.complete(model="m")\nc.chat.complete(model="m", mode="x")\n'
    )
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.rule, f.param) for f in result.findings] == [(3, R.MISSING, "mode")]


def test_missing_parameter_is_not_claimed_when_kwargs_are_splatted(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + 'opts = {}\npkg.Client("k").chat.complete(model="m", **opts)\n'
    assert scan(tmp_path, surfaces, changes, {"app.py": code}).findings == []


def test_self_attribute_typed_by_a_constructor_parameter(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        "class Service:\n"
        "    def __init__(self, client: pkg.Client):\n"
        "        self.client = client\n"
        "    def go(self):\n"
        '        return self.client.create("x", engine="e")\n'
    )
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.tier) for f in result.findings] == [(6, CLIENT_VARIABLE)]


def test_class_level_declaration_and_with_statement(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        "class Service:\n"
        "    client: pkg.Client\n"
        "    def go(self):\n"
        '        self.client.create("x", engine="e")\n'
        'with pkg.Client("k") as c:\n'
        '    c.create("x", engine="e")\n'
    )
    assert [f.line for f in scan(tmp_path, surfaces, changes, {"app.py": code}).findings] == [5, 7]


def test_rebinding_to_something_else_is_not_guessed(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        'import other\nc = pkg.Client("k")\nc = other.Thing()\nc.create("x", engine="e")\n'
    )
    assert scan(tmp_path, surfaces, changes, {"app.py": code}).findings == []


def test_none_placeholder_does_not_hide_the_client(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + 'c = None\nc = pkg.Client("k")\nc.create("x", engine="e")\n'
    assert [f.line for f in scan(tmp_path, surfaces, changes, {"app.py": code}).findings] == [4]


def test_client_imported_from_another_module(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    files = {
        "app/__init__.py": "",
        "app/clients.py": SDK_CODE + 'shared = pkg.Client("k")\n',
        "app/jobs.py": "from app.clients import shared\nshared.create('x', engine='e')\n",
        "app/views.py": "from .clients import shared\nshared.create('y', engine='e')\n",
        "app/plain.py": "import app.clients as cl\ncl.shared.create('z', engine='e')\n",
    }
    result = scan(tmp_path, surfaces, changes, files)
    assert [(f.file, f.line, f.tier, f.confidence) for f in result.findings] == [
        ("app/jobs.py", 2, CROSS_MODULE, "medium"),
        ("app/plain.py", 2, CROSS_MODULE, "medium"),
        ("app/views.py", 2, CROSS_MODULE, "medium"),
    ]


# --- dict literals and literal arguments -------------------------------------------


def test_typed_dict_keys_in_dict_literals(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        'c = pkg.Client("k")\n'
        'c.chat.complete(model="m", mode="x", tools=[{"type": "t", "display_width": 3}])\n'
        'c.chat.complete(model="m", mode="x", tools=[{"type": "t", "environment": "e"}])\n'
        'c.chat.complete(model="m", mode="x", tools=[{"other": 1, "keys": 2}])\n'
    )
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.rule, f.param, f.tier) for f in result.findings] == [
        (3, R.DICT_MISSING, "environment", SHAPE_MATCH),
        (3, R.DICT_KEY, "display_width", SHAPE_MATCH),
    ]
    assert result.findings[1].code == '"display_width"'


def test_keyword_of_a_params_typeddict_taken_as_kwargs(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        'c = pkg.Client("k")\n'
        'c.retrieve("abc", payment_method_types=["card"])\n'
        'c.retrieve("abc", expand=["x"])\n'
    )
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert [(f.line, f.rule, f.param) for f in result.findings] == [
        (3, R.KEYWORD, "payment_method_types")
    ]


def test_literal_arguments_with_api_family(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + (
        'MODEL = "old-model"\n'
        'c = pkg.Client("k")\n'
        'c.chat.complete(model="old-model", mode="x")\n'
        'c.chat.complete(model=MODEL, mode="x")\n'
        'c.chat.complete(model="new-model", mode="x")\n'
        'print("old-model")\n'
        "# old-model in a comment\n"
    )
    query = R.LiteralQuery(value="old-model", argument="model", family="chat")
    result = scan(
        tmp_path,
        surfaces,
        [],
        {"app.py": code},
        literals=[query],
        families=[("pkg.resources.chat.", "chat")],
    )
    assert [(f.line, f.rule, f.family, f.code) for f in result.findings] == [
        (2, R.LITERAL, "chat", "MODEL"),  # the constant, one step away
        (4, R.LITERAL, "chat", 'model="old-model"'),
    ]
    other = R.LiteralQuery(value="old-model", argument="model", family="audio.speech")
    wrong = scan(
        tmp_path,
        surfaces,
        [],
        {"app.py": code},
        literals=[other],
        families=[("pkg.resources.chat.", "chat")],
    )
    assert wrong.findings == []  # same string, but the call is not that API


def test_api_family_map_for_openai() -> None:
    families = R.FAMILIES["openai"]
    assert R.family_of("openai.resources.audio.speech.Speech.create", families) == "audio.speech"
    assert (
        R.family_of("openai.resources.chat.completions.completions.Completions.create", families)
        == "chat"
    )
    assert (
        R.family_of("openai.resources.beta.realtime.realtime.Realtime.connect", families)
        == "realtime"
    )
    assert R.family_of("openai.resources.files.Files.create", families) is None


# --- files, ignore rules, parse errors -----------------------------------------------


def test_ignore_rules_and_default_skips(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    bad = SDK_CODE + "pkg.helper(1, b=2)\n"
    files = {
        "app.py": bad,
        "vendor/lib.py": bad,
        "generated/x_pb2.py": bad,
        ".venv/lib/site-packages/thing.py": bad,
        "node_modules/x/y.py": bad,
        "build/lib/app.py": bad,
        ".guardianignore": "vendor/\n*_pb2.py\n",
    }
    result = scan(tmp_path, surfaces, changes, files)
    assert [f.file for f in result.findings] == ["app.py"]
    assert result.files_total == 1  # counted after the ignore rules


def test_a_virtualenv_with_any_name_is_skipped(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    files = {
        "tooling/pyvenv.cfg": "home = /usr\n",
        "tooling/lib/x.py": SDK_CODE + "pkg.helper(1, b=2)\n",
    }
    assert scan(tmp_path, surfaces, changes, files).findings == []


def test_unparseable_file_is_skipped_and_the_rest_scanned(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    files = {"broken.py": SDK_CODE + "def (:\n", "ok.py": SDK_CODE + "pkg.helper(1, b=2)\n"}
    result = scan(tmp_path, surfaces, changes, files)
    assert [f.file for f in result.findings] == ["ok.py"]
    assert result.skipped == [("broken.py", "syntax error at line 2")]


def test_repo_that_imports_the_sdk_but_uses_nothing_changed(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    code = SDK_CODE + 'c = pkg.Client("k")\nc.create("x", model="m")\nc.close()\n'
    result = scan(tmp_path, surfaces, changes, {"app.py": code})
    assert result.findings == [] and result.files_parsed == 1


def test_module_names() -> None:
    assert module_names("app/clients.py") == ["app.clients"]
    assert module_names("app/__init__.py") == ["app"]
    assert module_names("src/app/x.py") == ["src.app.x", "app.x"]
    assert module_names("__init__.py") == []


def test_files_are_listed_in_a_stable_order(tmp_path: Path) -> None:
    for name in ("b.py", "a.py", "pkg/z.py", "pkg/c.py", "notes.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    assert [f.relative for f in python_files(tmp_path)] == ["a.py", "b.py", "pkg/c.py", "pkg/z.py"]


# --- performance ---------------------------------------------------------------------


def test_500_file_repo_scans_well_under_30_seconds(
    tmp_path: Path, surfaces: tuple[Surface, Surface], changes: list[GradedChange]
) -> None:
    """F4.10. 50 files use the SDK, 450 do not: a typical repository's shape."""
    files: dict[str, str] = {}
    for n in range(450):
        files[f"app/mod{n}.py"] = "import json\n\n\n" + "".join(
            f"def f{i}(x):\n    return json.dumps({{'v': x, 'i': {i}}})\n\n" for i in range(20)
        )
    for n in range(50):
        files[f"app/sdk{n}.py"] = SDK_CODE + "".join(
            f'c{i} = pkg.Client("k")\n'
            f'c{i}.create("x", engine="e")\n'
            f'c{i}.chat.complete(model="m", mode="x")\n'
            for i in range(10)
        )
    started = time.perf_counter()
    result = scan(tmp_path, surfaces, changes, files)
    elapsed = time.perf_counter() - started
    assert result.files_total == 500 and result.files_parsed == 50
    assert len(result.findings) == 500
    assert elapsed < 30, elapsed
    print(f"\n500 files scanned in {elapsed:.1f}s")


# --- storage and the run -------------------------------------------------------------


def test_findings_are_stored_and_a_rescan_replaces_them(
    ctx: AppContext,
    tmp_path: Path,
    surfaces: tuple[Surface, Surface],
    changes: list[GradedChange],
) -> None:
    files = {"app.py": SDK_CODE + "pkg.helper(1, b=2)\nr = pkg.Reversal()\n"}
    result = scan(tmp_path, surfaces, changes, files)

    def store() -> int:
        return record_scan(
            ctx.sessions,
            repo="local:/repo",
            installation_id=LOCAL_INSTALLATION,
            target="pkg@2.0.0",
            head="abc",
            package="pkg",
            from_version="1.0.0",
            to_version="2.0.0",
            changes=changes,
            result=result,
            outcome=NOT_IMPLEMENTED,
        )

    first, second = store(), store()
    assert first == second  # same run key: the same run
    with session_scope(ctx.sessions) as session:
        rows = list(session.scalars(select(Finding).order_by(Finding.line)))
        records = list(session.scalars(select(ChangeRecord)))
        run = session.get(Run, first)
        assert run is not None and (run.from_version, run.to_version) == ("1.0.0", "2.0.0")
    assert [(r.file, r.line, r.match_tier) for r in rows] == [
        ("app.py", 2, DIRECT),
        ("app.py", 3, DIRECT),
    ]
    assert {r.version for r in records} == {"1.0.0..2.0.0"} and len(records) == 2


def _as_openai(files: dict[str, str]) -> dict[str, str]:
    return {
        name.replace("pkg", "openai"): text.replace("pkg", "openai") for name, text in files.items()
    }


def test_run_scans_a_local_checkout_end_to_end(
    ctx: AppContext, github: FakeGitHub, pypi: FakePyPI, tmp_path: Path
) -> None:
    pypi.add_wheel("openai", "1.0.0", _as_openai(V1))
    pypi.add_wheel("openai", "2.0.0", _as_openai(V2))
    github.add_release(OPENAI, "v1.0.0", "## 1.0.0\n")
    github.add_release(OPENAI, "v2.0.0", "## 2.0.0\n\n* changes\n")
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (repo / "app" / "llm.py").write_text(
        'import openai\nc = openai.Client("k")\nc.create("x", engine="e")\nc.request("get", "/")\n',
        encoding="utf-8",
    )
    ctx.llm.provider = __import__("guardian.common.llm", fromlist=["x"]).UnconfiguredProvider()
    request = RunRequest(repo=str(repo), target="openai@latest", package="openai")

    result = pipeline.run(request, ctx)

    assert (result.outcome, result.findings, result.finding_sites) == (NOT_IMPLEMENTED, 2, 2)
    assert result.findings_by_tier == {CLIENT_VARIABLE: 2}
    assert (result.files_scanned, result.files_parsed) == (1, 1)
    assert result.run_id is not None
    with session_scope(ctx.sessions) as session:
        rows = list(session.scalars(select(Finding).order_by(Finding.line)))
        assert [(r.line, r.fix_status, r.fix_tier) for r in rows] == [
            (3, "fixed", "codemod"),  # engine= -> model=
            (4, "report-only", None),  # a removed method: nothing to rename it to
        ]
    # Spec 05: one patch with the bump and the codemod; the removed method is reported.
    assert (result.patch_status, result.fixed, result.report_only) == ("partial", 1, 1)
    assert result.patch_files == ["app/llm.py", "requirements.txt"]
    assert result.patch_ref is not None
    patch = ctx.blobs.get(result.patch_ref).decode("utf-8")
    assert "+openai==2.0.0" in patch and '+c.create("x", model="e")' in patch
    assert result.reason == "patch built; verification is not built yet (spec 06)"

    (repo / "app" / "llm.py").write_text(
        "import openai\nopenai.Client('k').close()\n", encoding="utf-8"
    )
    clean = pipeline.run(request, ctx)
    assert (clean.outcome, clean.findings) == (NO_FINDINGS, 0)
