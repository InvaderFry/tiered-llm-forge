"""Tests for the orchestrator's model_router module."""

import sys
import os
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.model_router import (
    adaptive_cooldown_seconds,
    effective_input_cap,
    get_fallback_models,
    _parse_retry_after,
    _parse_usage,
    _record_observed_cap,
    _is_invalid_model_error,
    _wait_for_model,
    _mark_rate_limited,
    _mark_request_too_large,
    has_pending_rate_limits,
    is_request_too_large,
    is_invalid_model,
    mark_invalid_model,
    clear_request_too_large,
)
import orchestrator.model_router as _mr
from orchestrator import config as config_mod


@pytest.fixture(autouse=True)
def mock_config(tmp_path):
    config_yaml = tmp_path / "models.yaml"
    config_yaml.write_text(textwrap.dedent("""\
        tiers:
          - name: primary
            models:
              - groq/openai/gpt-oss-20b
            retries: 3
          - name: escalation
            models:
              - groq/openai/gpt-oss-120b
              - groq/llama-3.3-70b-versatile
            retries: 1
          - name: gemini
            models:
              - gemini/gemini-2.5-flash
            retries: 1
        weak_model: groq/llama-3.1-8b-instant
        spec_limits:
          soft_limit_chars: 12000
          hard_limit_chars: 16000
        cooldown_seconds: 0
    """))
    config_mod._config = None
    config_mod.load_config(config_yaml)
    yield
    config_mod._config = None


class TestGetFallbackModels:
    def test_returns_remaining_after_current(self):
        fallbacks = get_fallback_models("groq/openai/gpt-oss-120b", "escalation")
        assert fallbacks == ["groq/llama-3.3-70b-versatile"]

    def test_returns_all_if_model_not_in_tier(self):
        fallbacks = get_fallback_models("unknown/model", "escalation")
        assert fallbacks == [
            "groq/openai/gpt-oss-120b",
            "groq/llama-3.3-70b-versatile",
        ]

    def test_last_model_returns_empty(self):
        fallbacks = get_fallback_models(
            "groq/llama-3.3-70b-versatile", "escalation"
        )
        assert fallbacks == []


class TestRateLimitCoordinator:
    """Tests for the per-model rate-limit coordinator.

    Time and sleep are injected via module-level _clock / _sleep so
    tests never actually block.
    """

    def setup_method(self):
        _mr._next_available_at.clear()
        _mr._invalid_models.clear()
        _mr._observed_caps.clear()
        _mr._last_provider_pressure_at = 0.0
        clear_request_too_large()
        self._fake_now = [1000.0]
        self._slept = []
        _mr._clock = lambda: self._fake_now[0]
        _mr._sleep = lambda s: self._slept.append(s)

    def teardown_method(self):
        import time as _t
        _mr._clock = _t.time
        _mr._sleep = _t.sleep
        _mr._next_available_at.clear()
        _mr._invalid_models.clear()
        _mr._observed_caps.clear()
        _mr._last_provider_pressure_at = 0.0
        clear_request_too_large()

    def test_wait_sleeps_remaining_window(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 12.0, buffer=5.0)  # earliest = 1017
        _wait_for_model("groq/openai/gpt-oss-20b")
        assert len(self._slept) == 1
        assert abs(self._slept[0] - 17.0) < 0.01

    def test_no_wait_when_window_passed(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 12.0)
        self._fake_now[0] += 100          # clock advances past the window
        _wait_for_model("groq/openai/gpt-oss-20b")
        assert self._slept == []

    def test_unknown_model_no_wait(self):
        _wait_for_model("groq/never-seen")
        assert self._slept == []

    def test_models_tracked_independently(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 12.0)
        _wait_for_model("groq/openai/gpt-oss-120b")   # different model, no window
        assert self._slept == []

    def test_mark_updates_existing_entry(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 5.0)
        _mark_rate_limited("groq/openai/gpt-oss-20b", 20.0)  # longer window overwrites
        _wait_for_model("groq/openai/gpt-oss-20b")
        assert len(self._slept) == 1
        assert abs(self._slept[0] - 25.0) < 0.01   # 20 + 5 buffer

    def test_zero_buffer(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 10.0, buffer=0.0)
        _wait_for_model("groq/openai/gpt-oss-20b")
        assert len(self._slept) == 1
        assert abs(self._slept[0] - 10.0) < 0.01

    def test_has_pending_rate_limits(self):
        assert has_pending_rate_limits() is False
        _mark_rate_limited("groq/openai/gpt-oss-20b", 10.0)
        assert has_pending_rate_limits() is True

    def test_adaptive_cooldown_uses_pending_window_but_respects_ceiling(self):
        _mark_rate_limited("groq/openai/gpt-oss-20b", 40.0, buffer=0.0)
        assert adaptive_cooldown_seconds(30) == 30

    def test_adaptive_cooldown_uses_recent_pressure_when_window_has_passed(self):
        _mr._last_provider_pressure_at = self._fake_now[0]
        self._fake_now[0] += 12.0
        assert adaptive_cooldown_seconds(30) == 18.0


