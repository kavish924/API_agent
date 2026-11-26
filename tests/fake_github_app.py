"""An in-memory GitHub for the App's calls: tokens, repos, git data, pulls, issues.

It is strict where the real API is: installation tokens need a JWT signed by the App's
key, refs can only move forward unless forced, and a tree that touches
`.github/workflows/` is refused (the App has no Workflows permission, F7.2).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = "4242"


def make_key() -> tuple[str, str]:
    """(private PEM, public PEM) for a test App."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def _sha(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()


@dataclass
class FakeRepo:
    full_name: str
    default_branch: str = "main"
    trees: dict[str, dict[str, str]] = field(default_factory=dict)  # sha -> files
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)  # sha -> {tree, parents}
    refs: dict[str, str] = field(default_factory=dict)  # branch -> commit sha
    items: dict[int, dict[str, Any]] = field(default_factory=dict)  # pulls and issues by number
    modes: dict[str, str] = field(default_factory=dict)  # path -> mode, 100644 if unset
    sent_modes: dict[str, str] = field(default_factory=dict)  # modes the last tree POST used

    def head(self, branch: str | None = None) -> str:
        return self.refs[branch or self.default_branch]

    def files_at(self, commit: str) -> dict[str, str]:
        return self.trees[self.commits[commit]["tree"]]

    def pulls(self) -> list[dict[str, Any]]:
        return [i for i in self.items.values() if i["kind"] == "pr"]

    def issues(self) -> list[dict[str, Any]]:
        return [i for i in self.items.values() if i["kind"] == "issue"]


