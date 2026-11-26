"""API-level changes (spec 10): page parsing, families, snapshots, scan, fix, pipeline.

The real-snapshot fixture is OpenAI's deprecations page fetched on 2026-11-25
(decision recorded in spec 10). Scans run against a small fake `openai` SDK with a
chat, an images and a speech method, laid out like the real one.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from guardian import pipeline
from guardian.apidiff.surface import Surface, build_surface
from guardian.apilevel.families import UNKNOWN, default_table, parse_table
from guardian.apilevel.page import (
    CHOICE,
    ENDPOINT_SUNSET,
    MODEL_RETIRED,
    NONE,
    SINGLE,
    WHY,
    WITH_SETTINGS,
    ApiRecord,
    changed_records,
    classify_replacement,
    parse_page,
    parse_rows,
)
from guardian.apilevel.run import config_usages, run_api
from guardian.apilevel.source import API_SOURCES, ingest, poll_api_source, snapshot_text
from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.jobs import InProcessQueue, JobType
from guardian.common.models import ChangeRecord, Finding, Repo
from guardian.fix import FIXED, REPORT_ONLY
from guardian.pipeline.types import NO_FINDINGS, RunRequest
from tests.apifixtures import write_tree
from tests.fakes import FakeGitHub, FakePages, FakePyPI

PAGE = Path(__file__).parent / "fixtures" / "pages" / "openai-deprecations-2026-11-25.md"
TODAY = date(2026, 11, 25)
SOURCE = API_SOURCES[0]


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def records(page: str) -> list[ApiRecord]:
    return parse_page(page)


def by_id(records: list[ApiRecord], identifier: str) -> ApiRecord:
    (record,) = [r for r in records if r.identifier == identifier]
    return record


# --- the real snapshot -------------------------------------------------------------------


def test_real_snapshot_rows_and_ids(page: str, records: list[ApiRecord]) -> None:
    rows = parse_rows(page)
    assert sum(r.section == "Upcoming deprecations" for r in rows) == 34
    models = [r for r in records if r.kind == MODEL_RETIRED]
    assert len(models) == 52
    assert len([r for r in records if r.kind == ENDPOINT_SUNSET]) == 6
    assert all(r.shutdown_date is not None for r in records)


@pytest.mark.parametrize(
    ("identifier", "kind", "replacement", "fixable", "why"),
    [
        ("gpt-5", SINGLE, "gpt-6.1-sol", True, ""),
        ("tts-1", SINGLE, "gpt-realtime-2.1-mini", False, WHY["family"]),
        ("gpt-image-1.5", CHOICE, None, False, WHY[CHOICE]),
        ("o3-pro", WITH_SETTINGS, None, False, WHY[WITH_SETTINGS]),
        ("gpt-5.5-cyber-preview", NONE, None, False, WHY[NONE]),
        ("gpt-5.3-codex", SINGLE, "gpt-6-sol", False, WHY["family"]),  # Responses-only
        ("gpt-realtime", SINGLE, "gpt-realtime-2.1", True, ""),
    ],
)
def test_worked_cases(
    records: list[ApiRecord],
    identifier: str,
    kind: str,
    replacement: str | None,
    fixable: bool,
    why: str,
) -> None:
    record = by_id(records, identifier)
    assert (record.replacement_kind, record.replacement, record.fixable, record.reason) == (
        kind,
        replacement,
        fixable,
        why,
    )
    assert record.evidence_quote.startswith("|") and identifier in record.evidence_quote


def test_gpt_5_shutdown_and_families(records: list[ApiRecord]) -> None:
    record = by_id(records, "gpt-5")
    assert record.shutdown_date == date(2026, 12, 11)
    assert record.families == {"chat", "responses"}
    assert record.decided_by == ("gpt-*", "gpt-*")


@pytest.mark.parametrize(
    ("cell", "kind", "models"),
    [
        ("`gpt-6.1-sol`", SINGLE, ("gpt-6.1-sol",)),
        ("`a-1` or `b-2`", CHOICE, ("a-1", "b-2")),
        (
            "`gpt-6.1-sol` (`reasoning.mode: pro`) in the Responses API",
            WITH_SETTINGS,
            ("gpt-6.1-sol",),
        ),
        ("The most capable cyber model available to you.", NONE, ()),
        ("Use `gpt-6` for text, or [see the guide](https://x)", WITH_SETTINGS, ("gpt-6",)),
    ],
)
def test_classify_replacement(cell: str, kind: str, models: tuple[str, ...]) -> None:
    result = classify_replacement(cell)
    assert (result.kind, result.models) == (kind, models)


def test_family_table() -> None:
    table = default_table()
    assert table.of_model("gpt-4o-mini-tts")[0] == {"audio.speech"}
    assert table.of_model("gpt-4o-mini-realtime")[0] == {"realtime"}
    assert table.of_model("chatgpt-image-latest")[0] == {"images"}
    assert table.of_model("gpt-4o-audio")[0] == {"chat"}
    assert table.of_model("claude-something") == (frozenset({UNKNOWN}), None)
    assert table.of_method("openai.resources.chat.completions.Completions.create") == "chat"
    assert table.of_method("openai.resources.beta.realtime.x.connect") == "realtime"


def test_an_unknown_family_is_report_only() -> None:
    table = parse_table('[[models]]\nglob = "gpt-*"\nfamilies = ["chat"]\n')
    page = (
        "## Upcoming deprecations\n"
        "| Shutdown date | Affected model | Status | Recommended replacement | Notice date |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| **Jan 1, 2027** | `zeta-1` | Deprecated | `zeta-2` | Jul 1, 2026 |\n"
    )
    from guardian.apilevel.page import records as build

    (record,) = build(parse_rows(page), table=table)
    assert (record.fixable, record.reason) == (False, WHY[UNKNOWN])


# --- snapshots and the gate ---------------------------------------------------------------


def _add_row(page: str, row: str) -> str:
    anchor = "Fine-tuned versions of the models listed above"
    head, tail = page.split(anchor, 1)
    return head.rstrip("\n") + "\n" + row + "\n\n\n" + anchor + tail


NEW_ROW = "| **Mar 3, 2027** | `gpt-zz-9` | Deprecated | `gpt-6-luna` | Sep 3, 2026 |"


def test_a_second_snapshot_with_one_more_entry_gives_one_record(
    page: str, records: list[ApiRecord]
) -> None:
    later = parse_page(_add_row(page, NEW_ROW))
    (new,) = changed_records(records, later, today=TODAY)
    assert (new.identifier, new.replacement, new.fixable) == ("gpt-zz-9", "gpt-6-luna", True)
    assert changed_records(records, records, today=TODAY) == []


def test_first_ingestion_counts_only_future_shutdowns(records: list[ApiRecord]) -> None:
    assert len(changed_records(None, records, today=TODAY)) == len(records)
    late = changed_records(None, records, today=date(2027, 1, 15))
    assert {r.identifier for r in late} >= {"gpt-realtime", "whisper-1"}
    assert "gpt-5" not in {r.identifier for r in late}


def _active_repos(ctx: AppContext, *names: str) -> None:
    with session_scope(ctx.sessions) as session:
        for name in names:
            session.add(Repo(installation_id=7, full_name=name, default_branch="main"))


def test_poll_stores_a_snapshot_and_fans_out_then_304(
    ctx: AppContext, pages: FakePages, page: str
) -> None:
    _active_repos(ctx, "acme/a", "acme/b")
    pages.set(SOURCE.url, page.encode("utf-8"))
    first = poll_api_source(ctx, SOURCE, today=TODAY)
    assert first.snapshot is not None and (first.snapshot.revision, first.snapshot.created) == (
        1,
        True,
    )
    assert len(first.new_records) == 58 and first.runs_enqueued == 2
    seen: list[tuple[JobType, object]] = []
    queue = ctx.queue
    assert isinstance(queue, InProcessQueue)
    queue.register(JobType.RUN, lambda job: seen.append((job.type, job.payload["target"])))
    queue.drain()
    assert seen == [
        (JobType.RUN, "openai-deprecations@r1"),
        (JobType.RUN, "openai-deprecations@r1"),
    ]
    assert snapshot_text(ctx, SOURCE) == page  # byte-for-byte

    second = poll_api_source(ctx, SOURCE, today=TODAY)
    assert second.not_modified and second.snapshot is None and second.runs_enqueued == 0
    assert pages.requests[-1].headers["If-None-Match"] == pages.pages[SOURCE.url][1]


def test_unchanged_content_with_a_new_etag_stores_nothing(
    ctx: AppContext, pages: FakePages, page: str
) -> None:
    _active_repos(ctx, "acme/a")
    pages.set(SOURCE.url, page.encode("utf-8"))
    poll_api_source(ctx, SOURCE, today=TODAY)
    pages.pages[SOURCE.url] = (page.encode("utf-8"), '"another-etag"')
    again = poll_api_source(ctx, SOURCE, today=TODAY)
    assert again.snapshot is not None and not again.snapshot.created
    assert again.runs_enqueued == 0


def test_a_new_row_starts_runs_on_the_next_revision(
    ctx: AppContext, pages: FakePages, page: str
) -> None:
    _active_repos(ctx, "acme/a")
    pages.set(SOURCE.url, page.encode("utf-8"))
    poll_api_source(ctx, SOURCE, today=TODAY)
    pages.set(SOURCE.url, _add_row(page, NEW_ROW).encode("utf-8"))
    later = poll_api_source(ctx, SOURCE, today=TODAY)
    assert later.snapshot is not None and later.snapshot.revision == 2
    assert [r.identifier for r in later.new_records] == ["gpt-zz-9"]
    assert later.runs_enqueued == 1


def test_ingest_gives_the_same_records_as_a_fetch(
    ctx: AppContext, pages: FakePages, page: str
) -> None:
    stored = ingest(ctx, SOURCE, page.encode("utf-8"), today=TODAY)
    assert stored.snapshot is not None and stored.snapshot.created
    assert stored.new_records == changed_records(None, parse_page(page), today=TODAY)


# --- scanning and fixing --------------------------------------------------------------------

FAKE_OPENAI: dict[str, str] = {
    "openai/__init__.py": "from openai._client import OpenAI as OpenAI\n",
    "openai/_compat.py": "from functools import cached_property as cached_property\n",
    "openai/_client.py": """
