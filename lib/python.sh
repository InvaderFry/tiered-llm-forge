#!/bin/bash
# lib/python.sh — Install Python files into the new project

install_python() {
    cp "$FORGE_DIR/templates/orchestrator.py"    "$PROJECT_DIR/orchestrator.py"
    cp "$FORGE_DIR/templates/src/__init__.py"    "$PROJECT_DIR/src/__init__.py"
    cp "$FORGE_DIR/templates/tests/conftest.py"  "$PROJECT_DIR/tests/conftest.py"
    echo "✔  orchestrator.py created"
    echo "✔  src/__init__.py created"
    echo "✔  tests/conftest.py created"
}