class FakeGitHubApp:
    def __init__(self, public_key: str) -> None:
        self.public_key = public_key
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[httpx.Request] = []
        self.writes: list[str] = []  # "METHOD path" for every write
        self._next = 1

    # --- setup ------------------------------------------------------------------------

    def add_repo(self, full_name: str, files: dict[str, str], branch: str = "main") -> FakeRepo:
        repo = FakeRepo(full_name, branch)
        tree = self._tree(repo, files)
        commit = self._commit(repo, "initial", tree, [])
        repo.refs[branch] = commit
        self.repos[full_name] = repo
        return repo

    def push(self, full_name: str, files: dict[str, str]) -> str:
        """A new commit on the default branch (someone else's push)."""
        repo = self.repos[full_name]
        merged = {**repo.files_at(repo.head()), **files}
        commit = self._commit(repo, "push", self._tree(repo, merged), [repo.head()])
        repo.refs[repo.default_branch] = commit
        return commit

    def close(self, full_name: str, number: int, *, merged: bool = False) -> None:
        item = self.repos[full_name].items[number]
        item["state"] = "closed"
        item["merged_at"] = "2026-11-25T00:00:00Z" if merged else None

    def _tree(self, repo: FakeRepo, files: dict[str, str]) -> str:
        sha = _sha("tree", json.dumps(files, sort_keys=True))
        repo.trees[sha] = dict(files)
        return sha

    def _commit(self, repo: FakeRepo, message: str, tree: str, parents: list[str]) -> str:
        sha = _sha("commit", message, tree, *parents)
        repo.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    # --- the API ------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        body: Any = json.loads(request.content) if request.content else None
        if method != "GET":
            self.writes.append(f"{method} {path}")

        token = re.match(r"^/app/installations/(\d+)/access_tokens$", path)
        if token and method == "POST":
            return self._token(request, int(token.group(1)))
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ghs_"):
            return httpx.Response(401, json={"message": "Bad credentials"})

        match = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if match is None or match.group(1) not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = self.repos[match.group(1)], match.group(2) or ""
        return self._repo(repo, method, rest, body, request)

    def _token(self, request: httpx.Request, installation: int) -> httpx.Response:
        raw = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            claims = jwt.decode(raw, self.public_key, algorithms=["RS256"])
        except jwt.PyJWTError as exc:
            return httpx.Response(401, json={"message": f"bad JWT: {exc}"})
        if claims.get("iss") != APP_ID:
            return httpx.Response(401, json={"message": "wrong app"})
        return httpx.Response(
            201, json={"token": f"ghs_{installation}", "expires_at": "2099-01-01T00:00:00Z"}
        )

    def _repo(  # noqa: C901 - one routing table
        self, repo: FakeRepo, method: str, rest: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if rest == "" and method == "GET":
            return httpx.Response(
                200, json={"full_name": repo.full_name, "default_branch": repo.default_branch}
            )
        if m := re.match(r"^/commits/(.+)$", rest):
            ref = m.group(1)
            sha = repo.refs.get(ref, ref if ref in repo.commits else None)
            return httpx.Response(200, json={"sha": sha}) if sha else httpx.Response(404)
        if m := re.match(r"^/tarball/(.+)$", rest):
            return httpx.Response(200, content=self._tarball(repo, m.group(1)))
        if m := re.match(r"^/git/commits/(.+)$", rest):
            commit = repo.commits.get(m.group(1))
            if commit is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "sha": m.group(1),
                    "tree": {"sha": commit["tree"]},
                    "parents": [{"sha": p} for p in commit["parents"]],
                },
            )
        if (m := re.match(r"^/git/trees/(.+)$", rest)) and method == "GET":
            files = repo.trees.get(m.group(1))
            if files is None:
                return httpx.Response(404, json={"message": "Not Found"})
            entries = [
                {"path": path, "mode": repo.modes.get(path, "100644"), "type": "blob"}
                for path in sorted(files)
            ]
            return httpx.Response(200, json={"sha": m.group(1), "tree": entries})
        if rest == "/git/trees" and method == "POST":
            files = dict(repo.trees[body["base_tree"]])
            repo.sent_modes = {e["path"]: e["mode"] for e in body["tree"]}
            for entry in body["tree"]:
                if entry["path"].startswith(".github/workflows/"):
                    return httpx.Response(403, json={"message": "workflows permission"})
                files[entry["path"]] = entry["content"]
            return httpx.Response(201, json={"sha": self._tree(repo, files)})
        if rest == "/git/commits" and method == "POST":
            sha = self._commit(repo, body["message"], body["tree"], body["parents"])
            return httpx.Response(201, json={"sha": sha})
        if m := re.match(r"^/git/ref/heads/(.+)$", rest):
            sha = repo.refs.get(m.group(1))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": sha}})
        if rest == "/git/refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in repo.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            repo.refs[branch] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if (m := re.match(r"^/git/refs/heads/(.+)$", rest)) and method == "PATCH":
            branch = m.group(1)
            if branch == repo.default_branch:
                return httpx.Response(422, json={"message": "not allowed in this fake"})
            old = repo.refs.get(branch)
            if old is None:
                return httpx.Response(422, json={"message": "no such ref"})
            if not body.get("force") and old not in repo.commits[body["sha"]]["parents"]:
                return httpx.Response(422, json={"message": "not a fast forward"})
            repo.refs[branch] = body["sha"]
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        if rest == "/pulls" and method == "GET":
            params = request.url.params
            pulls = repo.pulls()
            if params.get("state", "open") != "all":
                pulls = [p for p in pulls if p["state"] == params.get("state", "open")]
            if "head" in params:
                pulls = [
                    p
                    for p in pulls
                    if f"{repo.full_name.split('/')[0]}:{p['head']['ref']}" == params["head"]
                ]
            return httpx.Response(200, json=pulls)
        if rest == "/pulls" and method == "POST":
            return self._create(repo, "pr", body)
        if rest == "/issues" and method == "POST":
            return self._create(repo, "issue", body)
        if m := re.match(r"^/(pulls|issues)/(\d+)$", rest):
            item = repo.items.get(int(m.group(2)))
            if item is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if method == "PATCH":
                item.update({k: v for k, v in body.items() if k in ("title", "body", "state")})
            return httpx.Response(200, json=item)
        return httpx.Response(404, json={"message": f"fake has no route for {method} {rest}"})

    def _create(self, repo: FakeRepo, kind: str, body: dict[str, Any]) -> httpx.Response:
        number = self._next
        self._next += 1
        segment = "pull" if kind == "pr" else "issues"
        item: dict[str, Any] = {
            "number": number,
            "kind": kind,
            "state": "open",
            "title": body["title"],
            "body": body.get("body", ""),
            "merged_at": None,
            "html_url": f"https://github.com/{repo.full_name}/{segment}/{number}",
        }
        if kind == "pr":
            item.update(
                {
                    "head": {"ref": body["head"]},
                    "base": {"ref": body["base"]},
                    "draft": bool(body.get("draft")),
                }
            )
        repo.items[number] = item
        return httpx.Response(201, json=item)

    def _tarball(self, repo: FakeRepo, ref: str) -> bytes:
        commit = repo.refs.get(ref, ref)
        files = repo.files_at(commit)
        buffer = io.BytesIO()
        top = f"{repo.full_name.replace('/', '-')}-{commit[:7]}"
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, text in sorted(files.items()):
                data = text.encode("utf-8")
                info = tarfile.TarInfo(f"{top}/{path}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()
