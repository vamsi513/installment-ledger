#!/usr/bin/env bash
# Build and test the project from a clean export of HEAD, in a throwaway
# virtualenv, using the pinned dependencies and the pinned ruff. This is the
# same sequence the CI workflow runs, on the same Python and the same tool
# versions, so a pass here and a pass in CI mean the same thing.
#
# Needs a reachable Postgres. With the local compose service running, the
# default DATABASE_URL in app/config.py already points at it.
set -euo pipefail

PYTHON="${PYTHON:-python3.12}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
EXPORT_DIR="$(mktemp -d)"
trap 'rm -rf "$EXPORT_DIR"' EXIT

echo "== exporting HEAD to $EXPORT_DIR"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$EXPORT_DIR"
cd "$EXPORT_DIR"

echo "== creating a fresh virtualenv"
"$PYTHON" -m venv .venv
.venv/bin/python --version

echo "== installing pinned dependencies"
.venv/bin/python -m pip install --quiet --no-cache-dir -r requirements.txt

echo "== ruff"
.venv/bin/ruff --version
.venv/bin/ruff check .
.venv/bin/ruff format --check .

echo "== migration, both directions"
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head

echo "== pytest"
.venv/bin/python -m pytest
