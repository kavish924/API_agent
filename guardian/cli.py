"""`guardian` command line (spec 00 F0.10).

The whole pipeline runs from here with no GitHub App and no cloud resources.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from guardian import pipeline
from guardian.common import db
from guardian.common.config import get_settings
from guardian.common.context import AppContext, build_context
from guardian.common.log import configure_logging
from guardian.pipeline.types import NOT_IMPLEMENTED, RunRequest

app = typer.Typer(
    help="API Change Guardian: applies provider changes to the code that depends on them.",
    no_args_is_help=True,
    add_completion=False,
)


def _context() -> AppContext:
    settings = get_settings()
    configure_logging(settings.log_level)
    return build_context(settings)


@app.command()
def poll() -> None:
    """Check every change source and start runs for changes that pass the release gate."""
    result = pipeline.poll(_context())
    typer.echo(result.model_dump_json())


@app.command()
def run(
    repo: Annotated[Path, typer.Option("--repo", help="Path to a local checkout to scan.")],
    package: Annotated[str, typer.Option("--package", help="Watched package, e.g. openai.")],
    from_version: Annotated[
        str | None,
        typer.Option("--from", help="Old version. Default: the version the repo pins."),
    ] = None,
    to_version: Annotated[
        str | None,
        typer.Option("--to", help="New version. Default: the latest release."),
    ] = None,
) -> None:
    """Run the pipeline once for a local repository and one package."""
    request = RunRequest(
        repo=str(repo),
        target=f"{package}@{to_version or 'latest'}",
        package=package,
        from_version=from_version,
        to_version=to_version,
    )
    result = pipeline.run(request, _context())
    typer.echo(result.model_dump_json())


@app.command()
def ingest(
    source: Annotated[str, typer.Option("--source", help="API-page source ID.")],
    file: Annotated[
        Path,
        typer.Option("--file", exists=True, dir_okay=False, help="Saved copy of the page."),
    ],
) -> None:
    """Store a saved copy of a provider page as a snapshot (spec 10)."""
    _context()
    typer.echo(f'{{"outcome":"{NOT_IMPLEMENTED}","source":"{source}","file":"{file.name}"}}')


@app.command()
def migrate() -> None:
    """Create or upgrade the database schema."""
    url = get_settings().database_url
    db.upgrade(url)
    typer.echo("Database is up to date.")


if __name__ == "__main__":
    app()
