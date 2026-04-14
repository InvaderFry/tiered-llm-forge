"""Tests for lightweight CLI helpers."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

import orchestrator.__main__ as cli
from orchestrator.__main__ import _blocked_dependencies, _cooldown_duration, _should_cooldown


def test_blocked_dependencies_detects_failed_and_blocked_upstream():
    spec = {"dependencies": ["task-001", "task-002", "task-003"]}
    outcomes = {
        "task-001": "passed",
        "task-002": "failed",
        "task-003": "blocked",
    }
    assert _blocked_dependencies(spec, outcomes) == ["task-002", "task-003"]


def test_blocked_dependencies_ignores_passing_or_unknown_upstream():
    spec = {"dependencies": ["task-001", "task-002"]}
    outcomes = {"task-001": "passed"}
    assert _blocked_dependencies(spec, outcomes) == []


@pytest.fixture
def dry_run_spec(monkeypatch, tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    spec_path = specs_dir / "task-001-example.md"
    spec_path.write_text("---\n", encoding="utf-8")

    monkeypatch.setattr(cli, "SPECS_DIR", specs_dir)
    monkeypatch.setattr(cli, "validate_specs", lambda _: ([], []))
    monkeypatch.setattr(cli, "topological_sort", lambda files: list(files))
    monkeypatch.setattr(
        cli,
        "load_spec",
        lambda _: {
            "task_name": "task-001-example",
            "path": Path("specs/task-001-example.md"),
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "dependencies": [],
            "raw_text": "---\n",
        },
    )
    monkeypatch.setattr(cli, "ensure_default_branch_exists", lambda: None)
    monkeypatch.setattr(cli, "get_default_branch", lambda: "main")
    monkeypatch.setattr(cli, "get_config", lambda: {"cooldown_seconds": 30})
    monkeypatch.setattr(cli, "get_tier", lambda name: {"models": [f"{name}-model"], "retries": 1})
    monkeypatch.setattr(cli, "branch_exists", lambda _: False)
    monkeypatch.setattr(cli, "run_startup_preflight", lambda repo_root=None: ([], []))

    return spec_path


def test_cmd_dry_run_fails_when_specs_or_tests_are_untracked(monkeypatch, dry_run_spec, caplog):
    monkeypatch.setattr(
        cli,
        "validate_tracked_clean",
        lambda _: ["specs/task-001-example.md is not tracked by git."],
    )

    with caplog.at_level("ERROR"):
        result = cli.cmd_dry_run()

    assert result == 1
    assert "Cannot dry-run: task specs/tests must be committed and clean before orchestration." in caplog.text
    assert "git add specs tests" in caplog.text
    assert 'git commit -m "chore: add task specs and tests"' in caplog.text


def test_cmd_dry_run_fails_when_specs_or_tests_are_dirty(monkeypatch, dry_run_spec, caplog):
    monkeypatch.setattr(
        cli,
        "validate_tracked_clean",
        lambda _: ["tests/test_001_example.py has uncommitted changes."],
    )

    with caplog.at_level("ERROR"):
        result = cli.cmd_dry_run()

    assert result == 1
    assert "tests/test_001_example.py has uncommitted changes." in caplog.text
    assert "Commit planner inputs before retrying:" in caplog.text


def test_cmd_dry_run_fails_when_startup_preflight_fails(monkeypatch, dry_run_spec, caplog):
    monkeypatch.setattr(cli, "validate_tracked_clean", lambda _: [])
    monkeypatch.setattr(cli, "run_startup_preflight", lambda repo_root=None: (["missing aider"], []))

    with caplog.at_level("ERROR"):
        result = cli.cmd_dry_run()

    assert result == 1
    assert "Preflight error: missing aider" in caplog.text
    assert "Cannot dry-run until preflight errors are fixed." in caplog.text


def test_cmd_preflight_reports_errors(monkeypatch, caplog):
    monkeypatch.setattr(cli, "run_startup_preflight", lambda repo_root=None: (["bad model"], ["legacy alias"]))

    with caplog.at_level("WARNING"):
        result = cli.cmd_preflight()

    assert result == 1
    assert "Preflight warning: legacy alias" in caplog.text
    assert "Preflight error: bad model" in caplog.text


def test_should_cooldown_uses_adaptive_provider_pressure(monkeypatch):
    monkeypatch.setattr(cli, "adaptive_cooldown_seconds", lambda cooldown: 12.5 if cooldown == 30 else 0.0)
    assert _cooldown_duration(30) == 12.5
    assert _should_cooldown(["skipped", "blocked"], 30) is True


def test_should_cooldown_skips_when_no_provider_pressure(monkeypatch):
    monkeypatch.setattr(cli, "adaptive_cooldown_seconds", lambda cooldown: 0.0)
    assert _cooldown_duration(30) == 0.0
    assert _should_cooldown(["passed"], 30) is False
