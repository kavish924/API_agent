"""Cached surfaces and diffs (spec 03 F3.5, F3.6, F3.12).

A surface is built once per (package, version) and a diff once per (package, old, new).
Both are stored as JSON in the blob store, which is the cache and the audit record.
A second run over the same versions touches neither PyPI nor griffe.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from guardian.apidiff.compare import ApiDiff, compare
from guardian.apidiff.surface import FORMAT, ApiSymbol, Surface, build_surface
from guardian.apidiff.wheels import PYPI_URL, fetch_wheel
from guardian.common.blobstore import BlobStore
from guardian.common.context import AppContext
from guardian.common.log import get_logger
from guardian.watch.sources import SdkSource

log = get_logger(__name__)


class ApiDiffService:
    def __init__(
        self,
        http: httpx.Client,
        blobs: BlobStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        pypi_url: str = PYPI_URL,
    ) -> None:
        self._http = http
        self._blobs = blobs
        self._sleep = sleep
        self._pypi_url = pypi_url
        self._surfaces: dict[tuple[str, str], Surface] = {}

    @classmethod
    def from_context(cls, ctx: AppContext) -> ApiDiffService:
        return cls(ctx.http, ctx.blobs, sleep=ctx.sleep)

    @staticmethod
    def surface_key(source: SdkSource, version: str) -> str:
        return f"sdk/{source.package}/surface-v{FORMAT}/{version}.json"

    @staticmethod
    def diff_key(source: SdkSource, old: str, new: str) -> str:
        return f"sdk/{source.package}/diff-v{FORMAT}/{old}__{new}.json"

    def surface(self, source: SdkSource, version: str) -> Surface:
        memo = (source.package, version)
        if memo in self._surfaces:
            return self._surfaces[memo]
        key = self.surface_key(source, version)
        if self._blobs.exists(key):
            surface = Surface.from_json(self._blobs.get(key).decode())
        else:
            started = time.perf_counter()
            with tempfile.TemporaryDirectory(prefix="guardian-wheel-") as temp:
                root = Path(temp)
                fetch_wheel(
                    self._http,
                    source.package,
                    version,
                    root,
                    sleep=self._sleep,
                    pypi_url=self._pypi_url,
                )
                surface = build_surface(root, source.module, version)
            self._blobs.put(key, surface.to_json().encode())
            log.info(
                "surface built",
                extra={
                    "fields": {
                        "package": source.package,
                        "version": version,
                        "public_names": len(surface.public),
                        "unresolved": len(surface.unresolved),
                        "seconds": round(time.perf_counter() - started, 1),
                    }
                },
            )
        self._surfaces[memo] = surface
        return surface

    def diff(self, source: SdkSource, old: str, new: str) -> ApiDiff:
        key = self.diff_key(source, old, new)
        if self._blobs.exists(key):
            return ApiDiff.from_json(self._blobs.get(key).decode())
        result = compare(self.surface(source, old), self.surface(source, new))
        self._blobs.put(key, result.to_json().encode())
        return result

    def lookup(self, source: SdkSource, version: str, symbol: str) -> ApiSymbol | None:
        """Signature and docstring of `symbol` in `version` (the agent's `lookup_api`)."""
        return self.surface(source, version).lookup(symbol)