from openai._compat import cached_property
from openai.resources.audio import Audio
from openai.resources.chat import Chat
from openai.resources.images import Images


class OpenAI:
    def __init__(self, *, api_key: str | None = None) -> None: ...

    @cached_property
    def chat(self) -> Chat: ...

    @cached_property
    def images(self) -> Images: ...

    @cached_property
    def audio(self) -> Audio: ...
""",
    "openai/resources/__init__.py": "",
    "openai/resources/chat/__init__.py": """
from openai._compat import cached_property
from openai.resources.chat.completions import Completions


class Chat:
    @cached_property
    def completions(self) -> Completions: ...
""",
    "openai/resources/chat/completions.py": """
class Completions:
    def create(self, *, model: str, messages: list) -> dict: ...
""",
    "openai/resources/images.py": """
class Images:
    def generate(self, *, model: str, prompt: str) -> dict: ...
""",
    "openai/resources/audio/__init__.py": """
from openai._compat import cached_property
from openai.resources.audio.speech import Speech


class Audio:
    @cached_property
    def speech(self) -> Speech: ...
""",
    "openai/resources/audio/speech.py": """
class Speech:
    def create(self, *, model: str, input: str, voice: str) -> bytes: ...
""",
}


@pytest.fixture(scope="module")
def openai_surface(tmp_path_factory: pytest.TempPathFactory) -> Surface:
    root = tmp_path_factory.mktemp("openai")
    return build_surface(write_tree(root, FAKE_OPENAI), "openai", "1.0.0")


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


CHAT = (
    "import os\n"
    "from openai import OpenAI\n"
    "\n"
    "client = OpenAI()\n"
    'MODEL = "gpt-5"  # the default model\n'
    "\n"
    "\n"
    "def ask(q):\n"
    '    first = client.chat.completions.create(model="gpt-5", messages=q)\n'
    "    second = client.chat.completions.create(model=MODEL, messages=q)\n"
    '    third = client.chat.completions.create(model=os.environ["MODEL"], messages=q)\n'
    '    print("gpt-5")  # gpt-5 in a comment\n'
    '    client.images.generate(model="gpt-5", prompt="x")\n'
    '    client.audio.speech.create(model="tts-1", input="hi", voice="a")\n'
    '    client.chat.completions.create(model="gpt-5.5-cyber-preview", messages=q)\n'
    "    return first, second, third\n"
)


def test_findings_fixes_and_possible_usages(
    tmp_path: Path, records: list[ApiRecord], openai_surface: Surface
) -> None:
    root = repo(
        tmp_path,
        {
            "requirements.txt": "openai==1.0.0\n",
            "app.py": CHAT,
            ".env.example": "MODEL=gpt-5\nOTHER=gpt-5-mini\n",
            "config/models.yaml": "fallback: gpt-5.1\n",
        },
    )
    upcoming = [r for r in records if r.kind == MODEL_RETIRED]
    result = run_api(root, SOURCE, upcoming, openai_surface)

    found = sorted((f.line, by_query(upcoming, f.query)) for f in result.findings)
    assert found == [(5, "gpt-5"), (9, "gpt-5"), (14, "tts-1"), (15, "gpt-5.5-cyber-preview")]
    assert result.patch is not None
    statuses = {
        f.line: fix.status for f, fix in zip(result.findings, result.patch.fixes, strict=True)
    }
    assert statuses == {5: FIXED, 9: FIXED, 14: REPORT_ONLY, 15: REPORT_ONLY}
    reasons = {fix.reason for fix in result.patch.fixes if fix.status == REPORT_ONLY}
    assert reasons == {WHY["family"], WHY[NONE]}

    before, after = result.patch.files["app.py"]
    assert set(result.patch.files) == {"app.py"}  # no dependency file: no bump
    assert after == before.replace('MODEL = "gpt-5"', 'MODEL = "gpt-6.1-sol"').replace(
        'create(model="gpt-5", messages=q)', 'create(model="gpt-6.1-sol", messages=q)'
    )
    assert "gpt-6.1-sol" not in after.split("print(")[1].split("\n")[0]  # the comment line

    possible = {str(p) for p in result.possible}
    assert "app.py:13 `gpt-5` (passed to a images method)" in possible
    assert ".env.example:1 `gpt-5` (config file)" in possible
    assert ".env.example:2 `gpt-5-mini` (config file)" in possible
    assert "config/models.yaml:1 `gpt-5.1` (config file)" in possible
    assert not any(p.startswith(".env.example:2 `gpt-5`") for p in possible)
    assert result.earliest_shutdown == date(2026, 12, 11)


def by_query(records: list[ApiRecord], query: int | None) -> str:
    assert query is not None
    return records[query].identifier


def test_config_usages_skip_real_env_files(tmp_path: Path, records: list[ApiRecord]) -> None:
    root = repo(tmp_path, {".env": "MODEL=gpt-5\n", "settings.json": '{"m": "gpt-5"}'})
    usages = config_usages(root, [by_id(records, "gpt-5")])
    assert [str(u) for u in usages] == ["settings.json:1 `gpt-5` (config file)"]


def test_pipeline_api_run_end_to_end(
    ctx: AppContext, pypi: FakePyPI, page: str, tmp_path: Path
) -> None:
    pypi.add_wheel("openai", "1.0.0", FAKE_OPENAI)
    ingest(ctx, SOURCE, page.encode("utf-8"), today=TODAY)
    root = repo(tmp_path, {"requirements.txt": "openai==1.0.0\n", "app.py": CHAT})
    result = pipeline.run(RunRequest(repo=str(root), target="openai-deprecations@r1"), ctx)

    assert (result.api_source, result.behaviour_change, result.shutdown_date) == (
        "openai-deprecations",
        True,
        "2026-12-11",
    )
    assert (result.findings, result.fixed, result.report_only) == (4, 2, 2)
    assert result.patch_files == ["app.py"]
    assert result.patch_ref is not None
    diff = ctx.blobs.get(result.patch_ref).decode("utf-8")
    assert '+MODEL = "gpt-6.1-sol"  # the default model' in diff
    assert "requirements.txt" not in diff
    assert any("passed to a images method" in p for p in result.possible_usages)
    with session_scope(ctx.sessions) as session:
        rows = list(session.scalars(select(Finding).order_by(Finding.line)))
        assert [(r.line, r.fix_status) for r in rows] == [
            (5, FIXED),
            (9, FIXED),
            (14, REPORT_ONLY),
            (15, REPORT_ONLY),
        ]
        stored = session.scalars(
            select(ChangeRecord).where(ChangeRecord.identifier == "gpt-5")
        ).one()
        assert (stored.layer, stored.replacement, stored.shutdown_date) == (
            "api",
            "gpt-6.1-sol",
            date(2026, 12, 11),
        )


def test_pipeline_api_run_with_nothing_affected(
    ctx: AppContext, pypi: FakePyPI, page: str, tmp_path: Path
) -> None:
    pypi.add_wheel("openai", "1.0.0", FAKE_OPENAI)
    ingest(ctx, SOURCE, page.encode("utf-8"), today=TODAY)
    root = repo(
        tmp_path,
        {"requirements.txt": "openai==1.0.0\n", "app.py": "from openai import OpenAI\nOpenAI()\n"},
    )
    result = pipeline.run(RunRequest(repo=str(root), target="openai-deprecations@r1"), ctx)
    assert (result.outcome, result.findings, result.patch_ref) == (NO_FINDINGS, 0, None)


def test_a_failed_gate_is_retried_by_the_next_poll(
    ctx: AppContext, pages: FakePages, page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import guardian.apilevel.source as source_module

    _active_repos(ctx, "acme/a")
    pages.set(SOURCE.url, page.encode("utf-8"))
    real = source_module.fan_out_api

    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database went away")

    monkeypatch.setattr(source_module, "fan_out_api", broken)
    with pytest.raises(RuntimeError):
        poll_api_source(ctx, SOURCE, today=TODAY)
    monkeypatch.setattr(source_module, "fan_out_api", real)

    retried = poll_api_source(ctx, SOURCE, today=TODAY)  # the page itself is unchanged
    assert retried.runs_enqueued == 1 and len(retried.new_records) == 58
    assert poll_api_source(ctx, SOURCE, today=TODAY).runs_enqueued == 0  # gated once only


def test_poll_outcome_counts_sdk_sources_and_api_pages_together(
    ctx: AppContext, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import guardian.pipeline as pipeline_module
    from guardian.apilevel.source import ApiPoll
    from guardian.pipeline.types import POLL_FAILED, POLL_PARTIAL
    from tests.fakes import OPENAI, STRIPE

    github.fail_with[OPENAI] = 500
    github.fail_with[STRIPE] = 500
    # Every SDK source fails and the page answers 304: one source worked.
    assert pipeline.poll(ctx).outcome == POLL_PARTIAL

    def page_down(ctx: AppContext, source: object) -> ApiPoll:
        return ApiPoll(SOURCE, error="GET page: HTTP 503")

    monkeypatch.setattr(pipeline_module, "poll_api_source", page_down)
    # Everything fails: the poll says so.
    assert pipeline.poll(ctx).outcome == POLL_FAILED
