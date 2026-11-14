"""Fetch an SDK version's wheel from PyPI and unpack it for static reading (spec 03 F3.1).

A wheel is untrusted input. It is hash-checked, unzipped with path checks, and never
installed or imported. Sdists are never used: building one can run arbitrary code.
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from guardian.common.http import request_with_retry

PYPI_URL = "https://pypi.org"
MAX_FILES = 100_000
MAX_UNPACKED_BYTES = 1_000_000_000


class WheelError(Exception):
    """A wheel could not be fetched or is not safe to unpack."""


def _get(http: httpx.Client, url: str, sleep: Callable[[float], None]) -> httpx.Response:
    try:
        response = request_with_retry(http, "GET", url, sleep=sleep)
    except httpx.HTTPError as exc:
        raise WheelError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise WheelError(f"GET {url}: HTTP {response.status_code}")
    return response


def unpack_wheel(data: bytes, destination: Path) -> None:
    """Unzip a wheel, refusing any member that could land outside `destination`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise WheelError("not a zip file") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise WheelError(f"too many files in wheel: {len(members)}")
        if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
            raise WheelError("wheel is too large when unpacked")
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise WheelError(f"unsafe path in wheel: {name!r}")
        destination.mkdir(parents=True, exist_ok=True)
        archive.extractall(destination)


def fetch_wheel(
    http: httpx.Client,
    package: str,
    version: str,
    destination: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    pypi_url: str = PYPI_URL,
) -> None:
    """Download the wheel of `package` `version`, verify its hash, unpack it."""
    metadata = _get(http, f"{pypi_url}/pypi/{package}/{version}/json", sleep).json()
    wheels = [f for f in metadata.get("urls", []) if f.get("packagetype") == "bdist_wheel"]
    if not wheels:
        raise WheelError(f"{package} {version} has no wheel on PyPI (sdists are never used)")
    # Prefer the pure-Python wheel; any wheel has the same .py sources for our purpose.
    chosen = next((f for f in wheels if f["filename"].endswith("-none-any.whl")), wheels[0])
    data = _get(http, chosen["url"], sleep).content
    expected = chosen.get("digests", {}).get("sha256")
    if not expected or hashlib.sha256(data).hexdigest() != expected:
        raise WheelError(f"sha256 mismatch for {chosen['filename']}")
    unpack_wheel(data, destination)
