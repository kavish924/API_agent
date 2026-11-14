"""API surface, comparison, reconciliation and the diff gate. Covers spec 03."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from guardian import pipeline
from guardian.apidiff import compare as cmp
from guardian.apidiff.compare import ApiDiff, Change, compare
from guardian.apidiff.gate import make_diff_assessor
from guardian.apidiff.reconcile import (
    FROM_DIFF,
    FROM_NOTES,
    HIGH,
    MEDIUM,
    SYMBOL_NOT_IN_OLD_VERSION,
    reconcile,
)
from guardian.apidiff.service import ApiDiffService
from guardian.apidiff.surface import CLASS, FUNCTION, KEY, Surface, SurfaceError, build_surface
from guardian.apidiff.wheels import WheelError, fetch_wheel, unpack_wheel
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.models import Repo
from guardian.extract.schema import ChangeType, ExtractedChange
from guardian.pipeline.types import GATE_API_DIFF_ONLY, POLL_PARTIAL
from guardian.watch.sources import SdkSource
from tests.apifixtures import V1, V2, write_tree
from tests.fakes import FakeGitHub, FakePyPI

PKG = SdkSource(id="pkg-python", provider="acme", package="pkg", repo="acme/pkg-python")


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory: pytest.TempPathFactory) -> tuple[Surface, Surface]:
    root = tmp_path_factory.mktemp("pkg")
    old = build_surface(write_tree(root / "v1", V1), "pkg", "1.0.0")
    new = build_surface(write_tree(root / "v2", V2), "pkg", "2.0.0")
    return old, new


@pytest.fixture(scope="module")
def old(surfaces: tuple[Surface, Surface]) -> Surface:
    return surfaces[0]


@pytest.fixture(scope="module")
def new(surfaces: tuple[Surface, Surface]) -> Surface:
    return surfaces[1]


@pytest.fixture(scope="module")
def diff(old: Surface, new: Surface) -> ApiDiff:
    return compare(old, new)


def _one(diff: ApiDiff, kind: str, param: str | None = None, symbol: str | None = None) -> Change:
    found = [
        c
        for c in diff.changes
        if c.kind == kind and c.param == param and (symbol is None or c.symbol == symbol)
    ]
    assert len(found) == 1, [(c.kind, c.symbol, c.param) for c in diff.changes]
    return found[0]


# --- surface ------------------------------------------------------------------


def test_loading_never_executes_the_package(old: Surface) -> None:
    """The fixture's `__init__` raises on import. Building its surface did not."""
    assert "pkg.Client" in old.public


def test_public_names_are_followed_into_private_modules(old: Surface) -> None:
    assert old.public["pkg.Client"] == "pkg._client.Client"
    assert old.public["pkg.Reversal"] == "pkg._reversal.Reversal"
    assert old.public["pkg.types.ToolParam"] == "pkg.types.tool_param.ToolParam"
    assert old.public["pkg.helper"] == "pkg.helper"
    assert "pkg.VERSION" in old.public
    assert not any("._" in path for path in old.public)  # no private path is public


def test_imported_names_are_public_only_where_reexported(old: Surface) -> None:
    assert "pkg.resources.chat.Chat" in old.public  # defined there
    assert "pkg.resources.chat.is_given" not in old.public  # merely imported there
    assert "pkg.resources.chat.NOT_GIVEN" not in old.public
    assert "pkg.Timeout" not in old.public  # an alias to another package


def test_only_in_package_aliases_count_as_unresolved(old: Surface) -> None:
    assert old.unresolved == ["pkg.Gone"]  # `pkg._missing` does not exist; httpx is external


def test_typed_dicts_and_their_keys(old: Surface) -> None:
    tool = old.classes["pkg.types.tool_param.ToolParam"]
    assert tool.typed_dict
    assert tool.keys == {"type": True, "display_width": False, "environment": False}
    params = old.classes["pkg.params.RetrieveParams"]
    assert params.typed_dict  # inherited through RequestOptions
    assert old.all_keys(params.path) == {
        "expand": False,
        "payment_method_types": False,
        "api_key": False,
    }
    assert not old.classes["pkg._client.Client"].typed_dict