class TestRequestTooLargePerTask:
    """The request-too-large flag must be per-task, not session-wide.

    A huge spec that blows past GPT-OSS 20B's prescreen cap should not cause
    the next (possibly tiny) task to skip GPT-OSS 20B. Clearing is the caller's job and
    happens at the top of run_task.
    """

    def teardown_method(self):
        clear_request_too_large()
        _mr._observed_caps.clear()

    def test_mark_and_query(self):
        assert is_request_too_large("groq/openai/gpt-oss-20b") is False
        _mark_request_too_large("groq/openai/gpt-oss-20b")
        assert is_request_too_large("groq/openai/gpt-oss-20b") is True

    def test_clear_resets_flag(self):
        _mark_request_too_large("groq/openai/gpt-oss-20b")
        clear_request_too_large()
        assert is_request_too_large("groq/openai/gpt-oss-20b") is False

    def test_per_thread_isolation(self):
        import threading
        _mark_request_too_large("groq/openai/gpt-oss-20b")

        other_thread_saw = []

        def in_other_thread():
            other_thread_saw.append(is_request_too_large("groq/openai/gpt-oss-20b"))

        t = threading.Thread(target=in_other_thread)
        t.start()
        t.join()

        # Other thread has its own set -- should not see main thread's flag
        assert other_thread_saw == [False]
        # Main thread still sees its own flag
        assert is_request_too_large("groq/openai/gpt-oss-20b") is True

    def test_provider_rejection_records_observed_cap(self):
        cap = _record_observed_cap(
            "groq/openai/gpt-oss-20b",
            "Request too large for model ... Limit 8000, Requested 8072",
        )
        assert cap == 8000
        assert effective_input_cap("groq/openai/gpt-oss-20b", 16000) == 8000

    def test_observed_cap_beats_declared_cap(self):
        _record_observed_cap(
            "groq/openai/gpt-oss-20b",
            "Request too large for model ... Limit 8000, Requested 8072",
        )
        assert effective_input_cap("groq/openai/gpt-oss-20b", 32000) == 8000
        assert effective_input_cap("groq/openai/gpt-oss-20b", 6000) == 6000


class TestInvalidModelTracking:
    def teardown_method(self):
        _mr._invalid_models.clear()

    def test_mark_and_query_invalid_model(self):
        assert is_invalid_model("gemini/gemini-2.5-flash") is False
        mark_invalid_model("gemini/gemini-2.5-flash")
        assert is_invalid_model("gemini/gemini-2.5-flash") is True

    def test_invalid_model_error_detection(self):
        msg = 'GeminiException - {"error": {"status": "NOT_FOUND", "message": "model is not found for API version"}}'
        assert _is_invalid_model_error(msg) is True


class TestParseRetryAfter:
    def test_extracts_wait_time(self):
        msg = "Rate limit reached. Please try again in 12.5s."
        assert _parse_retry_after(msg) == 12.5

    def test_uses_last_occurrence(self):
        msg = "try again in 5s. ... try again in 10s."
        assert _parse_retry_after(msg) == 10.0

    def test_returns_none_when_not_found(self):
        assert _parse_retry_after("some other error") is None

    def test_handles_integer_seconds(self):
        assert _parse_retry_after("try again in 30s") == 30.0


class TestParseUsage:
    def test_empty_output(self):
        stats = _parse_usage("")
        assert stats == {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0}

    def test_basic_tokens_and_cost(self):
        out = "Tokens: 1.2k sent, 234 received. Cost: $0.0034 message, $0.01 session."
        stats = _parse_usage(out)
        assert stats["tokens_sent"] == 1200
        assert stats["tokens_received"] == 234
        assert stats["cost_usd"] == 0.0034

    def test_integer_tokens_no_unit(self):
        out = "Tokens: 850 sent, 412 received. Cost: $0.0001 message, $0.0001 session."
        stats = _parse_usage(out)
        assert stats["tokens_sent"] == 850
        assert stats["tokens_received"] == 412
        assert stats["cost_usd"] == 0.0001

    def test_megatokens_unit(self):
        out = "Tokens: 1.5M sent, 0.5M received. Cost: $4.20 message, $4.20 session."
        stats = _parse_usage(out)
        assert stats["tokens_sent"] == 1_500_000
        assert stats["tokens_received"] == 500_000
        assert stats["cost_usd"] == 4.20

    def test_sums_multiple_invocations(self):
        # aider can print usage multiple times in one --message run
        out = (
            "doing things\n"
            "Tokens: 1.0k sent, 100 received. Cost: $0.0010 message, $0.0010 session.\n"
            "more things\n"
            "Tokens: 2.0k sent, 200 received. Cost: $0.0020 message, $0.0030 session.\n"
        )
        stats = _parse_usage(out)
        assert stats["tokens_sent"] == 3000
        assert stats["tokens_received"] == 300
        assert stats["cost_usd"] == 0.0030

    def test_no_match_returns_zeroes(self):
        stats = _parse_usage("nothing aider-shaped here")
        assert stats == {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0}


class TestObservedCapPrescreen:
    def teardown_method(self):
        _mr._observed_caps.clear()

    def test_run_with_tier_fallback_skips_when_observed_cap_is_lower(self, monkeypatch):
        _mr._observed_caps["groq/openai/gpt-oss-20b"] = 8000
        monkeypatch.setattr(_mr, "_estimate_request_tokens", lambda *args, **kwargs: 9000)
        monkeypatch.setattr(_mr, "run_aider", lambda *args, **kwargs: pytest.fail("run_aider should not be called"))

        success, model_used, stats, attempts = _mr.run_with_tier_fallback(
            "primary",
            "message",
            "src/example.py",
            read_files=[],
            cwd=None,
        )

        assert success is False
        assert model_used is None
        assert stats == {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0}
        assert attempts == [
            {
                "model": "groq/openai/gpt-oss-20b",
                "reason": "pre_screen_too_large",
                "success": False,
                "wall_seconds": 0.0,
            }
        ]
