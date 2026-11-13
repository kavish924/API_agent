"""Migrations and schema. Covers F0.3, F0.4."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError

from guardian.common import db
from guardian.common.models import TABLE_NAMES, Base, ChangeRecord, Finding, Repo, Run

EXPECTED_TABLES = {
    "repos",
    "sources",
    "source_documents",
    "change_records",
    "runs",
    "findings",
    "pull_requests",
    "llm_calls",
    "agent_steps",
}


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_models_define_the_nine_tables() -> None:
    assert TABLE_NAMES == EXPECTED_TABLES


def test_migrations_create_all_tables(engine: Engine) -> None:
    assert _tables(engine) == EXPECTED_TABLES


def test_migrations_are_idempotent(migrated_url: str, engine: Engine) -> None:
    db.upgrade(migrated_url)  # second run must be a no-op
    assert _tables(engine) == EXPECTED_TABLES


def test_migrations_match_the_models(engine: Engine) -> None:
    """Every model column exists in the migrated database, and nothing extra."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        migrated = {column["name"] for column in inspector.get_columns(table.name)}
        assert migrated == {column.name for column in table.columns}, table.name


def test_downgrade_removes_everything(migrated_url: str, engine: Engine) -> None:
    db.downgrade(migrated_url)
    assert _tables(engine) == set()


def test_rows_round_trip_with_json_columns(sessions: db.SessionFactory) -> None:
    with db.session_scope(sessions) as session:
        repo = Repo(installation_id=9_000_000_001, full_name="octo/app")
        session.add(repo)
        session.flush()
        run = Run(repo_id=repo.id, target="openai@1.45.0", head_sha="abc123")
        record = ChangeRecord(layer="sdk", origin="notes", change_type="renamed", fixable=True)
        session.add_all([run, record])
        session.flush()
        session.add(
            Finding(
                run_id=run.id,
                change_record_id=record.id,
                file="app/llm.py",
                line=12,
                matched_symbol="openai.OpenAI.chat",
                match_tier="direct",
                confidence="high",
            )
        )

    with db.session_scope(sessions) as session:
        stored = session.scalars(select(Run)).one()
        assert stored.status == "started"
        assert session.scalars(select(Repo)).one().active is True
        assert session.scalars(select(Finding)).one().line == 12


def test_run_key_is_unique(sessions: db.SessionFactory) -> None:
    """(repo, target, head SHA) is what makes a redelivered job a no-op."""
    with db.session_scope(sessions) as session:
        repo = Repo(installation_id=1, full_name="octo/app")
        session.add(repo)
        session.flush()
        session.add(Run(repo_id=repo.id, target="openai@1.45.0", head_sha="abc"))
        repo_id = repo.id

    with pytest.raises(IntegrityError), db.session_scope(sessions) as session:
        session.add(Run(repo_id=repo_id, target="openai@1.45.0", head_sha="abc"))


def test_foreign_keys_are_enforced(sessions: db.SessionFactory) -> None:
    with pytest.raises(IntegrityError), db.session_scope(sessions) as session:
        session.add(Run(repo_id=424242, target="openai@1.45.0", head_sha="abc"))