def test_resolve_follows_inheritance_and_typed_properties(old: Surface) -> None:
    inherited = old.resolve("pkg.Client.request")
    assert inherited is not None
    assert (inherited.kind, inherited.symbol) == (FUNCTION, "pkg._base.BaseResource.request")

    chained = old.resolve("Client.chat.complete")  # no package prefix, through a property
    assert chained is not None and chained.symbol == "pkg.resources.chat.Chat.complete"

    key = old.resolve("pkg.types.ToolParam.display_width")
    assert key is not None and key.kind == KEY

    cls = old.resolve("pkg._client.Client")  # a canonical path works too
    assert cls is not None and cls.kind == CLASS

    for missing in ("pkg.Client.nope", "pkg.DoesNotExist", "", "pkg.Client.create.deeper"):
        assert old.resolve(missing) is None


def test_lookup_gives_signature_and_docstring(old: Surface, new: Surface) -> None:
    create = old.lookup("pkg.Client.create")
    assert create is not None
    assert create.signature == "create(name: str, *, engine: str, retries: int = 2)"

    helper = old.lookup("pkg.helper")
    assert helper is not None and (helper.signature, helper.doc) == (
        "helper(a, b = 1)",
        "Add things.",
    )

    client = old.lookup("pkg.Client")
    assert client is not None and client.doc == "The client."
    assert client.signature == "Client(api_key: str, *, timeout: float | NotGiven = NOT_GIVEN)"

    retrieve = new.lookup("pkg.Client.retrieve")
    assert retrieve is not None
    assert retrieve.signature == "retrieve(id: str, /, **params: Unpack[RetrieveParams])"

    assert old.lookup("pkg.Client.does_not_exist") is None


def test_surface_round_trips_through_json(old: Surface) -> None:
    restored = Surface.from_json(old.to_json())
    assert restored == old
    assert restored.resolve("pkg.Client.request") == old.resolve("pkg.Client.request")
    with pytest.raises(SurfaceError):
        Surface.from_json('{"format": 0}')


def test_missing_package_is_a_surface_error(tmp_path: Path) -> None:
    with pytest.raises(SurfaceError):
        build_surface(tmp_path, "nothing_here", "1.0.0")


# --- comparison ---------------------------------------------------------------


def test_identical_surfaces_have_no_changes(old: Surface) -> None:
    same = compare(old, old)
    assert same.changes == [] and not same.breaking


def test_exactly_the_expected_changes_are_reported(diff: ApiDiff) -> None:
    assert sorted((c.kind, c.display, c.param) for c in diff.changes) == [
        (cmp.KWARG_REMOVED, "pkg.Client.retrieve", "payment_method_types"),
        (cmp.METHOD_REMOVED, "pkg.Other.request", None),  # display = shortest public path
        (cmp.NAME_REMOVED, "pkg.OldError", None),
        (cmp.NAME_REMOVED, "pkg.Reversal", None),
        (cmp.PARAM_KIND, "pkg.Client.retrieve", "id"),
        (cmp.PARAM_REMOVED, "pkg.helper", "b"),
        (cmp.PARAM_RENAMED, "pkg.Client.create", "engine"),
        (cmp.PARAM_REQUIRED, "pkg.resources.chat.Chat.complete", "mode"),
        (cmp.KEY_REMOVED, "pkg.RetrieveParams", "payment_method_types"),
        (cmp.KEY_REMOVED, "pkg.types.ToolParam", "display_width"),
        (cmp.KEY_REQUIRED, "pkg.types.ToolParam", "environment"),
    ]
    assert diff.breaking


def test_renamed_class_is_paired_with_its_new_name(diff: ApiDiff) -> None:
    renamed = _one(diff, cmp.NAME_REMOVED, symbol="pkg._reversal.Reversal")
    assert (renamed.new_name, renamed.candidate) == ("pkg.TransferReversal", True)
    gone = _one(diff, cmp.NAME_REMOVED, symbol="pkg._errors.OldError")
    assert (gone.new_name, gone.candidate) == (None, False)


def test_inherited_method_removal_is_reported_once(diff: ApiDiff) -> None:
    removed = _one(diff, cmp.METHOD_REMOVED)
    assert removed.symbol == "pkg._base.BaseResource.request"  # the declaring class
    assert removed.public == ("pkg.Client.request", "pkg.Other.request")  # everyone who lost it


