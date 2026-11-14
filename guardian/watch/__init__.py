"""Change watching: poll sources, release gate, fan-out, version range (spec 01)."""

from __future__ import annotations

from guardian.common.context import AppContext
from guardian.watch.github import GitHubClient


def github_client(ctx: AppContext) -> GitHubClient:
    """A read-only GitHub client for public release data, built from the context."""
    token = ctx.settings.github_token
    return GitHubClient(
        ctx.http,
        token=token.get_secret_value() if token is not None else None,
        sleep=ctx.sleep,
    )
