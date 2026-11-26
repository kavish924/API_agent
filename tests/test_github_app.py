"""GitHub App integration (spec 07) against an in-memory GitHub.

Covers the webhook, installation events, checkout, repo config, and delivery: draft
upgrade PRs, updates, closed-PR memory, the issue fallback, issue-only mode, partial
patches, the pause flag, duplicate delivery and API-level PRs.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from guardian import pipeline
from guardian.common.config import Settings
from guardian.common.context import AppContext, build_context
from guardian.common.db import session_scope
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.common.llm import StubProvider, UnconfiguredProvider
from guardian.common.models import Repo
from guardian.fix import FIXED, REPORT_ONLY, PatchSet
from guardian.fix.types import FindingFix
from guardian.github_app.checkout import CheckoutError, extract_tarball, fetch_checkout
from guardian.github_app.client import AppCredentials, GitHubApp
from guardian.github_app.config import RepoConfig, parse_config
from guardian.github_app.events import apply_event
from guardian.github_app.publish import (
    DECLINED,
    ISSUE_OPENED,
    ISSUE_UPDATED,
    PAUSED,
    PR_OPENED,
    PR_UPDATED,
    SKIPPED,
    Publisher,
)
from guardian.github_app.render import API, SDK, Report, ReportItem
from guardian.github_app.webhook import create_app, valid_signature
from guardian.pipeline.types import RunRequest
from guardian.verify.static import L0, L2, Verification
from tests.apifixtures import V1, V2
from tests.fake_github_app import APP_ID, FakeGitHubApp, make_key
from tests.fakes import OPENAI, FakeGitHub, FakePages, FakePyPI

REPO = "acme/app"
INSTALLATION = 7
SECRET = b"webhook-secret"


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    return make_key()


@pytest.fixture
def fake(keys: tuple[str, str]) -> FakeGitHubApp:
    return FakeGitHubApp(keys[1])


@pytest.fixture
def app_ctx(
    migrated_url: str,
    tmp_path: Path,
    stub: StubProvider,
    github: FakeGitHub,
    pypi: FakePyPI,
    fake: FakeGitHubApp,
    keys: tuple[str, str],
) -> Iterator[AppContext]:
    pages = FakePages()

    def route(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.github.com":
            owner_repo = "/".join(path.split("/")[2:4])
            if path.startswith("/app/") or owner_repo in fake.repos:
                return fake.handle(request)
            return github._handle(request)
        if host == "developers.openai.com":
            return pages.handle(request)
        return pypi.handle(request)

    http = httpx.Client(transport=httpx.MockTransport(route))
    settings = Settings(
        database_url=migrated_url,
        blob_dir=tmp_path / "blobs",
        work_dir=tmp_path / "work",
        llm_model="stub-model",
        _env_file=None,
    )
    context = build_context(
        settings,
        provider=stub,
        http=http,
        sleep=lambda _: None,
        github=GitHubApp(http, AppCredentials(APP_ID, keys[0]), sleep=lambda _: None),
    )
    yield context
    context.engine.dispose()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


# --- webhook --------------------------------------------------------------------------


def test_webhook_rejects_a_bad_signature_and_enqueues_nothing() -> None:
    published: list[Job] = []
    client = TestClient(create_app(SECRET, published.append))
    body = json.dumps({"action": "created"}).encode()
    for header in (
        {},
        {"X-Hub-Signature-256": "sha256=00"},
        {"X-Hub-Signature-256": sign(body + b"x")},
    ):
        response = client.post(
            "/webhook", content=body, headers={"X-GitHub-Event": "installation", **header}
        )
        assert response.status_code == 401
    assert published == []


def test_webhook_enqueues_installation_events_only() -> None:
    published: list[Job] = []
    client = TestClient(create_app(SECRET, published.append))
    body = json.dumps({"action": "created", "installation": {"id": 1}}).encode()
    ok = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": sign(body),
            "X-GitHub-Delivery": "d-1",
        },
    )
    ignored = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(body)},
    )
    assert (ok.status_code, ignored.status_code) == (202, 202)
    (job,) = published
    assert job.type == JobType.EVENT and job.payload["event"] == "installation"
    assert job.payload["payload"]["installation"]["id"] == 1
    assert client.get("/healthz").json() == {"status": "ok"}
    assert not valid_signature(b"", body, sign(body))


# --- installation events ----------------------------------------------------------------


def _jobs(ctx: AppContext) -> list[Job]:
    queue = ctx.queue
    assert isinstance(queue, InProcessQueue)
    seen: list[Job] = []
    queue.register(JobType.RUN, seen.append)
    queue.drain()
    return seen


def test_installation_created_registers_repos_and_starts_catch_up(ctx: AppContext) -> None:
    payload = {
        "action": "created",
        "installation": {"id": 9},
        "repositories": [{"full_name": "acme/a"}, {"full_name": "acme/b"}],
    }
    change = apply_event(ctx, "installation", payload)
    assert change.activated == ["acme/a", "acme/b"]
    jobs = _jobs(ctx)
    assert change.runs_enqueued == len(jobs) == 6  # 2 repos x (2 SDKs + 1 API source)
    assert {j.payload["target"] for j in jobs} == {
        "openai@latest",
        "stripe@latest",
        "openai-deprecations@latest",
    }

    again = apply_event(ctx, "installation", payload)  # a redelivered event
    assert (again.activated, again.runs_enqueued) == ([], 0)


def test_uninstall_and_removal_deactivate_and_fan_out_skips_them(ctx: AppContext) -> None:
    apply_event(
        ctx,
        "installation",
        {
            "action": "created",
            "installation": {"id": 9},
            "repositories": [{"full_name": "acme/a"}, {"full_name": "acme/b"}],
        },
    )
    removed = apply_event(
        ctx,
        "installation_repositories",
        {
            "action": "removed",
            "installation": {"id": 9},
            "repositories_removed": [{"full_name": "acme/a"}],
        },
    )
    assert removed.deactivated == ["acme/a"]
    from guardian.apilevel.source import API_SOURCES, fan_out_api

    _jobs(ctx)
    assert fan_out_api(ctx, API_SOURCES[0], 1) == 1  # only acme/b
    deleted = apply_event(ctx, "installation", {"action": "deleted", "installation": {"id": 9}})
    assert deleted.deactivated == ["acme/b"]
    with session_scope(ctx.sessions) as session:
        assert not any(r.active for r in session.scalars(select(Repo)))


# --- checkout and config ------------------------------------------------------------------


def _tar(members: dict[str, bytes], *, link: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo("top/link")
            info.type, info.linkname = tarfile.SYMTYPE, link
            archive.addfile(info)
    return buffer.getvalue()


def test_tarball_extraction_is_safe(tmp_path: Path) -> None:
    extract_tarball(_tar({"top/app.py": b"x = 1\n"}, link="/etc/passwd"), tmp_path / "ok")
    assert (tmp_path / "ok" / "app.py").read_text() == "x = 1\n"
    assert not (tmp_path / "ok" / "link").exists()
    with pytest.raises(CheckoutError, match="unsafe"):
        extract_tarball(_tar({"top/../../evil.py": b"x"}), tmp_path / "bad")


def test_repo_config() -> None:
    assert parse_config(None) == RepoConfig()
    config = parse_config("mode: issue\npackages: [openai]\napi_changes: off\n")
    assert (config.mode, config.api_changes, config.wants("openai"), config.wants("stripe")) == (
        "issue",
        "off",
        True,
        False,
    )
    bad = parse_config("mode: yolo\nfoo: 1\n")
    assert bad.mode == "pr" and len(bad.warnings) == 2
    assert parse_config("{{{").warnings


# --- delivery ----------------------------------------------------------------------------

BEFORE_APP = 'import openai\nc = openai.Client("k")\nc.create("x", engine="e")\nc.request("a")\n'
AFTER_APP = BEFORE_APP.replace('engine="e"', 'model="e"')


def _item(key: str, status: str = FIXED, line: int = 3) -> ReportItem:
    return ReportItem(
        "app.py",
        line,
        'engine="e"',
        f"change `{key}`",
        key,
        "quote",
        "https://example/notes",
        "high",
        status,
        "codemod",
        "",
    )


def _report(
    *keys: str,
    new: str = "2.0.0",
    verified: bool = True,
    statuses: tuple[str, ...] = (),
    kind: str = SDK,
) -> Report:
    patch = PatchSet("openai", "1.0.0", new)
    patch.files = {
        "requirements.txt": ("openai==1.0.0\n", f"openai=={new}\n"),
        "app.py": (BEFORE_APP, AFTER_APP),
    }
    statuses = statuses or tuple(FIXED for _ in keys)
    patch.fixes = [
        FindingFix(i, s, "codemod" if s == FIXED else None) for i, s in enumerate(statuses)
    ]
    items = [_item(k, s, 3 + i) for i, (k, s) in enumerate(zip(keys, statuses, strict=True))]
    verification = Verification(level=L2 if verified else L0)
    subject = "openai-deprecations" if kind == API else "openai"
    return Report(
        kind,
        subject,
        "openai",
        "1.0.0",
        new,
        items,
        patch,
        verification,
        shutdown_date="2026-12-11" if kind == API else None,
    )


@pytest.fixture
def publisher(app_ctx: AppContext, fake: FakeGitHubApp) -> Publisher:
    fake.add_repo(REPO, {"requirements.txt": "openai==1.0.0\n", "app.py": BEFORE_APP})
    assert app_ctx.github is not None
    remote = fetch_checkout(app_ctx.github, INSTALLATION, REPO, app_ctx.settings.work_dir)
    return Publisher(app_ctx, app_ctx.github, remote)


def test_a_verified_patch_opens_one_draft_pr_with_one_commit(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    repo = fake.repos[REPO]
    main_before = repo.head()
    published = publisher.publish(_report("A", "B"), RepoConfig())
    assert published.action == PR_OPENED
    (pr,) = repo.pulls()
    assert (pr["draft"], pr["head"]["ref"], pr["base"]["ref"]) == (
        True,
        "guardian/upgrade-openai",
        "main",
    )
    assert pr["title"] == "Upgrade openai 1.0.0 → 2.0.0"
    assert "<!-- api-change-guardian:openai -->" in pr["body"]
    commit = repo.head("guardian/upgrade-openai")
    assert repo.commits[commit]["parents"] == [main_before]
    files = repo.files_at(commit)
    assert files["requirements.txt"] == "openai==2.0.0\n" and files["app.py"] == AFTER_APP
    assert repo.head() == main_before  # nothing on the default branch


def test_the_same_job_twice_gives_one_pr_and_one_commit(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    first = publisher.publish(_report("A"), RepoConfig())
    commits = len(fake.repos[REPO].commits)
    second = publisher.publish(_report("A"), RepoConfig())
    assert (first.action, second.action, first.number) == (PR_OPENED, PR_UPDATED, second.number)
    assert len(fake.repos[REPO].pulls()) == 1 and len(fake.repos[REPO].commits) == commits


def test_a_later_release_updates_the_same_pr(publisher: Publisher, fake: FakeGitHubApp) -> None:
    first = publisher.publish(_report("A"), RepoConfig())
    later = publisher.publish(_report("A", new="2.1.0"), RepoConfig())
    assert later.action == PR_UPDATED and later.number == first.number
    (pr,) = fake.repos[REPO].pulls()
    assert pr["title"] == "Upgrade openai 1.0.0 → 2.1.0"
    branch_head = fake.repos[REPO].head("guardian/upgrade-openai")
    assert fake.repos[REPO].files_at(branch_head)["requirements.txt"] == "openai==2.1.0\n"


def test_a_closed_pr_is_remembered_until_a_new_change_appears(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    first = publisher.publish(_report("A", "B"), RepoConfig())
    assert first.number is not None
    fake.close(REPO, first.number)
    writes = len(fake.writes)
    declined = publisher.publish(_report("A", "B", new="2.1.0"), RepoConfig())
    assert declined.action == DECLINED and len(fake.writes) == writes
    fresh = publisher.publish(_report("A", "B", "C", new="2.2.0"), RepoConfig())
    assert fresh.action == PR_OPENED and fresh.number != first.number


def test_a_merged_pr_is_not_a_decline(publisher: Publisher, fake: FakeGitHubApp) -> None:
    first = publisher.publish(_report("A"), RepoConfig())
    assert first.number is not None
    fake.close(REPO, first.number, merged=True)
    assert publisher.publish(_report("A", new="3.0.0"), RepoConfig()).action == PR_OPENED


def test_unverified_twice_gives_exactly_one_issue(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    first = publisher.publish(_report("A", verified=False), RepoConfig())
    second = publisher.publish(_report("A", verified=False), RepoConfig())
    assert (first.action, second.action) == (ISSUE_OPENED, ISSUE_UPDATED)
    (issue,) = fake.repos[REPO].issues()
    assert issue["title"] == "openai 1.0.0 → 2.0.0: code needs changes"
    assert "could not be verified" in issue["body"]
    assert fake.repos[REPO].pulls() == []


def test_a_closed_issue_is_remembered_like_a_closed_pr(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    first = publisher.publish(_report("A", verified=False), RepoConfig())
    assert first.number is not None
    fake.close(REPO, first.number)
    assert publisher.publish(_report("A", verified=False), RepoConfig()).action == DECLINED


def test_issue_only_mode_pushes_nothing_and_posts_the_diff(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    published = publisher.publish(_report("A"), RepoConfig(mode="issue"))
    assert published.action == ISSUE_OPENED
    assert set(fake.repos[REPO].refs) == {"main"}
    assert not any("/git/" in w for w in fake.writes)
    (issue,) = fake.repos[REPO].issues()
    assert "```diff" in issue["body"] and '+c.create("x", model="e")' in issue["body"]


def test_a_partial_patch_says_so(publisher: Publisher, fake: FakeGitHubApp) -> None:
    report = _report("A", "B", statuses=(FIXED, REPORT_ONLY))
    assert publisher.publish(report, RepoConfig()).action == PR_OPENED
    (pr,) = fake.repos[REPO].pulls()
    assert pr["title"].endswith("(partial)")
    assert "### Left for a person" in pr["body"] and "app.py:4" in pr["body"]


def test_the_pause_flag_stops_every_write(
    publisher: Publisher, fake: FakeGitHubApp, app_ctx: AppContext
) -> None:
    app_ctx.is_paused = lambda: True
    assert publisher.publish(_report("A"), RepoConfig()).action == PAUSED
    assert publisher.publish(_report("A", verified=False), RepoConfig()).action == PAUSED
    assert fake.writes == [] or all(w.startswith("POST /app/") for w in fake.writes)


def test_an_open_dependabot_pr_is_linked(publisher: Publisher, fake: FakeGitHubApp) -> None:
    repo = fake.repos[REPO]
    repo.items[99] = {
        "number": 99,
        "kind": "pr",
        "state": "open",
        "title": "Bump openai",
        "body": "",
        "merged_at": None,
        "head": {"ref": "dependabot/pip/openai-2.0.0"},
        "base": {"ref": "main"},
        "draft": False,
        "html_url": "",
    }
    publisher.publish(_report("A"), RepoConfig())
    ours = next(p for p in repo.pulls() if p["number"] != 99)
    assert "Related dependency PRs: #99" in ours["body"]


def test_api_level_pr_and_config_switch(publisher: Publisher, fake: FakeGitHubApp) -> None:
    report = _report("api:x", kind=API)
    report.patch.files.pop("requirements.txt")  # type: ignore[union-attr]
    off = publisher.publish(report, RepoConfig(api_changes="off"))
    assert off.action == SKIPPED and fake.repos[REPO].pulls() == []
    published = publisher.publish(report, RepoConfig())
    assert published.action == PR_OPENED
    (pr,) = fake.repos[REPO].pulls()
    assert pr["title"] == "[behaviour change] Replace OpenAI models retiring 2026-12-11"
    assert pr["head"]["ref"] == "guardian/api-openai-deprecations"
    assert "**Behaviour change.**" in pr["body"]
    commit = fake.repos[REPO].head("guardian/api-openai-deprecations")
    assert fake.repos[REPO].files_at(commit)["requirements.txt"] == "openai==1.0.0\n"


# --- the whole run for an installed repo ---------------------------------------------------


def _as_openai(files: dict[str, str]) -> dict[str, str]:
    return {k.replace("pkg", "openai"): v.replace("pkg", "openai") for k, v in files.items()}


def test_installed_repo_run_opens_a_verified_draft_pr(
    app_ctx: AppContext, fake: FakeGitHubApp, github: FakeGitHub, pypi: FakePyPI
) -> None:
    pypi.add_wheel("openai", "1.0.0", _as_openai(V1))
    pypi.add_wheel("openai", "2.0.0", _as_openai(V2))
    github.add_release(OPENAI, "v1.0.0", "## 1.0.0\n")
    github.add_release(OPENAI, "v2.0.0", "## 2.0.0\n\n* changes\n")
    fake.add_repo(REPO, {"requirements.txt": "openai==1.0.0\n", "app.py": BEFORE_APP})
    main_before = fake.repos[REPO].head()
    app_ctx.llm.provider = UnconfiguredProvider()

    request = RunRequest(
        repo=REPO, target="openai@latest", package="openai", installation_id=INSTALLATION
    )
    result = pipeline.run(request, app_ctx)

    assert (result.verification_level, result.delivery) == ("L2", PR_OPENED), result.reason
    (pr,) = fake.repos[REPO].pulls()
    assert pr["draft"] and pr["title"] == "Upgrade openai 1.0.0 → 2.0.0 (partial)"
    assert "**Verification: L2**" in pr["body"] and "tests were not run" in pr["body"]
    commit = fake.repos[REPO].head("guardian/upgrade-openai")
    files = fake.repos[REPO].files_at(commit)
    assert files["requirements.txt"] == "openai==2.0.0\n" and files["app.py"] == AFTER_APP
    assert fake.repos[REPO].head() == main_before

    again = pipeline.run(request, app_ctx)  # the same job delivered twice
    assert again.delivery == PR_UPDATED and len(fake.repos[REPO].pulls()) == 1
    assert fake.repos[REPO].head("guardian/upgrade-openai") == commit


def test_an_uninstalled_repo_is_not_run(app_ctx: AppContext, fake: FakeGitHubApp) -> None:
    with session_scope(app_ctx.sessions) as session:
        session.add(
            Repo(installation_id=INSTALLATION, full_name=REPO, default_branch="main", active=False)
        )
    request = RunRequest(
        repo=REPO, target="openai@latest", package="openai", installation_id=INSTALLATION
    )
    assert pipeline.run(request, app_ctx).outcome == "repo-inactive"
    assert fake.requests == []


def test_the_manifest_asks_for_exactly_the_spec_permissions() -> None:
    manifest = json.loads(
        (Path(__file__).parent.parent / "deploy" / "github-app-manifest.json").read_text()
    )
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    }
    assert "workflows" not in manifest["default_permissions"]
    assert manifest["public"] is False


def test_untrusted_text_cannot_mention_people_or_load_images(
    publisher: Publisher, fake: FakeGitHubApp
) -> None:
    report = _report("A")
    hostile = "Thanks @octo-org/maintainers ![x](https://t.example/p.png) <img src=x>"
    report.items = [
        ReportItem(
            "app.py",
            3,
            "c.create(engine='e')",
            "change `A`",
            "A",
            hostile,
            "https://example/notes",
            "high",
            FIXED,
            "codemod",
            "",
        )
    ]
    publisher.publish(report, RepoConfig())
    (pr,) = fake.repos[REPO].pulls()
    assert "@octo-org" not in pr["body"] and "@⁠octo-org" in pr["body"]
    assert "![x]" not in pr["body"] and "<img" not in pr["body"]


def test_an_executable_file_stays_executable(publisher: Publisher, fake: FakeGitHubApp) -> None:
    fake.repos[REPO].modes["app.py"] = "100755"
    publisher.publish(_report("A"), RepoConfig())
    assert fake.repos[REPO].sent_modes == {"app.py": "100755", "requirements.txt": "100644"}


def test_a_failed_delivery_is_recorded_not_raised(
    app_ctx: AppContext,
    fake: FakeGitHubApp,
    github: FakeGitHub,
    pypi: FakePyPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from guardian.github_app.client import GitHubError

    pypi.add_wheel("openai", "1.0.0", _as_openai(V1))
    pypi.add_wheel("openai", "2.0.0", _as_openai(V2))
    github.add_release(OPENAI, "v1.0.0", "## 1.0.0\n")
    github.add_release(OPENAI, "v2.0.0", "## 2.0.0\n\n* changes\n")
    fake.add_repo(REPO, {"requirements.txt": "openai==1.0.0\n", "app.py": BEFORE_APP})
    app_ctx.llm.provider = UnconfiguredProvider()

    def refuse(self: Publisher, report: Report, config: RepoConfig) -> None:
        raise GitHubError(422, "POST", "/pulls", "A pull request already exists")

    monkeypatch.setattr(Publisher, "publish", refuse)
    request = RunRequest(
        repo=REPO, target="openai@latest", package="openai", installation_id=INSTALLATION
    )
    result = pipeline.run(request, app_ctx)
    assert result.delivery == "delivery-failed"
    assert "A pull request already exists" in (result.reason or "")


def test_only_the_current_commit_is_kept_on_disk(app_ctx: AppContext, fake: FakeGitHubApp) -> None:
    assert app_ctx.github is not None
    fake.add_repo(REPO, {"app.py": "x = 1\n"})
    first = fetch_checkout(app_ctx.github, INSTALLATION, REPO, app_ctx.settings.work_dir)
    fake.push(REPO, {"app.py": "x = 2\n"})
    second = fetch_checkout(app_ctx.github, INSTALLATION, REPO, app_ctx.settings.work_dir)
    assert second.head_sha != first.head_sha
    assert not first.path.exists()
    assert [p.name for p in second.path.parent.iterdir()] == [second.head_sha]
    assert (second.path / "app.py").read_text() == "x = 2\n"
