#!/bin/bash
set -euo pipefail
# lib/structure.sh — Create project directory layout and initialize git

setup_structure() {
    mkdir -p "$PROJECT_DIR"/{specs,tests,src}
    cd "$PROJECT_DIR"
    git init
    echo "✔  Folder structure created"
}
