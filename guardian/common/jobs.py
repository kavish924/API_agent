"""Job queue interface (spec 00 F0.5).

Three job types travel on the queue. The in-process implementation here runs them in
the same process for local use and tests; the Pub/Sub implementation is spec 09.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from guardian.common.log import get_logger

log = get_logger(__name__)


class JobType(StrEnum):
    POLL = "poll"  # check every change source
    RUN = "run"  # one pipeline run for one repo and target
    EVENT = "event"  # a GitHub installation event


@dataclass(frozen=True)
class Job:
    type: JobType
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


Handler = Callable[[Job], None]


class JobQueue(Protocol):
    def publish(self, job: Job) -> None: ...


class InProcessQueue:
    """FIFO queue drained in the calling process. Handlers may publish more jobs."""

    def __init__(self) -> None:
        self._jobs: deque[Job] = deque()
        self._handlers: dict[JobType, Handler] = {}
        # Jobs whose handler raised, with the error. One failed job never stops the
        # rest of the queue (the Pub/Sub queue of spec 09 dead-letters them instead).
        self.failed: list[tuple[Job, str]] = []

    def register(self, job_type: JobType, handler: Handler) -> None:
        self._handlers[job_type] = handler

    def publish(self, job: Job) -> None:
        self._jobs.append(job)

    def __len__(self) -> int:
        return len(self._jobs)

    def drain(self, max_jobs: int | None = None) -> int:
        """Run queued jobs in order until the queue is empty or `max_jobs` is reached."""
        handled = 0
        while self._jobs and (max_jobs is None or handled < max_jobs):
            job = self._jobs.popleft()
            handler = self._handlers.get(job.type)
            if handler is None:
                raise LookupError(f"No handler registered for job type {job.type.value!r}.")
            try:
                handler(job)
            except Exception as exc:  # isolate the job; the queue goes on
                self.failed.append((job, f"{type(exc).__name__}: {exc}"))
                log.exception("job failed", extra={"fields": {"type": job.type.value}})
            handled += 1
        return handled
