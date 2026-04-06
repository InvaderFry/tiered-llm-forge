#!/bin/bash
set -euo pipefail
# lib/configs.sh — Install tool config files into the new project

install_configs() {
    cp "$FORGE_DIR/templates/.aider.conf.yml"  "$PROJECT_DIR/.aider.conf.yml"
    cp "$FORGE_DIR/templates/models.yaml"      "$PROJECT_DIR/models.yaml"
    echo "✔  .aider.conf.yml created"
    echo "✔  models.yaml created"
}
