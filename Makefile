# Convenience targets. On Windows without make, run the commands directly:
#   python scripts/dev.py
#   .venv\Scripts\python -m pytest

VENV_PY := $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,.venv/bin/python)

.PHONY: dev test lint types check migrate

dev:
	python scripts/dev.py

test:
	$(VENV_PY) -m pytest

lint:
	$(VENV_PY) -m ruff check .
	$(VENV_PY) -m ruff format --check .

types:
	$(VENV_PY) -m mypy

check: lint types test

migrate:
	$(VENV_PY) -m guardian.cli migrate
