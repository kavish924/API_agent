"""GitHub App API access with installation tokens (spec 07 F7.1, F7.18).

The App signs a short-lived JWT with its private key, exchanges it for an
installation token, and uses that token for every repository call. Personal access
tokens are never used. Every call goes through the retry-with-backoff helper.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt

from guardian.common.http import request_with_retry

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
TOKEN_MARGIN_SECONDS = 300  # renew a token this long before it expires


class GitHubError(Exception):
    def __init__(self, status: int, method: str, path: str, message: str) -> None:
        super().__init__(f"{method} {path}: HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    private_key: str  # PEM


def app_jwt(credentials: AppCredentials, now: float) -> str:
    """The App's own JWT: valid ten minutes, backdated a minute for clock drift."""
    payload = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": credentials.app_id}
    return jwt.encode(payload, credentials.private_key, algorithm="RS256")


class GitHubApp:
    def __init__(
        self,
        http: httpx.Client,
        credentials: AppCredentials,
        *,
        sleep: Callable[[float], None] = time.sleep,
        api_url: str = API_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.http = http
        self.credentials = credentials
        self.sleep = sleep
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self._tokens: dict[int, tuple[str, float]] = {}

    def _headers(self, token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "api-change-guardian",
        }

    def token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached is not None and cached[1] - TOKEN_MARGIN_SECONDS > self.clock():
            return cached[0]
        path = f"/app/installations/{installation_id}/access_tokens"
        response = request_with_retry(
            self.http,
            "POST",
            self.api_url + path,
            headers=self._headers(app_jwt(self.credentials, self.clock())),
            sleep=self.sleep,
        )
        if response.status_code != 201:
            raise GitHubError(response.status_code, "POST", path, _message(response))
        data = response.json()
        expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
        self._tokens[installation_id] = (str(data["token"]), expires.timestamp())
        return str(data["token"])

    def request(
        self,
        installation_id: int,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        response = request_with_retry(
            self.http,
            method,
            self.api_url + path,
            headers=self._headers(self.token(installation_id), accept),
            json=json,
            params=params,
            sleep=self.sleep,
            follow_redirects=True,
        )
        if response.status_code not in ok:
            raise GitHubError(response.status_code, method, path, _message(response))
        return response

    def get(self, installation_id: int, path: str, **kwargs: Any) -> Any:
        return self.request(installation_id, "GET", path, **kwargs).json()


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:200]
    except ValueError:
        return response.text[:200]
