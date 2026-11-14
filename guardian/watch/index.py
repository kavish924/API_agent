"""A cached index of one SDK's GitHub releases (spec 01 F1.2–F1.5, F1.13).

The index and every release body live in the blob store, so a poll or a run needs one
conditional request when nothing changed. Page 1 (the newest releases) is refreshed
with an ETag; older pages are fetched once, when a run first needs the full history.

Each entry is either `handled` (already announced, or historical) or not. The poller
turns unhandled entries into source documents, which keeps a crash between "fetched"
and "stored" from losing a release.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from packaging.version import Version

from guardian.common.blobstore import BlobStore
from guardian.watch.github import GitHubClient, Release
from guardian.watch.sources import SdkSource


@dataclass
class IndexEntry:
    tag: str
    prerelease: bool
    published_at: str | None
    sha256: str
    handled: bool


@dataclass(frozen=True)
class Refresh:
    not_modified: bool
    added: tuple[Version, ...] = ()
    changed: tuple[Version, ...] = ()  # body edited upstream


class ReleaseIndex:
    def __init__(self, source: SdkSource, github: GitHubClient, blobs: BlobStore) -> None:
        self.source = source
        self._github = github
        self._blobs = blobs
        self.etag: str | None = None
        self.next_page = 1  # next page of history not yet fetched
        self.complete = False  # every page has been fetched at least once
        self.entries: dict[str, IndexEntry] = {}
        self._load()

    # --- persistence ---------------------------------------------------------

    @property
    def _key(self) -> str:
        return f"sdk/{self.source.package}/index.json"

    def _raw_key(self, version: Version) -> str:
        return f"sdk/{self.source.package}/raw/{version}.md"

    def _load(self) -> None:
        if not self._blobs.exists(self._key):
            return
        data = json.loads(self._blobs.get(self._key))
        self.etag = data["etag"]
        self.next_page = data["next_page"]
        self.complete = data["complete"]
        self.entries = {key: IndexEntry(**value) for key, value in data["entries"].items()}

    def save(self) -> None:
        data = {
            "etag": self.etag,
            "next_page": self.next_page,
            "complete": self.complete,
            "entries": {key: asdict(entry) for key, entry in self.entries.items()},
        }
        self._blobs.put(self._key, json.dumps(data, indent=1).encode())

    # --- fetching ------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return self.next_page == 1

    def _ingest(
        self, releases: tuple[Release, ...], *, handled: bool
    ) -> tuple[list[Version], list[Version]]:
        added: list[Version] = []
        changed: list[Version] = []
        for release in releases:
            version = self.source.version_of(release.tag)
            if release.draft or version is None:
                continue
            body = release.body.encode()  # stored exactly as received
            digest = hashlib.sha256(body).hexdigest()
            prior = self.entries.get(str(version))
            if prior is not None and prior.sha256 == digest:
                continue
            self._blobs.put(self._raw_key(version), body)
            if prior is None:
                added.append(version)
                self.entries[str(version)] = IndexEntry(
                    tag=release.tag,
                    prerelease=release.prerelease or version.is_prerelease,
                    published_at=release.published_at,
                    sha256=digest,
                    handled=handled,
                )
            else:
                changed.append(version)
                prior.sha256 = digest
        return added, changed

    def refresh(self) -> Refresh:
        """Re-read the newest page with a conditional request.

        The first refresh is a baseline: everything it finds is marked handled, so
        installing the watcher does not announce a project's existing releases.
        """
        baseline = self.is_empty
        page = self._github.releases_page(self.source.repo, 1, etag=self.etag)
        if page.not_modified:
            return Refresh(not_modified=True)
        added, changed = self._ingest(page.releases, handled=baseline)
        self.etag = page.etag
        if baseline:
            self.next_page = 2
            self.complete = not page.has_next
        self.save()
        return Refresh(not_modified=False, added=tuple(added), changed=tuple(changed))

    def ensure_complete(self) -> None:
        """Fetch the rest of the history once. Needed before computing a version range.

        Releases are listed by date, not by version (Stripe ships backports to old
        majors), so "every version above X" is only known from the full list.
        """
        if self.is_empty:
            self.refresh()
        while not self.complete:
            page = self._github.releases_page(self.source.repo, self.next_page)
            self._ingest(page.releases, handled=True)
            self.next_page += 1
            self.complete = not page.has_next
            self.save()

    # --- queries -------------------------------------------------------------

    def versions(self, *, include_prereleases: bool = False) -> list[Version]:
        return sorted(
            Version(key)
            for key, entry in self.entries.items()
            if include_prereleases or not entry.prerelease
        )

    def latest_stable(self) -> Version | None:
        stable = self.versions()
        return stable[-1] if stable else None

    def unhandled(self) -> list[Version]:
        return sorted(Version(key) for key, entry in self.entries.items() if not entry.handled)

    def mark_handled(self, version: Version) -> None:
        self.entries[str(version)].handled = True

    def entry(self, version: Version) -> IndexEntry:
        return self.entries[str(version)]

    def raw(self, version: Version) -> bytes:
        return self._blobs.get(self._raw_key(version))
