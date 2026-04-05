#!/bin/bash
# lib/dotfiles.sh — Install .env and .gitignore into the new project

install_dotfiles() {
    cp "$FORGE_DIR/templates/.env.example" "$PROJECT_DIR/.env"
    cp "$FORGE_DIR/templates/.gitignore"   "$PROJECT_DIR/.gitignore"
    echo "✔  .env created (fill in your keys before running the pipeline)"
    echo "✔  .gitignore created"
}
