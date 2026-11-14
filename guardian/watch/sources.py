"""The SDK change sources this version watches (spec 01 F1.1)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from sqlalchemy.orm import Session

from guardian.common.models import Source

SDK_RELEASE = "sdk-release"


@dataclass(frozen=True)
class SdkSource:
    id: str
    provider: str
    package: str  # name on PyPI
    repo: str  # GitHub "owner/name"
    tag_prefix: str = "v"
    changelog_path: str = "CHANGELOG.md"
    import_name: str | None = None  # top-level module, when it differs from `package`

    @property
    def module(self) -> str:
        return self.import_name or self.package

    def version_of(self, tag: str) -> Version | None:
        """Map a release tag to a version (`v1.45.0` → `1.45.0`), or None if it is not one."""
        try:
            return Version(tag.removeprefix(self.tag_prefix))
        except InvalidVersion:
            return None


SDK_SOURCES: tuple[SdkSource, ...] = (
    SdkSource(id="openai-python", provider="openai", package="openai", repo="openai/openai-python"),
    SdkSource(id="stripe-python", provider="stripe", package="stripe", repo="stripe/stripe-python"),
)


def source_for_package(
    package: str, sources: Iterable[SdkSource] = SDK_SOURCES
) -> SdkSource | None:
    wanted = canonicalize_name(package)
    return next((s for s in sources if canonicalize_name(s.package) == wanted), None)


def ensure_rows(session: Session, sources: Iterable[SdkSource]) -> None:
    """Create the `sources` rows that hold poll state, if they are missing."""
    for source in sources:
        if session.get(Source, source.id) is None:
            session.add(
                Source(
                    id=source.id,
                    kind=SDK_RELEASE,
                    provider=source.provider,
                    package=source.package,
                    url=f"https://github.com/{source.repo}",
                )
            )
