"""Per-repository settings in `.github/guardian.yml` (spec 07 F7.16, spec 10 F10.13).

Read from the default-branch commit the run scanned. Unknown keys and bad values fall
back to the defaults with a warning, so a typo never stops a run.

```yaml
mode: pr            # pr | issue   (issue: never push code; the diff goes in the issue)
packages: [openai]  # subset of the watched SDKs; default: all
api_changes: pr     # pr | issue | off
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from guardian.common.log import get_logger

log = get_logger(__name__)

CONFIG_PATH = ".github/guardian.yml"
MODES = ("pr", "issue")
API_MODES = ("pr", "issue", "off")


@dataclass(frozen=True)
class RepoConfig:
    mode: str = "pr"
    packages: frozenset[str] | None = None  # None: every watched package
    api_changes: str = "pr"  # decision 2026-11-25: draft PRs labelled a behaviour change
    warnings: tuple[str, ...] = field(default=())

    def wants(self, package: str) -> bool:
        return self.packages is None or package in self.packages


def parse_config(text: str | None) -> RepoConfig:
    if text is None:
        return RepoConfig()
    warnings: list[str] = []
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return RepoConfig(warnings=(f"{CONFIG_PATH} is not valid YAML: {exc}",))
    if not isinstance(data, dict):
        return RepoConfig(warnings=(f"{CONFIG_PATH} must be a mapping",))
    mode = str(data.get("mode", "pr"))
    if mode not in MODES:
        warnings.append(f"mode {mode!r} is not one of {MODES}; using pr")
        mode = "pr"
    raw_api = data.get("api_changes", "pr")
    # YAML 1.1 reads a bare `off` as false (and `on` as true).
    api = "off" if raw_api is False else str(raw_api)
    if api not in API_MODES:
        warnings.append(f"api_changes {api!r} is not one of {API_MODES}; using pr")
        api = "pr"
    packages = data.get("packages")
    chosen: frozenset[str] | None = None
    if packages is not None:
        if isinstance(packages, list) and all(isinstance(p, str) for p in packages):
            chosen = frozenset(p.lower() for p in packages)
        else:
            warnings.append("packages must be a list of names; using all")
    for key in data:
        if key not in ("mode", "packages", "api_changes"):
            warnings.append(f"unknown key {key!r} ignored")
    for warning in warnings:
        log.warning("repo config", extra={"fields": {"warning": warning}})
    return RepoConfig(mode, chosen, api, tuple(warnings))


def read_config(checkout: Path) -> RepoConfig:
    path = checkout / CONFIG_PATH
    if not path.is_file():
        return RepoConfig()
    return parse_config(path.read_bytes().decode("utf-8", errors="replace"))
