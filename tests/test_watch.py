"""Change watching end to end against a fake GitHub. Covers F1.2–F1.9, F1.13–F1.17."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from guardian import pipeline
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.jobs import InProcessQueue, JobType
from guardian.common.models import Repo, Source, SourceDocument
from guardian.pipeline import Assessment
from guardian.pipeline.types import (
    GATE_APPLIED,
    GATE_NOT_NEEDED,
    GATE_UNAVAILABLE,
    NOT_IMPLEMENTED,
    POLL_OK,
    POLL_PARTIAL,
    RunRequest,
)
from guardian.watch import github_client
from guardian.watch.depfiles import NOT_PINNED, NOT_USED, LocalRepoFiles
from guardian.watch.documents import ORIGIN_CHANGELOG, ORIGIN_RELEASE, StoredDocument
from guardian.watch.index import ReleaseIndex
from guardian.watch.range import OK, RANGE_TOO_LARGE, UP_TO_DATE, resolve_range
from guardian.watch.sources import SDK_SOURCES, source_for_package
from tests.fakes import OPENAI, STRIPE, FakeGitHub
from tests.test_changelog import STRIPE_POINTER

FIXTURES = Path(__file__).parent / "fixtures"
OPENAI_SOURCE, STRIPE_SOURCE = SDK_SOURCES

NOTE = "## {v}\n\n### Features\n\n* **api:** something new in {v}\n"


def _note(version: str) -> str:
    return NOTE.format(v=version)


def _documents(ctx: AppContext) -> list[SourceDocument]:
    with session_scope(ctx.sessions) as session:
        return list(session.scalars(select(SourceDocument).order_by(SourceDocument.id)))


def _baseline(ctx: AppContext, github: FakeGitHub) -> None:
    """A first poll, so later releases count as new."""
    pipeline.poll(ctx)
    github.reset_log()


def _breaking(_document: StoredDocument) -> Assessment:
    return Assessment(breaking=True)


def _repo_dir(tmp_path: Path, **files: str) -> LocalRepoFiles:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return LocalRepoFiles(root)


# --- polling ------------------------------------------------------------------


def test_first_poll_is_a_baseline_and_announces_nothing(
    ctx: AppContext, github: FakeGitHub
) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))

    result = pipeline.poll(ctx)

    assert result.outcome == POLL_OK
    assert (result.sources_checked, result.baselined, result.new_documents) == (2, 2, 0)
    assert result.gate == GATE_NOT_NEEDED
    assert _documents(ctx) == []
    with session_scope(ctx.sessions) as session:
        row = session.get(Source, OPENAI_SOURCE.id)
        assert row is not None and row.last_seen == "1.1.0" and row.etag


def test_new_release_is_stored_byte_for_byte(ctx: AppContext, github: FakeGitHub) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    body = "## 1.1.0\r\n\r\n* renamed `engine` → `model`\r\n* trailing spaces   \r\n"
    github.add_release(OPENAI, "v1.1.0", body)

    result = pipeline.poll(ctx)

    assert (result.new_documents, result.edited_documents, result.runs_enqueued) == (1, 0, 0)
    assert result.gate == GATE_UNAVAILABLE  # no assessor yet: nothing may fan out
    (document,) = _documents(ctx)
    assert (document.source_id, document.version, document.revision) == (
        "openai-python",
        "1.1.0",
        1,
    )
    assert ctx.blobs.get(document.blob_ref) == body.encode()
    assert document.blob_ref.endswith(f".{ORIGIN_RELEASE}.md")


def test_second_poll_costs_one_conditional_request_per_source(
    ctx: AppContext, github: FakeGitHub
) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))
    pipeline.poll(ctx)
    github.reset_log()

    result = pipeline.poll(ctx)

    assert (result.sources_unchanged, result.new_documents) == (2, 0)
    assert github.statuses == [304, 304]
    assert all(request.headers.get("if-none-match") for request in github.requests)
    assert len(_documents(ctx)) == 1


def test_prereleases_and_drafts_are_not_announced(ctx: AppContext, github: FakeGitHub) -> None:
    github.add_release(STRIPE, "v12.0.0", _note("12.0.0"))
    _baseline(ctx, github)
    github.add_release(STRIPE, "v12.1.0b1", _note("12.1.0b1"))
    github.add_release(STRIPE, "v12.1.0", _note("12.1.0"), draft=True)
    github.add_release(STRIPE, "v12.2.0", _note("12.2.0"), prerelease=True)
    github.add_release(STRIPE, "nightly", "not a version")

    assert pipeline.poll(ctx).new_documents == 0
    assert _documents(ctx) == []


def test_edited_note_becomes_a_new_revision(ctx: AppContext, github: FakeGitHub) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))
    pipeline.poll(ctx)
    github.edit_release(OPENAI, "v1.1.0", _note("1.1.0") + "\n* one more line\n")
    github.edit_release(OPENAI, "v1.0.0", "edited, but nobody uses this note yet")

    result = pipeline.poll(ctx)

    assert (result.new_documents, result.edited_documents) == (0, 1)
    first, second = _documents(ctx)
    assert (first.version, first.revision) == ("1.1.0", 1)
    assert (second.version, second.revision) == ("1.1.0", 2)
    assert ctx.blobs.exists(first.blob_ref)  # revision 1 is kept


def test_rate_limit_is_retried(ctx: AppContext, github: FakeGitHub, slept: list[float]) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    github.rate_limit_once.add(OPENAI)

    result = pipeline.poll(ctx)

    assert result.outcome == POLL_OK and result.baselined == 1
    assert slept == [3.0]  # honoured Retry-After


def test_failing_source_does_not_stop_the_others(ctx: AppContext, github: FakeGitHub) -> None:
    github.add_release(STRIPE, "v12.0.0", _note("12.0.0"))
    github.fail_with[OPENAI] = 500

    result = pipeline.poll(ctx)

    assert result.outcome == POLL_PARTIAL
    assert len(result.errors) == 1 and result.errors[0].startswith("openai-python:")
    assert result.baselined == 1  # stripe was still polled


def test_release_left_unhandled_by_a_crash_is_picked_up(
    ctx: AppContext, github: FakeGitHub
) -> None:
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))
    # Simulate a crash after the index was refreshed but before the note was stored.
    ReleaseIndex(OPENAI_SOURCE, github_client(ctx), ctx.blobs).refresh()

    result = pipeline.poll(ctx)  # the list is unchanged now, so this poll gets a 304

    assert result.sources_unchanged == 2
    assert result.new_documents == 1
    assert pipeline.poll(ctx).new_documents == 0


# --- changelog fallback -------------------------------------------------------


def test_stripe_pointer_release_uses_the_changelog_section(
    ctx: AppContext, github: FakeGitHub
) -> None:
    """Regression against real data: Stripe 16.0.0's rename exists only in CHANGELOG.md."""
    changelog = (FIXTURES / "stripe_changelog_excerpt.md").read_text(encoding="utf-8")
    github.set_file(STRIPE, "CHANGELOG.md", changelog)
    github.add_release(STRIPE, "v15.6.1", _note("15.6.1"))
    _baseline(ctx, github)
    github.add_release(STRIPE, "v16.0.0", STRIPE_POINTER)

    result = pipeline.poll(ctx)

    assert (result.new_documents, result.gaps) == (1, 0)
    (document,) = _documents(ctx)
    assert document.blob_ref.endswith(f".{ORIGIN_CHANGELOG}.md")
    text = ctx.blobs.get(document.blob_ref).decode()
    assert "Rename `stripe.Reversal` to `stripe.TransferReversal`" in text
    assert "See [the changelog]" not in text

    # The changelog is cached: the next pointer release revalidates it with a 304.
    github.reset_log()
    github.add_release(STRIPE, "v17.0.0", STRIPE_POINTER.replace("16-0-0", "17-0-0"))
    assert pipeline.poll(ctx).new_documents == 1
    changelog_statuses = [
        status
        for request, status in zip(github.requests, github.statuses, strict=True)
        if request.url.path.endswith("CHANGELOG.md")
    ]
    assert changelog_statuses == [304]


