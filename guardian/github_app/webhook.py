"""The webhook service (spec 07 F7.3, F7.4).

It does three things: check the HMAC SHA-256 signature, enqueue an `event` job for the
events the App acts on, and return at once. An invalid signature gets 401 and nothing
is enqueued. All work happens in the worker.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response

from guardian.common.jobs import Job, JobType
from guardian.common.log import get_logger

log = get_logger(__name__)

HANDLED_EVENTS = frozenset({"installation", "installation_repositories"})


def valid_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def create_app(
    secret: bytes,
    publish: Callable[[Job], None],
    *,
    after_publish: Callable[[], None] | None = None,
) -> FastAPI:
    """The webhook app. `after_publish` lets a local process drain its own queue."""
    app = FastAPI(title="API Change Guardian webhook", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        body = await request.body()
        if not valid_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("webhook rejected: bad signature")
            return Response(status_code=401)
        event = request.headers.get("X-GitHub-Event", "")
        if event not in HANDLED_EVENTS:
            return Response(status_code=202)
        try:
            payload: dict[str, Any] = await request.json()
        except ValueError:
            return Response(status_code=400)
        delivery = request.headers.get("X-GitHub-Delivery", "")
        publish(Job(JobType.EVENT, {"event": event, "delivery": delivery, "payload": payload}))
        log.info("webhook accepted", extra={"fields": {"event": event, "delivery": delivery}})
        if after_publish is not None:
            after_publish()
        return Response(status_code=202)

    return app
