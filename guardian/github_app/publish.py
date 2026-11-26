"""Deliver a run: one draft PR, one issue, or nothing (spec 07 F7.6–F7.15, F7.17, F7.19).

- **PR** when something was fixed and the patch verified (L1 or better): branch
  `guardian/upgrade-<package>` (or `guardian/api-<source>`) is created or reset from
  the scanned commit with one commit holding every change, and a **draft** PR is
  opened or updated (one per repo and subject).
- **Issue** when nothing was fixed or nothing verified, and always in issue-only mode
  (where the diff goes in the issue and nothing is pushed). Edited in place later.
- **Nothing** when the affected changes were all declined (a closed PR or issue
  covered them), or the repo config turned this subject off.

Every write goes through `deliver`, which honours the pause flag. A redelivered job
finds the same commit, PR and issue and changes nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from guardian.common.context import AppContext
from guardian.common.db import session_scope
from guardian.common.log import get_logger
from guardian.common.models import PullRequest, Repo
from guardian.github_app.checkout import RemoteRepo
from guardian.github_app.client import GitHubApp, GitHubError
from guardian.github_app.config import RepoConfig
from guardian.github_app.delivery import Delivery, deliver
from guardian.github_app.render import API, Report, issue_body, issue_title, pr_body, pr_title

log = get_logger(__name__)

PR_OPENED = "pr-opened"
PR_UPDATED = "pr-updated"
ISSUE_OPENED = "issue-opened"
ISSUE_UPDATED = "issue-updated"
DECLINED = "declined"  # every affected change was in a closed PR or issue (F7.11)
SKIPPED = "skipped-by-config"
PAUSED = "paused"

BRANCH_PREFIX = "guardian/"


@dataclass(frozen=True)
class Published:
    action: str
    number: int | None = None
    url: str | None = None
    reason: str = ""


def change_id(key: str) -> int:
    """A stable integer for a change key, for the JSON id lists in `pull_requests`."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:15], 16)


def branch_for(report: Report) -> str:
    name = f"api-{report.subject}" if report.kind == API else f"upgrade-{report.package}"
    branch = BRANCH_PREFIX + name
    assert branch.startswith(BRANCH_PREFIX)  # F7.7: only ever the app's own branches
    return branch


