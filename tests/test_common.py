"""Config, logging, blob store and job queue. Covers F0.2, F0.5, F0.7, F0.8."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from guardian.common.blobstore import LocalBlobStore
from guardian.common.config import Settings
from guardian.common.jobs import InProcessQueue, Job, JobType
from guardian.common.log import configure_logging, current_run_id, get_logger, run_context


def _settings() -> Settings:
    return Settings(_env_file=None)


# --- config -----------------------------------------------------------------


def test_defaults_need_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "GUARDIAN_DATABASE_URL", "GUARDIAN_PAUSED"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings()
    assert settings.database_url.startswith("sqlite:///")
    assert settings.paused is False
    assert settings.llm_provider == "none"


def test_database_url_is_read_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/guardian")
    assert _settings().database_url == "postgresql+psycopg://u:p@localhost/guardian"


def test_prefixed_variables_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDIAN_PAUSED", "true")
    monkeypatch.setenv("GUARDIAN_LOG_LEVEL", "DEBUG")
    settings = _settings()
    assert settings.paused is True
    assert settings.log_level == "DEBUG"


def test_secrets_do_not_leak_into_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDIAN_GITHUB_WEBHOOK_SECRET", "hunter2-not-a-real-secret")
    settings = _settings()
    assert "hunter2" not in repr(settings)
    assert settings.github_webhook_secret is not None
    assert settings.github_webhook_secret.get_secret_value() == "hunter2-not-a-real-secret"


def test_env_example_lists_every_setting() -> None:
    example = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    for field in Settings.model_fields:
        expected = "DATABASE_URL" if field == "database_url" else f"GUARDIAN_{field.upper()}"
        assert f"{expected}=" in example, expected


# --- logging ----------------------------------------------------------------


def test_log_lines_are_json_with_run_id() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    log = get_logger("guardian.test")

    with run_context("run-123") as run_id:
        log.info("scan finished", extra={"fields": {"findings": 2}})
        assert current_run_id() == run_id == "run-123"
    log.info("outside")

    inside, outside = (json.loads(line) for line in stream.getvalue().splitlines())
    assert inside["message"] == "scan finished"
    assert inside["run_id"] == "run-123"
    assert inside["findings"] == 2
    assert inside["level"] == "INFO"
    assert outside["run_id"] is None


def test_run_context_generates_an_id() -> None:
    with run_context() as run_id:
        assert run_id and current_run_id() == run_id
    assert current_run_id() is None


# --- blob store -------------------------------------------------------------


def test_blob_round_trip_is_byte_identical(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    data = "## 1.45.0\r\n\r\n* renamed `engine` → `model`\n".encode()

    ref = store.put("openai/1.45.0/r1.md", data)

    assert store.exists(ref)
    assert store.get(ref) == data
    assert not store.exists("openai/1.45.0/r2.md")


@pytest.mark.parametrize(
    "key", ["", "../outside", "a/../../outside", "/etc/passwd", "C:/x", "a\\b", "a//b", "./a"]
)
def test_blob_keys_cannot_leave_the_root(tmp_path: Path, key: str) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.put(key, b"x")


# --- job queue --------------------------------------------------------------


def test_queue_runs_jobs_in_order_and_handlers_can_fan_out() -> None:
    queue = InProcessQueue()
    seen: list[str] = []

    def on_poll(job: Job) -> None:
        seen.append("poll")
        for repo in ("a/one", "b/two"):
            queue.publish(Job(JobType.RUN, {"repo": repo}))

    def on_run(job: Job) -> None:
        seen.append(job.payload["repo"])

    queue.register(JobType.POLL, on_poll)
    queue.register(JobType.RUN, on_run)
    queue.publish(Job(JobType.POLL))

    assert queue.drain() == 3
    assert seen == ["poll", "a/one", "b/two"]
    assert len(queue) == 0


def test_queue_respects_max_jobs() -> None:
    queue = InProcessQueue()
    queue.register(JobType.EVENT, lambda job: None)
    for _ in range(3):
        queue.publish(Job(JobType.EVENT))
    assert queue.drain(max_jobs=2) == 2
    assert len(queue) == 1


def test_queue_rejects_unhandled_job_types() -> None:
    queue = InProcessQueue()
    queue.publish(Job(JobType.RUN))
    with pytest.raises(LookupError):
        queue.drain()


def test_jobs_get_unique_ids() -> None:
    assert Job(JobType.POLL).id != Job(JobType.POLL).id
