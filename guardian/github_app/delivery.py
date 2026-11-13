"""The single gate in front of every write to GitHub (spec 00 F0.12).

Opening a PR, pushing a branch and opening or editing an issue all go through
`deliver`. When the global pause flag is set nothing is written and the result is
`paused`, which the run records as its outcome.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from guardian.common.log import get_logger

log = get_logger(__name__)


class Delivery(StrEnum):
    DELIVERED = "delivered"
    PAUSED = "paused"


def deliver(is_paused: Callable[[], bool], write: Callable[[], None], *, what: str) -> Delivery:
    """Run `write` unless deliveries are paused. `what` describes the write for the log."""
    if is_paused():
        log.warning("delivery skipped: paused", extra={"fields": {"what": what}})
        return Delivery.PAUSED
    write()
    log.info("delivered", extra={"fields": {"what": what}})
    return Delivery.DELIVERED
