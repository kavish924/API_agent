"""Blob storage interface (spec 00 F0.7).

Holds raw release notes, page snapshots, API-diff JSON and run output, byte for byte.
The local-directory implementation is here; the Cloud Storage one is spec 09.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Store `data` under `key` and return a reference for `get`."""
        ...

    def get(self, ref: str) -> bytes: ...

    def exists(self, ref: str) -> bool: ...


class LocalBlobStore:
    """Stores blobs as files under one root directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # Keys are canonical forward-slash paths. Reject anything that could leave the
        # root, and anything non-canonical (`a//b`, `./a`) that would give one blob two
        # names. Check the raw segments: PurePosixPath would quietly normalise them.
        if not key or "\\" in key or ":" in key:
            raise ValueError(f"Invalid blob key: {key!r}")
        segments = key.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValueError(f"Invalid blob key: {key!r}")
        return self.root.joinpath(*PurePosixPath(key).parts)

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()

    def exists(self, ref: str) -> bool:
        return self._path(ref).is_file()
