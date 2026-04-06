#!/bin/bash
# new-project.sh — Bootstrap a new tiered LLM coding workflow project
# Run from anywhere: bash /path/to/tiered-llm-forge/new-project.sh
# Creates: ~/projects/my-project-YYYYMMDD-HHMMSS/

set -euo pipefail

cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo "ERROR: Project creation failed (exit code $exit_code)."
        if [ -d "${PROJECT_DIR:-}" ]; then
            echo "Partial project left at: $PROJECT_DIR"
            echo "You may want to remove it: rm -rf $PROJECT_DIR"
        fi
    fi
}
trap cleanup EXIT

FORGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
PROJECT_NAME="my-project-$TIMESTAMP"
PROJECT_DIR="$HOME/projects/$PROJECT_NAME"

source "$FORGE_DIR/lib/structure.sh"
source "$FORGE_DIR/lib/dotfiles.sh"
source "$FORGE_DIR/lib/configs.sh"
source "$FORGE_DIR/lib/python.sh"
source "$FORGE_DIR/lib/docs.sh"

echo ""
echo "=================================================="
echo "  Tiered LLM Workflow — New Project Bootstrap"
echo "  Project: $PROJECT_NAME"
echo "=================================================="
echo ""

setup_structure
install_dotfiles
install_configs
install_python
install_docs

# ─── Initial git commit ───────────────────────────────────────────────────────
git add .
git commit -m "chore: project scaffold"

echo ""
echo "=================================================="
echo "  Project ready: $PROJECT_DIR"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. cd $PROJECT_DIR"
echo "  2. source .venv/bin/activate"
echo "  3. Edit .env and add your GROQ_API_KEY"
echo "  4. Open Claude Code: claude"
echo "  5. See ORCHESTRATION.md for the full workflow"
echo ""
