"""Read-only GitHub client for public release data (spec 01).

Every request goes through the shared retry helper, so rate limits and transient
failures are handled in one place. Conditional requests (ETag) make an unchanged
source nearly free: a 304 does not count against the rate limit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from guardian.common.http import RetryPolicy, request_with_retry

API_URL = "https://api.github.com"
PER_PAGE = 100


class SourceError(Exception):
    """A change source could not be read."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Release:
    tag: str
    body: str
    prerelease: bool
    draft: bool
    published_at: str | None


@dataclass(frozen=True)
class ReleasePage:
    not_modified: bool
    releases: tuple[Release, ...] = ()
    etag: str | None = None
    has_next: bool = False


@dataclass(frozen=True)
class FileFetch:
    """`text` is None with `not_modified` False when the file does not exist."""

    not_modified: bool
    text: str | None = None
    etag: str | None = None


class GitHubClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        token: str | None = None,
        base_url: str = API_URL,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._policy = policy
        self._sleep = sleep

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        etag: str | None = None,
        accept: str = "application/vnd.github+json",
    ) -> httpx.Response:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "api-change-guardian",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if etag:
            headers["If-None-Match"] = etag
        try:
            return request_with_retry(
                self._http,
                "GET",
                f"{self._base_url}{path}",
                policy=self._policy,
                sleep=self._sleep,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"GET {path} failed: {type(exc).__name__}: {exc}") from exc

    def releases_page(self, repo: str, page: int = 1, etag: str | None = None) -> ReleasePage:
        """One page of releases, newest first. Pass `etag` for a conditional request."""
        response = self._get(
            f"/repos/{repo}/releases", params={"per_page": PER_PAGE, "page": page}, etag=etag
        )
        if response.status_code == 304:
            return ReleasePage(not_modified=True, etag=etag)
        if response.status_code != 200:
            raise SourceError(
                f"releases for {repo}: HTTP {response.status_code}", response.status_code
            )
        releases = tuple(
            Release(
                tag=str(item.get("tag_name") or ""),
                body=str(item.get("body") or ""),
                prerelease=bool(item.get("prerelease")),
                draft=bool(item.get("draft")),
                published_at=item.get("published_at"),
            )
            for item in response.json()
        )
        return ReleasePage(
            not_modified=False,
            releases=releases,
            etag=response.headers.get("etag"),
            has_next='rel="next"' in response.headers.get("link", ""),
        )

    def file(self, repo: str, path: str, etag: str | None = None) -> FileFetch:
        """Raw text of a file on the default branch."""
        response = self._get(
            f"/repos/{repo}/contents/{path}", etag=etag, accept="application/vnd.github.raw+json"
        )
        if response.status_code == 304:
            return FileFetch(not_modified=True, etag=etag)
        if response.status_code == 404:
            return FileFetch(not_modified=False)
        if response.status_code != 200:
            raise SourceError(
                f"{path} in {repo}: HTTP {response.status_code}", response.status_code
            )
        return FileFetch(not_modified=False, text=response.text, etag=response.headers.get("etag"))