def test_parameter_changes(diff: ApiDiff) -> None:
    renamed = _one(diff, cmp.PARAM_RENAMED, "engine")
    assert (renamed.symbol, renamed.new_name) == ("pkg._client.Client.create", "model")
    kind = _one(diff, cmp.PARAM_KIND, "id")
    assert kind.detail == "positional or keyword -> positional-only"
    assert _one(diff, cmp.PARAM_REQUIRED, "mode").detail == "new required parameter"
    # `legacy` lost `x` but still takes **kwargs, so a keyword caller is not broken.
    assert not [c for c in diff.changes if c.symbol.endswith(".legacy")]


def test_typed_dict_key_changes(diff: ApiDiff) -> None:
    key = _one(diff, cmp.KEY_REMOVED, "payment_method_types")
    assert key.public == ("pkg.RetrieveParams", "pkg.params.RetrieveParams")
    kwarg = _one(diff, cmp.KWARG_REMOVED, "payment_method_types")
    assert kwarg.symbol == "pkg._client.Client.retrieve"  # found on the method that takes it
    assert _one(diff, cmp.KEY_REMOVED, "display_width").symbol == "pkg.types.tool_param.ToolParam"
    assert _one(diff, cmp.KEY_REQUIRED, "environment")


def test_noise_is_counted_not_reported(diff: ApiDiff) -> None:
    assert diff.ignored == {
        cmp.IGNORE_ATTRIBUTE_REMOVED: 1,  # Response.client_secret
        cmp.IGNORE_CLASS_TO_ALIAS: 2,  # Thing, under both of its public paths
        cmp.IGNORE_DEFAULT_CHANGED: 1,  # n = 1 -> 3
        cmp.IGNORE_PLACEHOLDER_DEFAULT: 2,  # NOT_GIVEN -> not_given, twice
    }
    assert (diff.unresolved_old, diff.unresolved_new) == (1, 1)


def test_release_with_only_noise_is_not_breaking(tmp_path: Path) -> None:
    noisy = {
        **V1,
        "pkg/_client.py": V1["pkg/_client.py"]
        .replace("= NOT_GIVEN)", "= not_given)")
        .replace(
            "from pkg._types import NOT_GIVEN, NotGiven",
            "from pkg._types import NotGiven, not_given",
        ),
        "pkg/types/shared.py": V2["pkg/types/shared.py"],
        "pkg/types/response.py": V2["pkg/types/response.py"],
    }
    before = build_surface(write_tree(tmp_path / "a", V1), "pkg", "1.0.0")
    after = build_surface(write_tree(tmp_path / "b", noisy), "pkg", "1.0.1")
    result = compare(before, after)
    assert not result.breaking
    assert sum(result.ignored.values()) == 4


def test_diff_round_trips_through_json(diff: ApiDiff) -> None:
    assert ApiDiff.from_json(diff.to_json()) == diff


# --- wheels and the cached service ----------------------------------------------


def test_surface_is_built_once_then_served_from_the_blob_store(
    ctx: AppContext, pypi: FakePyPI
) -> None:
    pypi.add_wheel("pkg", "1.0.0", V1)
    pypi.add_wheel("pkg", "2.0.0", V2)
    service = ApiDiffService.from_context(ctx)

    first = service.diff(PKG, "1.0.0", "2.0.0")
    assert first.breaking and len(pypi.requests) == 4  # metadata + wheel, twice
    assert ctx.blobs.exists(service.surface_key(PKG, "1.0.0"))
    assert (
        ApiDiff.from_json(ctx.blobs.get(service.diff_key(PKG, "1.0.0", "2.0.0")).decode()) == first
    )

    pypi.requests.clear()
    again = ApiDiffService.from_context(ctx)  # a new run: nothing in memory
    assert again.diff(PKG, "1.0.0", "2.0.0") == first
    lookup = again.lookup(PKG, "2.0.0", "pkg.Client.create")
    assert lookup is not None and "model: str" in (lookup.signature or "")
    assert pypi.requests == []  # PyPI was contacted only for the first run


