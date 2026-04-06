#!/bin/bash
set -euo pipefail
# lib/python.sh — Install Python files into the new project

install_python() {
    cp -r "$FORGE_DIR/templates/orchestrator"   "$PROJECT_DIR/orchestrator"
    cp "$FORGE_DIR/templates/requirements.txt"   "$PROJECT_DIR/requirements.txt"
    cp "$FORGE_DIR/templates/Makefile"           "$PROJECT_DIR/Makefile"
    cp "$FORGE_DIR/templates/src/__init__.py"    "$PROJECT_DIR/src/__init__.py"
    cp "$FORGE_DIR/templates/tests/conftest.py"  "$PROJECT_DIR/tests/conftest.py"
    echo "✔  orchestrator/ package created"
    echo "✔  requirements.txt created"
    echo "✔  Makefile created"
    echo "✔  src/__init__.py created"
    echo "✔  tests/conftest.py created"

    echo "→  Creating virtual environment (.venv)…"
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
    "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
    echo "✔  .venv created and dependencies installed"
}
