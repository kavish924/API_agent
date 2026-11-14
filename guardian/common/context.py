"""Application context: the explicit set of dependencies every pipeline stage receives.

Stages take a context instead of reaching for globals, so the same code runs from the
CLI, from tests and (later) from the Pub/Sub worker with different implementations.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from sqlalchemy import Engine

from guardian.common.blobstore import BlobStore, LocalBlobStore
from guardian.common.config import Settings, get_settings
from guardian.common.db import SessionFactory, make_engine, make_session_factory
from guardian.common.jobs import InProcessQueue, JobQueue
from guardian.common.llm import LLMClient, LLMProvider, UnconfiguredProvider


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    sessions: SessionFactory
    blobs: BlobStore
    queue: JobQueue
    llm: LLMClient
    http: httpx.Client
    # A callable, not a bool, so a deployed worker can read a runtime switch (spec 09).
    is_paused: Callable[[], bool]
    # Used for retry backoff; tests replace it so they never really wait.
    sleep: Callable[[float], None] = time.sleep


def _provider_from_settings(settings: Settings) -> LLMProvider:
    if settings.llm_provider in ("", "none"):
        return UnconfiguredProvider()
    raise NotImplementedError(
        f"LLM provider {settings.llm_provider!r} is not implemented yet. "
        "The real provider is added in Phase 1, once the model is chosen (spec 02)."
    )


def build_context(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    queue: JobQueue | None = None,
    blobs: BlobStore | None = None,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AppContext:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    return AppContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        blobs=blobs or LocalBlobStore(settings.blob_dir),
        queue=queue or InProcessQueue(),
        llm=LLMClient(
            provider=provider or _provider_from_settings(settings),
            sessions=sessions,
            default_model=settings.llm_model or "unset",
        ),
        http=http or httpx.Client(timeout=30.0, follow_redirects=True),
        is_paused=lambda: settings.paused,
        sleep=sleep,
    )