def test_wheel_with_a_wrong_hash_is_rejected(
    ctx: AppContext, pypi: FakePyPI, tmp_path: Path
) -> None:
    pypi.add_wheel("pkg", "1.0.0", V1)
    pypi.corrupt.add(("pkg", "1.0.0"))
    with pytest.raises(WheelError, match="sha256"):
        fetch_wheel(ctx.http, "pkg", "1.0.0", tmp_path / "out", sleep=ctx.sleep)
    assert not (tmp_path / "out").exists()


def test_release_without_a_wheel_is_an_error_not_an_sdist_build(
    ctx: AppContext, pypi: FakePyPI, tmp_path: Path
) -> None:
    pypi.sdist_only.add(("pkg", "0.1.0"))
    with pytest.raises(WheelError, match="no wheel"):
        fetch_wheel(ctx.http, "pkg", "0.1.0", tmp_path / "out", sleep=ctx.sleep)
    with pytest.raises(WheelError, match="404"):
        fetch_wheel(ctx.http, "pkg", "9.9.9", tmp_path / "out", sleep=ctx.sleep)


@pytest.mark.parametrize(
    "member", ["../escape.py", "/abs/escape.py", "pkg\\..\\escape.py", "C:/x.py"]
)
def test_unsafe_paths_in_a_wheel_are_refused(tmp_path: Path, member: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg/__init__.py", "")
        archive.writestr(member, "x = 1")
    with pytest.raises(WheelError, match="unsafe path"):
        unpack_wheel(buffer.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "escape.py").exists()
    with pytest.raises(WheelError, match="not a zip"):
        unpack_wheel(b"definitely not a zip", tmp_path / "out")


# --- reconciliation -------------------------------------------------------------


def _record(symbol: str, change_type: ChangeType, **fields: object) -> ExtractedChange:
    return ExtractedChange.model_validate(
        {
            "provider": "acme",
            "package": "pkg",
            "version": "2.0.0",
            "change_type": change_type,
            "qualified_symbol": symbol,
            "evidence_quote": "quoted from the release note",
            **fields,
        }
    )


def test_record_confirmed_by_the_diff_is_high_confidence(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record(
        "pkg.Client.create",
        ChangeType.SIGNATURE_CHANGED,
        affected_parameter="engine",
        replacement="model",
        fixable=True,
    )
    graded = reconcile([record], old, new, diff).changes[0]
    assert (graded.origin, graded.confidence, graded.fixable) == (FROM_NOTES, HIGH, True)
    assert graded.symbol == "pkg._client.Client.create"
    assert graded.replacement_verified and graded.diff_kinds == (cmp.PARAM_RENAMED,)
    assert graded.evidence_quote == "quoted from the release note"


def test_record_the_diff_cannot_see_is_kept_at_medium(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record("pkg.Client.chat.complete", ChangeType.BEHAVIOR_CHANGED, fixable=True)
    result = reconcile([record], old, new, diff)
    graded = next(c for c in result.changes if c.origin == FROM_NOTES)
    assert graded.confidence == MEDIUM and result.dropped == []
    assert not graded.fixable  # a behaviour change is never fixable, whatever the model said


def test_record_about_a_symbol_that_never_existed_is_dropped(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record("pkg.Client.does_not_exist", ChangeType.REMOVED)
    result = reconcile([record], old, new, diff)
    assert [(d.record.qualified_symbol, d.reason) for d in result.dropped] == [
        ("pkg.Client.does_not_exist", SYMBOL_NOT_IN_OLD_VERSION)
    ]
    assert all(c.origin == FROM_DIFF for c in result.changes)


def test_removal_with_an_existing_replacement_is_a_rename(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record(
        "pkg.Reversal", ChangeType.REMOVED, replacement="pkg.TransferReversal", fixable=True
    )
    graded = reconcile([record], old, new, diff).changes[0]
    assert graded.change_type == ChangeType.RENAMED
    assert (graded.confidence, graded.fixable, graded.replacement_verified) == (HIGH, True, True)


def test_replacement_that_does_not_exist_is_never_fixable(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record("pkg.Reversal", ChangeType.RENAMED, replacement="pkg.Imaginary", fixable=True)
    graded = reconcile([record], old, new, diff).changes[0]
    assert not graded.replacement_verified and not graded.fixable
    assert graded.confidence == HIGH  # the removal itself is still confirmed


def test_inherited_method_record_matches_the_declaring_class(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    record = _record("pkg.Other.request", ChangeType.REMOVED)
    result = reconcile([record], old, new, diff)
    graded = result.changes[0]
    assert (graded.symbol, graded.confidence) == ("pkg._base.BaseResource.request", HIGH)
    # The diff's own entry for it is not repeated as a diff-only record.
    assert not [c for c in result.changes if c.origin == FROM_DIFF and c.symbol == graded.symbol]


def test_breaks_no_note_mentions_become_diff_records(
    old: Surface, new: Surface, diff: ApiDiff
) -> None:
    result = reconcile([], old, new, diff)
    assert len(result.changes) == len(diff.changes)
    assert all(c.origin == FROM_DIFF and c.evidence_quote is None for c in result.changes)
    by_symbol = {(c.public_symbol, c.affected_parameter): c for c in result.changes}

    unannounced = by_symbol[("pkg.OldError", None)]
    assert (unannounced.change_type, unannounced.confidence, unannounced.fixable) == (
        ChangeType.REMOVED,
        HIGH,
        False,
    )
    candidate = by_symbol[("pkg.Reversal", None)]  # inferred from the names: medium
    assert (candidate.change_type, candidate.confidence, candidate.replacement) == (
        ChangeType.RENAMED,
        MEDIUM,
        "pkg.TransferReversal",
    )
    renamed = by_symbol[("pkg.Client.create", "engine")]
    assert (renamed.fixable, renamed.replacement) == (True, "model")
    assert by_symbol[("pkg.Client.retrieve", "id")].fixable  # keyword -> positional: a codemod
    assert not by_symbol[("pkg.helper", "b")].fixable


# --- the API-diff gate ----------------------------------------------------------


def _watch_pkg(ctx: AppContext, github: FakeGitHub, pypi: FakePyPI) -> None:
    pypi.add_wheel("pkg", "1.0.0", V1)
    github.add_release(PKG.repo, "v1.0.0", "## 1.0.0\n\n* first release\n")
    pipeline.poll(ctx, sources=(PKG,))  # baseline
    with session_scope(ctx.sessions) as session:
        session.add(Repo(installation_id=1, full_name="octo/app"))


def test_release_the_notes_call_harmless_still_passes_the_gate(
    ctx: AppContext, github: FakeGitHub, pypi: FakePyPI
) -> None:
    _watch_pkg(ctx, github, pypi)
    pypi.add_wheel("pkg", "2.0.0", V2)
    github.add_release(PKG.repo, "v2.0.0", "## 2.0.0\n\n* **chore:** internal cleanup\n")

    result = pipeline.poll(
        ctx,
        sources=(PKG,),
        assessor=make_diff_assessor(ctx, sources=(PKG,)),
        gate_label=GATE_API_DIFF_ONLY,
    )

    assert (result.new_documents, result.gate, result.runs_enqueued) == (1, GATE_API_DIFF_ONLY, 1)


def test_release_that_changes_no_api_starts_no_runs(
    ctx: AppContext, github: FakeGitHub, pypi: FakePyPI
) -> None:
    _watch_pkg(ctx, github, pypi)
    pypi.add_wheel("pkg", "1.0.1", V1)  # same API
    github.add_release(PKG.repo, "v1.0.1", "## 1.0.1\n\n* **fix:** typo\n")

    result = pipeline.poll(ctx, sources=(PKG,), assessor=make_diff_assessor(ctx, sources=(PKG,)))

    assert (result.new_documents, result.runs_enqueued, result.errors) == (1, 0, [])


def test_gate_failure_is_reported_and_starts_no_runs(
    ctx: AppContext, github: FakeGitHub, pypi: FakePyPI
) -> None:
    _watch_pkg(ctx, github, pypi)
    github.add_release(PKG.repo, "v2.0.0", "## 2.0.0\n\n* something\n")  # no wheel on PyPI

    result = pipeline.poll(ctx, sources=(PKG,), assessor=make_diff_assessor(ctx, sources=(PKG,)))

    assert result.outcome == POLL_PARTIAL and result.runs_enqueued == 0
    assert len(result.errors) == 1 and result.errors[0].startswith("gate for pkg 2.0.0:")
