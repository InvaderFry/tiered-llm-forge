#!/bin/bash
# lib/docs.sh — Install documentation files into the new project
# README.md has {{PROJECT_NAME}} substituted at install time.

install_docs() {
    sed "s/{{PROJECT_NAME}}/$PROJECT_NAME/g" \
        "$FORGE_DIR/templates/README.md" > "$PROJECT_DIR/README.md"
    cp "$FORGE_DIR/templates/CLAUDE.md" "$PROJECT_DIR/CLAUDE.md"
    echo "✔  README.md created"
    echo "✔  CLAUDE.md created"
}
