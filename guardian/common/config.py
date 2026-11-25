"""Configuration, loaded from environment variables (and `.env` locally).

Secrets are never hardcoded. Secret values use `SecretStr` so they do not appear in
logs or reprs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUARDIAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # `DATABASE_URL` is the conventional name, so it is accepted without the prefix.
    database_url: str = Field(
        default="sqlite:///guardian.db",
        validation_alias=AliasChoices("DATABASE_URL", "GUARDIAN_DATABASE_URL"),
    )
    blob_dir: Path = Path(".guardian/blobs")
    # Local scratch space: unpacked SDK wheels for the agent's type checker.
    work_dir: Path = Path(".guardian/work")

    # Global pause flag (spec 00 F0.12): checked before any write to GitHub.
    paused: bool = False

    log_level: str = "INFO"

    llm_provider: str = "none"
    llm_model: str = ""

    # Tier 2 fixer (spec 05 F5.5): `agent` (spec 11) or `single-shot`.
    fix_mode: Literal["agent", "single-shot"] = "agent"
    # Changed lines allowed per finding in a model-written diff (F5.9).
    fix_line_cap: int = 40

    # Agent fixer budgets (spec 11 F11.15, F11.16).
    agent_max_steps: int = 12
    agent_max_proposals: int = 3
    agent_max_tokens: int = 60_000
    agent_max_seconds: float = 120.0
    agent_run_cost_cap: float = 1.0

    # Optional token for reading public release data. Without it GitHub allows 60
    # requests an hour, which is enough for local use.
    github_token: SecretStr | None = None

    github_app_id: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None


def get_settings() -> Settings:
    """Build settings from the current environment. Cheap, so it is not cached."""
    return Settings()
