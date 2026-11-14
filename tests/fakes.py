"""A fake GitHub API for tests: releases with ETags and pagination, and repo files."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Any

import httpx

OPENAI = "openai/openai-python"
STRIPE = "stripe/stripe-python"

_PATH = re.compile(
    r"^/repos/(?P<repo>[^/]+/[^/]+)/(?:(?P<releases>releases)|contents/(?P<file>.+))$"
)


class FakeGitHub:
    def __init__(self, page_size: int = 100) -> None:
        self.page_size = page_size
        self.releases: dict[str, list[dict[str, Any]]] = {OPENAI: [], STRIPE: []}  # newest first
        self.files: dict[tuple[str, str], str] = {}
        self.fail_with: dict[str, int] = {}  # repo -> status returned on every request
        self.rate_limit_once: set[str] = set()  # repos that answer 429 one time
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []

    # --- test setup ----------------------------------------------------------

    def add_release(
        self, repo: str, tag: str, body: str = "", *, prerelease: bool = False, draft: bool = False
    ) -> None:
        self.releases.setdefault(repo, []).insert(
            0,
            {
                "tag_name": tag,
                "body": body,
                "prerelease": prerelease,
                "draft": draft,
                "published_at": f"2026-01-{len(self.releases[repo]) + 1:02d}T00:00:00Z",
            },
        )

    def edit_release(self, repo: str, tag: str, body: str) -> None:
        next(r for r in self.releases[repo] if r["tag_name"] == tag)["body"] = body

    def set_file(self, repo: str, path: str, text: str) -> None:
        self.files[(repo, path)] = text

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    # --- inspection ----------------------------------------------------------

    def reset_log(self) -> None:
        self.requests.clear()
        self.statuses.clear()

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    # --- handler -------------------------------------------------------------

    def _respond(self, status: int, **kwargs: Any) -> httpx.Response:
        self.statuses.append(status)
        return httpx.Response(status, **kwargs)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        match = _PATH.match(request.url.path)
        if match is None:
            return self._respond(404, json={"message": "Not Found"})
        repo = match.group("repo")
        if repo in self.rate_limit_once:
            self.rate_limit_once.discard(repo)
            return self._respond(429, headers={"Retry-After": "3"}, json={"message": "slow down"})
        if repo in self.fail_with:
            return self._respond(self.fail_with[repo], json={"message": "boom"})

        if match.group("releases"):
            if repo not in self.releases:
                return self._respond(404, json={"message": "Not Found"})
            return self._releases(request, repo)
        return self._file(request, repo, match.group("file"))

    def _releases(self, request: httpx.Request, repo: str) -> httpx.Response:
        items = self.releases[repo]
        etag = (
            '"' + hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16] + '"'
        )
        if request.headers.get("if-none-match") == etag:
            return self._respond(304)
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * self.page_size
        headers = {"ETag": etag}
        if start + self.page_size < len(items):
            headers["Link"] = f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
        return self._respond(200, json=items[start : start + self.page_size], headers=headers)

    def _file(self, request: httpx.Request, repo: str, path: str) -> httpx.Response:
        text = self.files.get((repo, path))
        if text is None:
            return self._respond(404, json={"message": "Not Found"})
        etag = '"' + hashlib.sha256(text.encode()).hexdigest()[:16] + '"'
        if request.headers.get("if-none-match") == etag:
            return self._respond(304)
        return self._respond(200, text=text, headers={"ETag": etag})


class FakePyPI:
    """A fake PyPI: the JSON API for one release, and the wheel file it points to."""

    def __init__(self) -> None:
        self.wheels: dict[tuple[str, str], bytes] = {}
        self.sdist_only: set[tuple[str, str]] = set()
        self.corrupt: set[tuple[str, str]] = set()  # served bytes will not match the hash
        self.requests: list[httpx.Request] = []

    def add_wheel(self, package: str, version: str, files: dict[str, str]) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, source in files.items():
                archive.writestr(name, source.lstrip("\n"))
        self.wheels[(package, version)] = buffer.getvalue()

    def add_raw_wheel(self, package: str, version: str, data: bytes) -> None:
        self.wheels[(package, version)] = data

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        meta = re.match(r"^/pypi/(?P<package>[^/]+)/(?P<version>[^/]+)/json$", path)
        if meta:
            key = (meta["package"], meta["version"])
            if key in self.sdist_only:
                return httpx.Response(200, json={"urls": [{"packagetype": "sdist"}]})
            if key not in self.wheels:
                return httpx.Response(404, json={"message": "Not Found"})
            name = f"{key[0]}-{key[1]}-py3-none-any.whl"
            return httpx.Response(
                200,
                json={
                    "urls": [
                        {"packagetype": "sdist", "filename": f"{key[0]}-{key[1]}.tar.gz"},
                        {
                            "packagetype": "bdist_wheel",
                            "filename": name,
                            "url": f"https://files.pythonhosted.org/packages/{name}",
                            "digests": {"sha256": hashlib.sha256(self.wheels[key]).hexdigest()},
                        },
                    ]
                },
            )
        wheel = re.match(r"^/packages/(?P<package>.+)-(?P<version>[^-]+)-py3-none-any\.whl$", path)
        if wheel and (wheel["package"], wheel["version"]) in self.wheels:
            key = (wheel["package"], wheel["version"])
            data = self.wheels[key]
            return httpx.Response(200, content=data + b"x" if key in self.corrupt else data)
        return httpx.Response(404, json={"message": "Not Found"})


def combined_client(github: FakeGitHub, pypi: FakePyPI) -> httpx.Client:
    """One HTTP client that routes by host, like the real one the app uses."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return github._handle(request)
        return pypi.handle(request)

    return httpx.Client(transport=httpx.MockTransport(route))
