"""Source documents: the release notes later stages read (spec 01 F1.4, F1.5, F1.14, F1.17).

A document is stored once per (source, version, revision). An edit upstream becomes a
new revision under the same key, never a second record. The text is written to the
blob store before the database row, so nothing is processed that was not first kept.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from packaging.version import Version
from sqlalchemy import select

from guardian.common.blobstore import BlobStore
from guardian.common.db import SessionFactory, session_scope
from guardian.common.log import get_logger
from guardian.common.models import SourceDocument
from guardian.watch.changelog import extract_section, pointer_reason
from guardian.watch.github import GitHubClient
from guardian.watch.index import ReleaseIndex
from guardian.watch.sources import SdkSource

log = get_logger(__name__)

ORIGIN_RELEASE = "release"  # the GitHub release body
ORIGIN_CHANGELOG = "changelog"  # a CHANGELOG.md section (F1.14)


@dataclass(frozen=True)
class StoredDocument:
    id: int
    source_id: str
    package: str
    version: str
    revision: int
    origin: str
    blob_ref: str
    created: bool  # False when this exact content was already stored
    is_edit: bool  # True when it replaced an earlier revision


@dataclass(frozen=True)
class Gap:
    """A version with no usable release note anywhere (F1.17)."""

    version: str
    reason: str


class DocumentStore:
    def __init__(self, sessions: SessionFactory, blobs: BlobStore) -> None:
        self._sessions = sessions
        self._blobs = blobs

    def has(self, source_id: str, version: str) -> bool:
        with session_scope(self._sessions) as session:
            query = select(SourceDocument.id).where(
                SourceDocument.source_id == source_id, SourceDocument.version == version
            )
            return session.scalars(query.limit(1)).first() is not None

    def store(self, source: SdkSource, version: str, data: bytes, origin: str) -> StoredDocument:
        digest = hashlib.sha256(data).hexdigest()
        with session_scope(self._sessions) as session:
            latest = session.scalars(
                select(SourceDocument)
                .where(SourceDocument.source_id == source.id, SourceDocument.version == version)
                .order_by(SourceDocument.revision.desc())
                .limit(1)
            ).first()
            if latest is not None and latest.content_hash == digest:
                return self._view(source, latest, created=False, is_edit=False)
            revision = latest.revision + 1 if latest is not None else 1
            ref = self._blobs.put(f"sdk/{source.package}/{version}/r{revision}.{origin}.md", data)
            row = SourceDocument(
                source_id=source.id,
                version=version,
                revision=revision,
                content_hash=digest,
                blob_ref=ref,
            )
            session.add(row)
            session.flush()
            return self._view(source, row, created=True, is_edit=latest is not None)

    def text(self, document: StoredDocument) -> str:
        return self._blobs.get(document.blob_ref).decode()

    @staticmethod
    def _view(
        source: SdkSource, row: SourceDocument, *, created: bool, is_edit: bool
    ) -> StoredDocument:
        return StoredDocument(
            id=row.id,
            source_id=row.source_id,
            package=source.package,
            version=row.version,
            revision=row.revision,
            origin=row.blob_ref.rsplit(".", 2)[-2],
            blob_ref=row.blob_ref,
            created=created,
            is_edit=is_edit,
        )


class Notes:
    """Turns index entries into stored documents, with the changelog fallback."""

    def __init__(
        self,
        source: SdkSource,
        index: ReleaseIndex,
        github: GitHubClient,
        store: DocumentStore,
        blobs: BlobStore,
    ) -> None:
        self._source = source
        self._index = index
        self._github = github
        self._store = store
        self._blobs = blobs
        self._changelog_text: str | None = None
        self._changelog_loaded = False

    def materialize(self, version: Version) -> StoredDocument | Gap:
        """Store the release note for `version`, or say why there is none."""
        raw = self._index.raw(version)
        reason = pointer_reason(raw.decode())
        if reason is None:
            return self._store.store(self._source, str(version), raw, ORIGIN_RELEASE)

        changelog = self._changelog()
        tag_version = self._index.entry(version).tag.removeprefix(self._source.tag_prefix)
        section = None
        if changelog is not None:
            section = extract_section(changelog, tag_version) or extract_section(
                changelog, str(version)
            )
        log.info(
            "release body not usable",
            extra={
                "fields": {
                    "package": self._source.package,
                    "version": str(version),
                    "rule": reason,
                    "changelog_section_found": section is not None,
                }
            },
        )
        if section is None:
            return Gap(str(version), f"{reason}; no changelog section")
        return self._store.store(self._source, str(version), section.encode(), ORIGIN_CHANGELOG)

    def _changelog(self) -> str | None:
        """The project's changelog, fetched with a conditional request and cached."""
        if self._changelog_loaded:
            return self._changelog_text
        key = f"sdk/{self._source.package}/changelog.md"
        etag_key = f"sdk/{self._source.package}/changelog.etag"
        cached = self._blobs.get(key).decode() if self._blobs.exists(key) else None
        etag = self._blobs.get(etag_key).decode() if self._blobs.exists(etag_key) else None
        fetched = self._github.file(
            self._source.repo, self._source.changelog_path, etag=etag if cached else None
        )
        if fetched.not_modified:
            self._changelog_text = cached
        elif fetched.text is not None:
            self._blobs.put(key, fetched.text.encode())
            if fetched.etag:
                self._blobs.put(etag_key, fetched.etag.encode())
            self._changelog_text = fetched.text
        self._changelog_loaded = True
        return self._changelog_text
