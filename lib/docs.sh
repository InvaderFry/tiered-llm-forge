#!/bin/bash
set -euo pipefail
# lib/docs.sh — Install documentation files into the new project
# ORCHESTRATION.md has {{PROJECT_NAME}} substituted at install time.

install_docs() {
    sed "s/{{PROJECT_NAME}}/$PROJECT_NAME/g" \
        "$FORGE_DIR/templates/ORCHESTRATION.md" > "$PROJECT_DIR/ORCHESTRATION.md"
    cp "$FORGE_DIR/templates/CLAUDE.md" "$PROJECT_DIR/CLAUDE.md"
    cp -r "$FORGE_DIR/templates/docs"   "$PROJECT_DIR/docs"
    echo "✔  ORCHESTRATION.md created"
    echo "✔  CLAUDE.md created"
    echo "✔  docs/ reference guides created"
}
