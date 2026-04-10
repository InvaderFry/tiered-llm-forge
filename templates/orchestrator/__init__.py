"""Tiered LLM orchestrator — coordinates cheap model code generation."""

from pathlib import Path

SPECS_DIR = Path(__file__).parent.parent / "specs"
FORGE_LOGS_DIR = Path(__file__).parent.parent / "forgeLogs"