def test_version_with_no_note_anywhere_is_a_gap(ctx: AppContext, github: FakeGitHub) -> None:
    github.set_file(STRIPE, "CHANGELOG.md", "# Changelog\n\n## 1.0.0\n\n- first\n")
    github.add_release(STRIPE, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(STRIPE, "v2.0.0", "")  # the changelog has no 2.0.0 section
    github.add_release(OPENAI, "v1.0.0", "")  # this repo has no changelog file at all

    result = pipeline.poll(ctx)

    assert result.outcome == POLL_OK
    assert result.gaps == 2
    assert result.new_documents == 0
    assert _documents(ctx) == []
    assert pipeline.poll(ctx).gaps == 0  # a gap is reported once, not on every poll


# --- gate and fan-out ---------------------------------------------------------


def _install(ctx: AppContext, *names: str, active: bool = True) -> None:
    with session_scope(ctx.sessions) as session:
        for name in names:
            session.add(Repo(installation_id=7, full_name=name, active=active))


def test_release_that_passes_the_gate_fans_out_to_active_repos(
    ctx: AppContext, github: FakeGitHub
) -> None:
    _install(ctx, "a/one", "b/two", "c/three")
    _install(ctx, "d/uninstalled", active=False)
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))

    result = pipeline.poll(ctx, assessor=_breaking)

    assert (result.gate, result.runs_enqueued) == (GATE_APPLIED, 3)
    queue = ctx.queue
    assert isinstance(queue, InProcessQueue) and len(queue) == 3
    seen: list[RunRequest] = []
    queue.register(JobType.RUN, lambda job: seen.append(RunRequest.model_validate(job.payload)))
    queue.drain()
    assert [request.repo for request in seen] == ["a/one", "b/two", "c/three"]
    assert {request.target for request in seen} == {"openai@1.1.0"}
    assert seen[0].installation_id == 7


