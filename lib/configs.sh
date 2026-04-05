#!/bin/bash
# lib/configs.sh — Install tool config files into the new project

install_configs() {
    cp "$FORGE_DIR/templates/.aider.conf.yml"    "$PROJECT_DIR/.aider.conf.yml"
    cp "$FORGE_DIR/templates/litellm-config.yaml" "$PROJECT_DIR/litellm-config.yaml"
    echo "✔  .aider.conf.yml created"
    echo "✔  litellm-config.yaml created"
}
