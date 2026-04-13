"""Tests for lightweight CLI helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.__main__ import _blocked_dependencies


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