def test_release_with_nothing_breaking_starts_no_runs(ctx: AppContext, github: FakeGitHub) -> None:
    _install(ctx, "a/one")
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))

    result = pipeline.poll(ctx, assessor=lambda document: Assessment(breaking=False))

    assert (result.new_documents, result.gate, result.runs_enqueued) == (1, GATE_APPLIED, 0)


def test_edited_note_with_the_same_records_starts_no_runs(
    ctx: AppContext, github: FakeGitHub
) -> None:
    _install(ctx, "a/one")
    github.add_release(OPENAI, "v1.0.0", _note("1.0.0"))
    _baseline(ctx, github)
    github.add_release(OPENAI, "v1.1.0", _note("1.1.0"))
    assert pipeline.poll(ctx, assessor=_breaking).runs_enqueued == 1
    github.edit_release(OPENAI, "v1.1.0", _note("1.1.0") + "\ntypo fixed\n")

    same = pipeline.poll(
        ctx, assessor=lambda document: Assessment(breaking=True, records_changed=False)
    )
    assert (same.edited_documents, same.runs_enqueued) == (1, 0)

    github.edit_release(OPENAI, "v1.1.0", _note("1.1.0") + "\n* BREAKING: removed `x`\n")
    assert pipeline.poll(ctx, assessor=_breaking).runs_enqueued == 1


# --- version range ------------------------------------------------------------


def _releases(github: FakeGitHub, repo: str, *versions: str) -> None:
    for version in versions:
        github.add_release(repo, f"v{version}", _note(version))


