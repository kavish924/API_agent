"""Database schema (spec 00, "Data model"). One layer for SQLite and Postgres."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint names keep migrations identical across SQLite and Postgres.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repo(Base):
    """A repository the GitHub App is installed on."""

    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("installation_id", "full_name", name="uq_repos_installation_full_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Source(Base):
    """A watched change source: an SDK's releases or a provider page."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # sdk-release | api-page
    provider: Mapped[str] = mapped_column(String(100))
    package: Mapped[str | None] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(255))
    last_seen: Mapped[str | None] = mapped_column(String(255))  # version or content hash


class SourceDocument(Base):
    """A raw release note or page snapshot. An edit is a new revision, same key."""

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("source_id", "version", "revision", name="uq_source_documents_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    version: Mapped[str] = mapped_column(String(100))  # SDK version or snapshot label
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    blob_ref: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChangeRecord(Base):
    """One change, extracted from a document or found by the API diff."""

    __tablename__ = "change_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    layer: Mapped[str] = mapped_column(String(10))  # sdk | api
    origin: Mapped[str] = mapped_column(String(10))  # notes | page | diff
    package: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[str | None] = mapped_column(String(100))
    change_type: Mapped[str] = mapped_column(String(40))
    qualified_symbol: Mapped[str | None] = mapped_column(String(500))
    identifier: Mapped[str | None] = mapped_column(String(255))
    affected_parameter: Mapped[str | None] = mapped_column(String(255))
    replacement: Mapped[str | None] = mapped_column(String(500))
    fixable: Mapped[bool] = mapped_column(default=False)
    shutdown_date: Mapped[date | None] = mapped_column(Date)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    migration_hint: Mapped[str | None] = mapped_column(Text)


class Run(Base):
    """One pipeline run for one repo and one target. The key makes redelivery a no-op."""

    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("repo_id", "target", "head_sha", name="uq_runs_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    target: Mapped[str] = mapped_column(String(255))  # e.g. openai@1.45.0
    head_sha: Mapped[str] = mapped_column(String(64))
    from_version: Mapped[str | None] = mapped_column(String(100))
    to_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="started")
    outcome: Mapped[str | None] = mapped_column(String(40))
    verification_level: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Finding(Base):
    """A call site in a repo that a change record affects."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    change_record_id: Mapped[int] = mapped_column(ForeignKey("change_records.id"))
    file: Mapped[str] = mapped_column(String(1000))
    line: Mapped[int] = mapped_column(Integer)
    matched_symbol: Mapped[str] = mapped_column(String(500))
    match_tier: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[str] = mapped_column(String(10))  # high | medium
    fix_tier: Mapped[str | None] = mapped_column(String(20))
    fix_status: Mapped[str | None] = mapped_column(String(20))


class PullRequest(Base):
    """The PR or issue for one repo and one package or source, with closed-PR memory."""

    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "subject", name="uq_pull_requests_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    subject: Mapped[str] = mapped_column(String(255))  # package or source ID
    kind: Mapped[str] = mapped_column(String(10), default="pr")  # pr | issue
    number: Mapped[int | None] = mapped_column(Integer)
    branch: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20), default="open")
    covered_change_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    declined_change_ids: Mapped[list[int]] = mapped_column(JSON, default=list)


class LLMCall(Base):
    """Every LLM call, logged by the shared client (spec 00 F0.6)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    purpose: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any]] = mapped_column(JSON)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentStep(Base):
    """One step of an agent fixer session (spec 11 F11.17)."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "finding_id", "step_number", name="uq_agent_steps_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"))
    step_number: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(50))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)


TABLE_NAMES = frozenset(Base.metadata.tables)
