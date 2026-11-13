"""Pipeline entry points, pause gate and CLI. Covers F0.5, F0.10, F0.12."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import inspect
from typer.testing import CliRunner

from guardian import pipeline
from guardian.cli import app
from guardian.common import db
from guardian.common.config import Settings
from guardian.common.context import AppContext, build_context
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.github_app.delivery import Delivery, deliver
from guardian.pipeline.types import NOT_IMPLEMENTED, RunRequest

runner = CliRunner()


# --- entry points -----------------------------------------------------------


def test_entry_points_are_plain_functions(ctx: AppContext) -> None:
    assert pipeline.poll(ctx).outcome == NOT_IMPLEMENTED
    request = RunRequest(repo="octo/app", target="openai@1.45.0", package="openai")
    assert pipeline.run(request, ctx).outcome == NOT_IMPLEMENTED
    assert pipeline.handle_event({"action": "created"}, ctx).outcome == NOT_IMPLEMENTED


def test_queue_invokes_all_three_job_types(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(pipeline, "poll", lambda c: called.append("poll"))
    monkeypatch.setattr(pipeline, "run", lambda request, c: called.append(request.target))
    monkeypatch.setattr(
        pipeline, "handle_event", lambda payload, c: called.append(payload["action"])
    )

    queue = InProcessQueue()
    pipeline.register_handlers(queue, ctx)
    queue.publish(Job(JobType.POLL))
    queue.publish(Job(JobType.RUN, {"repo": "octo/app", "target": "openai@1.45.0"}))
    queue.publish(Job(JobType.EVENT, {"action": "created"}))

    assert queue.drain() == 3
    assert called == ["poll", "openai@1.45.0", "created"]


def test_context_refuses_an_unknown_llm_provider(migrated_url: str) -> None:
    settings = Settings(
        database_url=migrated_url,
        llm_provider="vertex",
        _env_file=None,
    )
    with pytest.raises(NotImplementedError, match="Phase 1"):
        build_context(settings)


# --- pause gate -------------------------------------------------------------


def test_delivery_runs_the_write_when_not_paused() -> None:
    writes: list[str] = []
    result = deliver(lambda: False, lambda: writes.append("pr"), what="open PR")
    assert result is Delivery.DELIVERED
    assert writes == ["pr"]


def test_paused_delivery_writes_nothing_and_reports_paused() -> None:
    writes: list[str] = []
    result = deliver(lambda: True, lambda: writes.append("pr"), what="open PR")
    assert result is Delivery.PAUSED
    assert result.value == "paused"
    assert writes == []


def test_pause_flag_comes_from_settings(migrated_url: str, tmp_path: Path) -> None:
    settings = Settings(
        database_url=migrated_url,
        blob_dir=tmp_path,
        paused=True,
        _env_file=None,
    )
    context = build_context(settings)
    try:
        assert deliver(context.is_paused, lambda: None, what="open PR") is Delivery.PAUSED
    finally:
        context.engine.dispose()


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def cli_env(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("GUARDIAN_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setenv("GUARDIAN_LLM_PROVIDER", "none")
    monkeypatch.chdir(tmp_path)  # no stray .env is picked up
    return database_url


def _last_json(output: str) -> dict[str, object]:
    result: dict[str, object] = json.loads(output.strip().splitlines()[-1])
    return result


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Help text without colour codes.

    Rich colours help output when it thinks it is in CI (for example GITHUB_ACTIONS is
    set), and it styles `-` and `-repo` separately, so `--repo` never appears in one
    piece. Stripping the codes makes these checks independent of the environment.
    """
    return _ANSI_ESCAPE.sub("", output)


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("poll", "run", "ingest", "migrate"):
        assert command in _plain(result.output)


def test_run_help_lists_its_options() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    for option in ("--repo", "--package", "--from", "--to"):
        assert option in _plain(result.output)


def test_plain_strips_colour_codes() -> None:
    coloured = "\x1b[1;36m-\x1b[0m\x1b[1;36m-repo\x1b[0m  \x1b[33mPATH\x1b[0m"
    assert _plain(coloured) == "--repo  PATH"


def test_migrate_then_poll_and_run(cli_env: str, tmp_path: Path) -> None:
    migrated = runner.invoke(app, ["migrate"])
    assert migrated.exit_code == 0, migrated.output
    engine = db.make_engine(cli_env)
    try:
        assert "runs" in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    polled = runner.invoke(app, ["poll"])
    assert polled.exit_code == 0, polled.output
    assert _last_json(polled.output)["outcome"] == NOT_IMPLEMENTED

    ran = runner.invoke(
        app, ["run", "--repo", str(tmp_path), "--package", "openai", "--to", "1.45.0"]
    )
    assert ran.exit_code == 0, ran.output
    assert _last_json(ran.output)["outcome"] == NOT_IMPLEMENTED


def test_ingest_requires_an_existing_file(cli_env: str, tmp_path: Path) -> None:
    missing = runner.invoke(
        app, ["ingest", "--source", "openai-deprecations", "--file", str(tmp_path / "nope.html")]
    )
    assert missing.exit_code != 0

    page = tmp_path / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    ok = runner.invoke(app, ["ingest", "--source", "openai-deprecations", "--file", str(page)])
    assert ok.exit_code == 0, ok.output
    assert _last_json(ok.output)["source"] == "openai-deprecations"