class Publisher:
    def __init__(self, ctx: AppContext, gh: GitHubApp, remote: RemoteRepo) -> None:
        self.ctx = ctx
        self.gh = gh
        self.remote = remote
        self.inst = remote.installation_id
        self.repo = remote.full_name

    # --- the decision --------------------------------------------------------------

    def publish(self, report: Report, config: RepoConfig) -> Published:
        if report.kind == API and config.api_changes == "off":
            return Published(SKIPPED, reason="api_changes is off in .github/guardian.yml")
        if report.kind != API and not config.wants(report.package):
            return Published(SKIPPED, reason=f"{report.package} is not in packages")
        affected = {change_id(item.change_key) for item in report.items}
        declined = self._declined(report.subject)
        if affected and affected <= declined:
            return Published(DECLINED, reason="every affected change was declined before")

        issue_only = config.mode == "issue" or (
            report.kind == API and config.api_changes == "issue"
        )
        patch = report.patch
        fixed = report.fixed > 0 and patch is not None and bool(patch.files)
        verified = report.verification is not None and report.verification.verified
        if issue_only:
            diff = patch.diff() if fixed and patch is not None else None
            why = "Issue-only mode is set for this repository, so no code is pushed."
            return self._issue(report, affected, diff=diff, why=why)
        if fixed and verified:
            return self._pull_request(report, affected)
        why = (
            "No change could be fixed automatically."
            if not fixed
            else "The fix could not be verified, so no PR was opened."
        )
        return self._issue(report, affected, diff=None, why=why)

    # --- memory of closed PRs and issues (F7.11, F7.12) ------------------------------
    #
    # One `pull_requests` row per (repo, key): the subject for its PR, `<subject>:issue`
    # for its issue. The row follows the current item; `declined_change_ids` only grows.

    @staticmethod
    def _key(subject: str, kind: str) -> str:
        return subject if kind == "pr" else f"{subject}:issue"

    def _repo_id(self) -> int:
        with session_scope(self.ctx.sessions) as session:
            repo = session.scalars(
                select(Repo).where(Repo.installation_id == self.inst, Repo.full_name == self.repo)
            ).first()
            if repo is None:
                repo = Repo(
                    installation_id=self.inst,
                    full_name=self.repo,
                    default_branch=self.remote.default_branch,
                )
                session.add(repo)
                session.flush()
            return repo.id

    def _row(self, key: str) -> PullRequest | None:
        repo_id = self._repo_id()
        with session_scope(self.ctx.sessions) as session:
            row = session.scalars(
                select(PullRequest).where(
                    PullRequest.repo_id == repo_id, PullRequest.subject == key
                )
            ).first()
            if row is not None:
                session.expunge(row)
            return row

    def _declined(self, subject: str) -> set[int]:
        """Refresh open items from GitHub; return every change id a closed item declined."""
        declined: set[int] = set()
        for kind in ("pr", "issue"):
            row = self._row(self._key(subject, kind))
            if row is None:
                continue
            if row.state == "open" and row.number is not None:
                state = self._remote_state(row)
                if state != "open":
                    self._close_row(row.id, state)
                    row = self._row(self._key(subject, kind))
                    assert row is not None
            declined |= set(row.declined_change_ids or [])
        return declined

    def _remote_state(self, row: PullRequest) -> str:
        kind = "pulls" if row.kind == "pr" else "issues"
        try:
            data = self.gh.get(self.inst, f"/repos/{self.repo}/{kind}/{row.number}")
        except GitHubError as exc:
            if exc.status in (404, 410):
                return "closed"
            raise
        if data.get("state") == "open":
            return "open"
        return "merged" if row.kind == "pr" and data.get("merged_at") else "closed"

    def _close_row(self, row_id: int, state: str) -> None:
        with session_scope(self.ctx.sessions) as session:
            row = session.get(PullRequest, row_id)
            if row is None:
                return
            row.state = state
            if state == "closed":  # closed without merging: what it covered is declined
                row.declined_change_ids = sorted(
                    set(row.declined_change_ids or []) | set(row.covered_change_ids or [])
                )

    def _open_row(self, subject: str, kind: str) -> PullRequest | None:
        row = self._row(self._key(subject, kind))
        return row if row is not None and row.state == "open" else None

    def _save_row(
        self, subject: str, kind: str, number: int, branch: str | None, covered: set[int]
    ) -> None:
        repo_id, key = self._repo_id(), self._key(subject, kind)
        with session_scope(self.ctx.sessions) as session:
            row = session.scalars(
                select(PullRequest).where(
                    PullRequest.repo_id == repo_id, PullRequest.subject == key
                )
            ).first()
            if row is None:
                row = PullRequest(repo_id=repo_id, subject=key, declined_change_ids=[])
                session.add(row)
            row.kind, row.number, row.branch, row.state = kind, number, branch, "open"
            row.covered_change_ids = sorted(covered)

    # --- writes ----------------------------------------------------------------------

    def _write(self, what: str, write: Callable[[], Published]) -> Published:
        box: list[Published] = []
        if deliver(self.ctx.is_paused, lambda: box.append(write()), what=what) == Delivery.PAUSED:
            return Published(PAUSED, reason="deliveries are paused")
        return box[0]

    def _pull_request(self, report: Report, affected: set[int]) -> Published:
        branch = branch_for(report)
        assert report.patch is not None
        report.related_prs = self._related(report.package) if report.kind != API else []

        patch = report.patch

        def write() -> Published:
            files = {path: after for path, (_, after) in patch.files.items()}
            self._commit(branch, files, _commit_message(report))
            title, body = pr_title(report), pr_body(report)
            existing = self._find_open_pr(branch)
            if existing is not None:
                number = int(existing["number"])
                self.gh.request(
                    self.inst,
                    "PATCH",
                    f"/repos/{self.repo}/pulls/{number}",
                    json={"title": title, "body": body},
                )
                self._save_row(report.subject, "pr", number, branch, affected)
                return Published(PR_UPDATED, number, str(existing.get("html_url", "")))
            created = self.gh.request(
                self.inst,
                "POST",
                f"/repos/{self.repo}/pulls",
                json={
                    "title": title,
                    "head": branch,
                    "base": self.remote.default_branch,
                    "body": body,
                    "draft": True,
                },
            ).json()
            number = int(created["number"])
            self._save_row(report.subject, "pr", number, branch, affected)
            return Published(PR_OPENED, number, str(created.get("html_url", "")))

        return self._write(f"draft PR on {self.repo} for {report.subject}", write)

    def _issue(
        self, report: Report, affected: set[int], *, diff: str | None, why: str
    ) -> Published:
        def write() -> Published:
            title, body = issue_title(report), issue_body(report, diff=diff, why=why)
            row = self._open_row(report.subject, "issue")
            if row is not None and row.number is not None:
                if self._remote_state(row) == "open":
                    self.gh.request(
                        self.inst,
                        "PATCH",
                        f"/repos/{self.repo}/issues/{row.number}",
                        json={"title": title, "body": body},
                    )
                    self._save_row(report.subject, "issue", row.number, None, affected)
                    return Published(ISSUE_UPDATED, row.number)
                self._close_row(row.id, "closed")
            created = self.gh.request(
                self.inst, "POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body}
            ).json()
            number = int(created["number"])
            self._save_row(report.subject, "issue", number, None, affected)
            return Published(ISSUE_OPENED, number, str(created.get("html_url", "")))

        return self._write(f"issue on {self.repo} for {report.subject}", write)

    def _commit(self, branch: str, files: dict[str, str], message: str) -> str:
        """One commit on `branch` whose parent is the scanned commit (F7.6, F7.19)."""
        base = self.remote.head_sha
        repo = f"/repos/{self.repo}"
        base_tree = self.gh.get(self.inst, f"{repo}/git/commits/{base}")["tree"]["sha"]
        modes = self._modes(base_tree, files)
        tree = self.gh.request(
            self.inst,
            "POST",
            f"{repo}/git/trees",
            json={
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": path,
                        "mode": modes.get(path, "100644"),
                        "type": "blob",
                        "content": text,
                    }
                    for path, text in sorted(files.items())
                ],
            },
        ).json()["sha"]
        current: str | None = None
        try:
            current = self.gh.get(self.inst, f"{repo}/git/ref/heads/{branch}")["object"]["sha"]
        except GitHubError as exc:
            if exc.status != 404:
                raise
        if current is not None:
            head = self.gh.get(self.inst, f"{repo}/git/commits/{current}")
            parents = [p["sha"] for p in head.get("parents", [])]
            if head["tree"]["sha"] == tree and parents == [base]:
                return current  # the same change is already there: nothing to write
        commit = self.gh.request(
            self.inst,
            "POST",
            f"{repo}/git/commits",
            json={
                "message": message,
                "tree": tree,
                "parents": [base],
            },
        ).json()["sha"]
        if current is None:
            self.gh.request(
                self.inst,
                "POST",
                f"{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": commit},
            )
        else:  # only ever the app's own branch is force-moved (F7.7)
            self.gh.request(
                self.inst,
                "PATCH",
                f"{repo}/git/refs/heads/{branch}",
                json={"sha": commit, "force": True},
            )
        return str(commit)

    def _modes(self, base_tree: str, files: dict[str, str]) -> dict[str, str]:
        """Each changed file's mode in the base tree, so an executable stays executable."""
        tree = self.gh.get(
            self.inst, f"/repos/{self.repo}/git/trees/{base_tree}", params={"recursive": "1"}
        )
        modes: dict[str, str] = {}
        for entry in tree.get("tree", []):
            path, mode = entry.get("path"), entry.get("mode")
            if path in files and mode in ("100644", "100755"):
                modes[path] = mode
        return modes

    def _find_open_pr(self, branch: str) -> dict[str, Any] | None:
        owner = self.repo.split("/", 1)[0]
        pulls = self.gh.get(
            self.inst,
            f"/repos/{self.repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        return pulls[0] if pulls else None

    def _related(self, package: str) -> list[str]:
        """Open Dependabot or Renovate PRs that bump the same package (F7.14)."""
        try:
            pulls = self.gh.get(
                self.inst, f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100}
            )
        except GitHubError:
            return []
        prefixes = (f"dependabot/pip/{package}-", f"renovate/{package}")
        return [
            f"#{p['number']}"
            for p in pulls
            if str(p.get("head", {}).get("ref", "")).startswith(prefixes)
        ]


def _commit_message(report: Report) -> str:
    if report.kind == API:
        return (
            f"Replace OpenAI models retiring {report.shutdown_date}\n\n"
            "Behaviour change: output, cost and limits may differ."
        )
    return (
        f"Upgrade {report.package} {report.old} -> {report.new} and fix the code it breaks\n\n"
        "Version bump and fixes in one commit (API Change Guardian)."
    )
