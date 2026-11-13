"""One-command local setup: create a virtualenv, install the project, migrate the database.

Run with the system Python:  python scripts/dev.py
Works the same on Windows, macOS and Linux, and needs no make, uv or Docker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    # Runs on the system Python, before the venv exists, so this check is not redundant.
    if sys.version_info < (3, 11):  # noqa: UP036
        raise SystemExit("Python 3.11 or newer is required.")
    if not venv_python().exists():
        run(sys.executable, "-m", "venv", str(VENV))
    py = str(venv_python())
    run(py, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(py, "-m", "pip", "install", "--quiet", "-e", ".[dev]")
    run(py, "-m", "guardian.cli", "migrate")
    print("\nReady. Next:")
    print(f"  {py} -m pytest")
    print(f"  {py} -m guardian.cli --help")


if __name__ == "__main__":
    main()
