"""Tests for the integration gate helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

import orchestrator.integration as integration


def test_gemini_integration_fix_accepts_router_attempt_metadata(monkeypatch):
    calls = []

    monkeypatch.setattr(integration, "get_tier", lambda name: {"models": ["gemini/gemini-2.5-flash"]})
    monkeypatch.setattr(integration, "all_gemini_quota_exhausted", lambda: False)
    monkeypatch.setattr(integration, "run_full_suite", lambda tests_dir: (True, "passed"))

    def fake_run_with_tier_fallback(tier_name, message, target_file, read_files=None):
        calls.append((tier_name, target_file, read_files, message))
        return (
            True,
            "gemini/gemini-2.5-flash",
            {"tokens_sent": 10, "tokens_received": 5},
            [{"model": "gemini/gemini-2.5-flash", "success": True, "reason": "ok"}],
        )

    monkeypatch.setattr(integration, "run_with_tier_fallback", fake_run_with_tier_fallback)

    assert integration._attempt_gemini_integration_fix(
        "FAILED tests/test_app.py::test_app\nsrc/app.py:12: AssertionError",
        ["task-001-app"],
    ) is True

    assert calls
    assert calls[0][0] == "gemini"
    assert calls[0][1] == "src/app.py"
