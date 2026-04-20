"""Tests for orchestrator.summary."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

import orchestrator.summary as summary


def test_print_summary_uses_only_current_run_entries(monkeypatch, caplog):
    monkeypatch.setattr(summary, "get_run_time_breakdown", lambda: {})
    state = {
        "tasks": {
            "task-current": {"duration_seconds": 1.0, "failure_class": "assertion"},
            "task-old": {"duration_seconds": 99.0, "failure_class": "request_too_large"},
        },
        "runs": [
            {"passed": [], "failed": ["task-old"], "skipped": [], "blocked": []},
            {"passed": [], "failed": ["task-current"], "skipped": [], "blocked": []},
        ],
    }
    results = {"passed": [], "failed": ["task-current"], "skipped": [], "blocked": []}

    with caplog.at_level("INFO"):
        summary.print_summary(results, "main", state)

    assert "Total task time: 1.0s" in caplog.text
    assert "request_too_large" not in caplog.text


def test_print_summary_uses_make_run_and_shows_cumulative_section(monkeypatch, caplog):
    monkeypatch.setattr(summary, "get_run_time_breakdown", lambda: {})
    state = {
        "tasks": {"task-current": {"duration_seconds": 1.0, "failure_class": "assertion"}},
        "runs": [
            {"passed": ["task-old"], "failed": [], "skipped": [], "blocked": []},
            {"passed": [], "failed": ["task-current"], "skipped": [], "blocked": []},
        ],
    }
    results = {"passed": [], "failed": ["task-current"], "skipped": [], "blocked": []}

    with caplog.at_level("INFO"):
        summary.print_summary(results, "main", state)

    assert "After Claude fixes each failure, re-run: make run" in caplog.text
    assert "Use make resume only when the previous run was interrupted" in caplog.text
    assert "Cumulative session:" in caplog.text
