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


def _basic_main_spec(tmp_path, task_name="task-001-example"):
    spec_path = tmp_path / "specs" / f"{task_name}.md"
    spec_path.parent.mkdir(exist_ok=True)
    spec_path.write_text("---\n", encoding="utf-8")
    return {
        "task_name": task_name,
        "path": Path(f"specs/{task_name}.md"),
        "target": "src/example.py",
        "test": "tests/test_001_example.py",
        "dependencies": [],
        "raw_text": "---\n",
    }


def _patch_main_basics(monkeypatch, tmp_path, groups):
    monkeypatch.setattr(cli, "setup_logging", lambda verbose=False: None)
    monkeypatch.setattr(cli, "load_config", lambda path=None: {})
    monkeypatch.setattr(cli, "ensure_default_branch_exists", lambda: None)
    monkeypatch.setattr(cli, "get_default_branch", lambda: "main")
    monkeypatch.setattr(cli, "SPECS_DIR", tmp_path / "specs")
    monkeypatch.setattr(cli, "topological_sort", lambda files: list(files))
    monkeypatch.setattr(cli, "get_config", lambda: {"cooldown_seconds": 30})
    monkeypatch.setattr(cli, "get_auto_parallel", lambda: False)
    monkeypatch.setattr(cli, "run_startup_preflight", lambda repo_root=None: ([], []))
    monkeypatch.setattr(cli, "validate_tracked_clean", lambda paths: [])
    monkeypatch.setattr(cli, "load_state", lambda: {})
    monkeypatch.setattr(cli, "save_state", lambda state: None)
    monkeypatch.setattr(cli, "append_run_summary", lambda state, results: None)
    monkeypatch.setattr(cli, "print_summary", lambda results, default_branch, state: None)
    monkeypatch.setattr(cli, "integration_gate", lambda all_task_names, default_branch, state: None)
    monkeypatch.setattr(cli, "reset_run_time_breakdown", lambda: None)
    monkeypatch.setattr(cli, "find_parallel_groups", lambda ordered_specs: groups)


def test_main_runs_tasks_after_tracked_clean_preflight(monkeypatch, tmp_path):
    spec = _basic_main_spec(tmp_path)
    _patch_main_basics(monkeypatch, tmp_path, [[spec]])
    monkeypatch.setattr(sys, "argv", ["orchestrator"])
    monkeypatch.setattr(cli, "load_spec", lambda _: spec)

    call_order = []
    monkeypatch.setattr(cli, "validate_tracked_clean", lambda paths: call_order.append("tracked") or [])
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: call_order.append("run") or "passed",
    )

    cli.main()

    assert call_order == ["tracked", "run"]


def test_main_auto_parallel_switches_to_wave_mode(monkeypatch, tmp_path, caplog):
    spec_a = _basic_main_spec(tmp_path, "task-001-example")
    spec_b = _basic_main_spec(tmp_path, "task-002-example")
    groups = [[spec_a, spec_b]]
    _patch_main_basics(monkeypatch, tmp_path, groups)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "--auto-parallel"])
    specs_iter = iter([spec_a, spec_b])
    monkeypatch.setattr(cli, "load_spec", lambda _: next(specs_iter))
    monkeypatch.setattr(
        cli,
        "run_parallel_group",
        lambda *args, **kwargs: {
            "task-001-example": "passed",
            "task-002-example": "passed",
        },
    )
    monkeypatch.setattr(cli, "run_task", lambda *args, **kwargs: pytest.fail("sequential path should not run"))

    with caplog.at_level("INFO"):
        cli.main()

    assert "Auto-parallel: switching to parallel mode" in caplog.text


def test_main_no_auto_parallel_flag_overrides_config(monkeypatch, tmp_path):
    spec_a = _basic_main_spec(tmp_path, "task-001-example")
    spec_b = _basic_main_spec(tmp_path, "task-002-example")
    groups = [[spec_a, spec_b]]
    _patch_main_basics(monkeypatch, tmp_path, groups)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "--no-auto-parallel"])
    monkeypatch.setattr(cli, "get_auto_parallel", lambda: True)
    specs_iter = iter([spec_a, spec_b])
    monkeypatch.setattr(cli, "load_spec", lambda _: next(specs_iter))
    monkeypatch.setattr(cli, "run_parallel_group", lambda *args, **kwargs: pytest.fail("parallel path should not run"))

    calls = []
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda spec, *args, **kwargs: calls.append(spec["task_name"]) or "passed",
    )

    cli.main()

    assert calls == ["task-001-example", "task-002-example"]
