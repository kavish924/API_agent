"""A read-only copy of an installed repository at its default-branch commit (spec 04 F4.1).

The commit is downloaded as a tarball and extracted with checks: no absolute paths, no
`..`, no links or devices, and size limits. The pipeline then reads it exactly like a
local checkout. Nothing in it is executed.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from guardian.github_app.client import GitHubApp

MAX_MEMBERS = 50_000
MAX_BYTES = 500_000_000


class CheckoutError(Exception):
    pass


@dataclass(frozen=True)
class RemoteRepo:
    full_name: str
    installation_id: int
    default_branch: str
    head_sha: str
    path: Path


def repo_head(gh: GitHubApp, installation_id: int, full_name: str) -> tuple[str, str]:
    """(default branch, its head commit)."""
    info = gh.get(installation_id, f"/repos/{full_name}")
    branch = str(info["default_branch"])
    commit = gh.get(installation_id, f"/repos/{full_name}/commits/{branch}")
    return branch, str(commit["sha"])


def extract_tarball(data: bytes, destination: Path) -> None:
    """Extract a GitHub tarball, dropping its top-level directory. Unsafe members fail."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise CheckoutError(f"not a tarball: {exc}") from exc
    with archive:  # opened above so a bad archive is a CheckoutError, not a TarError
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise CheckoutError(f"too many files: {len(members)}")
        if sum(m.size for m in members) > MAX_BYTES:
            raise CheckoutError("repository is too large")
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts or "\\" in member.name:
                raise CheckoutError(f"unsafe path in tarball: {member.name!r}")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                continue  # links and devices are never extracted
            relative = PurePosixPath(*parts[1:]) if len(parts) > 1 else None
            if relative is None or not str(relative):
                continue
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def fetch_checkout(
    gh: GitHubApp, installation_id: int, full_name: str, work_dir: Path
) -> RemoteRepo:
    branch, sha = repo_head(gh, installation_id, full_name)
    path = work_dir / "checkouts" / full_name.replace("/", "__") / sha
    marker = path / ".guardian-complete"
    if not marker.exists():
        if path.exists():
            shutil.rmtree(path)
        response = gh.request(installation_id, "GET", f"/repos/{full_name}/tarball/{sha}")
        extract_tarball(response.content, path)
        marker.write_text(sha, encoding="utf-8")
    return RemoteRepo(full_name, installation_id, branch, sha, path)