def test_range_from_requirements_pin_to_latest(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    _releases(github, OPENAI, "1.29.0", "1.30.0", "1.31.0", "1.32.0", "1.33.0", "1.34.0", "1.35.0")
    files = _repo_dir(tmp_path, **{"requirements.txt": "openai==1.30.0\n"})

    result = resolve_range(ctx, OPENAI_SOURCE, files)

    assert (result.outcome, result.old, result.new) == (OK, "1.30.0", "1.35.0")
    assert result.decided_by == "requirements.txt"
    assert [d.version for d in result.documents] == [
        "1.31.0",
        "1.32.0",
        "1.33.0",
        "1.34.0",
        "1.35.0",
    ]
    assert (result.versions, result.stored, result.gaps) == (5, 5, [])

    # A second run stores nothing and only revalidates the release list.
    github.reset_log()
    again = resolve_range(ctx, OPENAI_SOURCE, files)
    assert (again.stored, len(again.documents)) == (0, 5)
    assert github.statuses == [304]


def test_lock_file_decides_the_old_version(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    _releases(github, OPENAI, "1.30.0", "1.31.0")
    files = _repo_dir(
        tmp_path,
        **{
            "pyproject.toml": '[project]\ndependencies = ["openai>=1.0"]\n',
            "uv.lock": '[[package]]\nname = "openai"\nversion = "1.30.0"\n',
        },
    )
    result = resolve_range(ctx, OPENAI_SOURCE, files)
    assert (result.old, result.new, result.decided_by) == ("1.30.0", "1.31.0", "uv.lock")


def test_repo_that_does_not_use_the_package_ends_without_any_request(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    files = _repo_dir(tmp_path, **{"requirements.txt": "requests==2.0\n"})
    result = resolve_range(ctx, OPENAI_SOURCE, files)
    assert result.outcome == NOT_USED
    assert github.requests == []


def test_unpinned_and_up_to_date_end_the_run(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    _releases(github, OPENAI, "1.30.0", "1.31.0")
    unpinned = _repo_dir(tmp_path, **{"requirements.txt": "openai>=1.0\n"})
    assert resolve_range(ctx, OPENAI_SOURCE, unpinned).outcome == NOT_PINNED

    current = _repo_dir(tmp_path, **{"requirements.txt": "openai==1.31.0\n"})
    result = resolve_range(ctx, OPENAI_SOURCE, current)
    assert (result.outcome, result.old, result.new) == (UP_TO_DATE, "1.31.0", "1.31.0")
    assert _documents(ctx) == []


def test_range_cap(ctx: AppContext, github: FakeGitHub) -> None:
    _releases(github, OPENAI, *[f"1.{n}.0" for n in range(8)])
    result = resolve_range(ctx, OPENAI_SOURCE, None, from_version="1.0.0", cap=5)
    assert (result.outcome, result.versions) == (RANGE_TOO_LARGE, 7)
    assert _documents(ctx) == []


def test_range_reads_the_whole_history_across_pages_and_includes_backports(
    ctx: AppContext, tmp_path: Path
) -> None:
    github = FakeGitHub(page_size=2)
    ctx.http = github.client()
    _releases(github, STRIPE, "4.0.0", "4.1.0", "5.0.0", "5.1.0", "6.0.0")
    _releases(github, STRIPE, "4.1.1")  # a backport, published after 6.0.0
    github.add_release(STRIPE, "v6.1.0b1", _note("6.1.0b1"))

    result = resolve_range(ctx, STRIPE_SOURCE, None, from_version="4.1.0")

    assert result.new == "6.0.0"  # the pre-release is not "latest"
    assert [d.version for d in result.documents] == ["4.1.1", "5.0.0", "5.1.0", "6.0.0"]
    assert len([p for p in github.paths() if p.endswith("/releases")]) == 4  # 7 releases, 2 a page

    beta = resolve_range(ctx, STRIPE_SOURCE, None, from_version="6.0.0", to_version="6.1.0b1")
    assert beta.outcome == OK and beta.versions == 0  # stable pin: pre-releases are skipped
    pinned_beta = resolve_range(ctx, STRIPE_SOURCE, None, from_version="6.0.0rc1")
    assert [d.version for d in pinned_beta.documents] == ["6.0.0"]


def test_range_uses_the_changelog_for_pointer_releases(ctx: AppContext, github: FakeGitHub) -> None:
    changelog = (FIXTURES / "stripe_changelog_excerpt.md").read_text(encoding="utf-8")
    github.set_file(STRIPE, "CHANGELOG.md", changelog)
    github.add_release(STRIPE, "v15.6.1", _note("15.6.1"))
    github.add_release(STRIPE, "v16.0.0", STRIPE_POINTER)
    github.add_release(STRIPE, "v16.0.5", STRIPE_POINTER)  # not in the changelog: a gap
    github.add_release(STRIPE, "v17.0.0", STRIPE_POINTER)

    result = resolve_range(ctx, STRIPE_SOURCE, None, from_version="15.6.1")

    assert [(d.version, d.origin) for d in result.documents] == [
        ("16.0.0", ORIGIN_CHANGELOG),
        ("17.0.0", ORIGIN_CHANGELOG),
    ]
    assert [gap.version for gap in result.gaps] == ["16.0.5"]
    assert len([p for p in github.paths() if p.endswith("CHANGELOG.md")]) == 1  # fetched once


# --- pipeline.run -------------------------------------------------------------


def test_run_on_a_local_checkout_resolves_the_range(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    _releases(github, OPENAI, "1.30.0", "1.31.0", "1.32.0")
    files = _repo_dir(tmp_path, **{"requirements.txt": "openai==1.30.0\n"})

    result = pipeline.run(
        RunRequest(repo=str(files.root), target="openai@latest", package="openai"), ctx
    )

    assert result.outcome == NOT_IMPLEMENTED  # later stages are not built
    assert (result.from_version, result.to_version) == ("1.30.0", "1.32.0")
    assert (result.pinned_in, result.notes, result.notes_stored, result.gaps) == (
        "requirements.txt",
        2,
        2,
        0,
    )


def test_run_ends_silently_with_a_reason(
    ctx: AppContext, github: FakeGitHub, tmp_path: Path
) -> None:
    files = _repo_dir(tmp_path, **{"requirements.txt": "requests==2.0\n"})
    request = RunRequest(repo=str(files.root), target="stripe@latest", package="stripe")
    assert pipeline.run(request, ctx).outcome == NOT_USED

    unknown = RunRequest(repo=str(files.root), target="twilio@latest", package="twilio")
    assert pipeline.run(unknown, ctx).outcome == pipeline.PACKAGE_NOT_WATCHED


def test_run_reports_a_source_error(ctx: AppContext, github: FakeGitHub) -> None:
    github.fail_with[OPENAI] = 500
    request = RunRequest(
        repo="not-a-local-path", target="openai@latest", package="openai", from_version="1.0.0"
    )
    result = pipeline.run(request, ctx)
    assert result.outcome == pipeline.SOURCE_ERROR
    assert result.reason is not None and "500" in result.reason


@pytest.mark.parametrize("package", ["openai", "OpenAI", "stripe"])
def test_watched_packages_are_found_by_name(package: str) -> None:
    assert source_for_package(package) is not None
