"""Shared fixtures.

By default every test gets a fresh SQLite database. Set TEST_DATABASE_URL to run the
same suite against Postgres (CI does this), which is how spec 00 F0.4 is checked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from guardian.common import db
from guardian.common.config import Settings
from guardian.common.context import AppContext, build_context
from guardian.common.llm import StubProvider


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
def ctx(migrated_url: str, tmp_path: Path, stub: StubProvider) -> Iterator[AppContext]:
    settings = Settings(
        database_url=migrated_url,
        blob_dir=tmp_path / "blobs",
        llm_model="stub-model",
        _env_file=None,
    )
    context = build_context(settings, provider=stub)
    yield context
    context.engine.dispose()
