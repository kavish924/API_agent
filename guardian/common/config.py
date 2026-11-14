"""Configuration, loaded from environment variables (and `.env` locally).

Secrets are never hardcoded. Secret values use `SecretStr` so they do not appear in
logs or reprs.
"""

from __future__ import annotations

from pathlib import Path

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

    # Global pause flag (spec 00 F0.12): checked before any write to GitHub.
    paused: bool = False

    log_level: str = "INFO"

    llm_provider: str = "none"
    llm_model: str = ""

    # Optional token for reading public release data. Without it GitHub allows 60
    # requests an hour, which is enough for local use.
    github_token: SecretStr | None = None

    github_app_id: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None


def get_settings() -> Settings:
    """Build settings from the current environment. Cheap, so it is not cached."""
    return Settings()
