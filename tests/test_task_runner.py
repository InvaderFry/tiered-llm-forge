"""Tests for orchestrator.task_runner helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.task_runner import _compute_resume_starts


class TestComputeResumeStarts:
    def test_fresh_run_returns_defaults(self):
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(None)
        assert skip is False
        assert p_start == 1
        assert e_start == 1
        assert skip_gemini is False

    def test_primary_exhausted_skip_to_escalation(self):
        # last attempt was primary#3 (last attempt of a 3-retry tier)
        rp = {"tier": "primary", "attempt": 3, "total_attempts": 3}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is False
        assert p_start == 4   # one past the last attempt (will be >= retries+1, so loop is skipped)
        assert e_start == 1
        assert skip_gemini is False

    def test_mid_primary_resumes_at_next_attempt(self):
        rp = {"tier": "primary", "attempt": 2, "total_attempts": 2}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is False
        assert p_start == 3
        assert e_start == 1
        assert skip_gemini is False

    def test_escalation_tier_skips_primary(self):
        # Any resume from escalation should skip the primary loop entirely.
        rp = {"tier": "escalation", "attempt": 1, "total_attempts": 4}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert e_start == 2
        assert skip_gemini is False

    def test_escalation_mid_tier_resumes_at_next(self):
        # Crashed mid-escalation at attempt 2 → next run starts at attempt 3.
        rp = {"tier": "escalation", "attempt": 2, "total_attempts": 5}
        skip, _, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert e_start == 3
        assert skip_gemini is False

    def test_gemini_tier_skips_all_and_sets_skip_gemini(self):
        # If last attempt was in the gemini tier, all tiers are skipped
        # so --resume falls straight to Stage 3 (Claude review).
        rp = {"tier": "gemini", "attempt": 1, "total_attempts": 6}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert p_start == 1
        assert e_start == 1
        assert skip_gemini is True
