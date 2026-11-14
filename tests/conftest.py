"""Shared fixtures.

By default every test gets a fresh SQLite database. Set TEST_DATABASE_URL to run the
same suite against Postgres (CI does this), which is how spec 00 F0.4 is checked.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from guardian.common import db
from guardian.common.config import Settings
from guardian.common.context import AppContext, build_context
from guardian.common.llm import StubProvider
from tests.fakes import FakeGitHub, FakePyPI, combined_client


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo `configure_logging` after each test.

    The CLI installs a handler on whatever stdout is at the time. Under the CLI test
    runner that is a temporary stream, and leaving the handler in place would make
    later tests log to a closed file.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def database_url(tmp_path: Path) -> Iterator[str]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        db.downgrade(external)  # start clean on a shared server
        yield external
        db.downgrade(external)
    else:
        yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_url(database_url: str) -> str:
    db.upgrade(database_url)
    return database_url


@pytest.fixture
def engine(migrated_url: str) -> Iterator[Engine]:
    engine = db.make_engine(migrated_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine: Engine) -> db.SessionFactory:
    return db.make_session_factory(engine)


@pytest.fixture
def stub() -> StubProvider:
    return StubProvider()


@pytest.fixture
def github() -> FakeGitHub:
    """A fake GitHub API. Nothing in the test suite reaches the network."""
    return FakeGitHub()


@pytest.fixture
def pypi() -> FakePyPI:
    """A fake PyPI serving wheels the tests build in memory."""
    return FakePyPI()


@pytest.fixture
def slept() -> list[float]:
    """Backoff delays the code asked for. Tests never really wait."""
    return []


@pytest.fixture
def ctx(
    migrated_url: str,
    tmp_path: Path,
    stub: StubProvider,
    github: FakeGitHub,
    pypi: FakePyPI,
    slept: list[float],
) -> Iterator[AppContext]:
    settings = Settings(
        database_url=migrated_url,
        blob_dir=tmp_path / "blobs",
        llm_model="stub-model",
        _env_file=None,
    )
    context = build_context(
        settings, provider=stub, http=combined_client(github, pypi), sleep=slept.append
    )
    yield context
    context.engine.dispose()
