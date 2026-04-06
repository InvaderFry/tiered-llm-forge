"""Tests for the orchestrator's model_router module."""

import sys
import os
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.model_router import (
    select_model_for_spec,
    get_fallback_models,
    _parse_retry_after,
)
from orchestrator import config as config_mod


@pytest.fixture(autouse=True)
def mock_config(tmp_path):
    config_yaml = tmp_path / "models.yaml"
    config_yaml.write_text(textwrap.dedent("""\
        tiers:
          - name: primary
            models:
              - groq/qwen/qwen3-32b
              - groq/moonshotai/kimi-k2-instruct
              - groq/meta-llama/llama-4-scout-17b-16e-instruct
            retries: 3
          - name: escalation
            models:
              - groq/openai/gpt-oss-120b
            retries: 2
        weak_model: groq/llama-3.1-8b-instant
        large_context_model: groq/meta-llama/llama-4-scout-17b-16e-instruct
        large_context_chars: 16000
        spec_limits:
          soft_limit_chars: 12000
          hard_limit_chars: 16000
        cooldown_seconds: 0
    """))
    config_mod._config = None
    config_mod.load_config(config_yaml)
    yield
    config_mod._config = None


class TestSelectModel:
    def test_small_spec_uses_first_primary(self):
        model = select_model_for_spec("small spec")
        assert model == "groq/qwen/qwen3-32b"

    def test_large_spec_uses_large_context_model(self):
        model = select_model_for_spec("x" * 20_000)
        assert model == "groq/meta-llama/llama-4-scout-17b-16e-instruct"


class TestGetFallbackModels:
    def test_returns_remaining_after_current(self):
        fallbacks = get_fallback_models("groq/qwen/qwen3-32b", "primary")
        assert fallbacks == [
            "groq/moonshotai/kimi-k2-instruct",
            "groq/meta-llama/llama-4-scout-17b-16e-instruct",
        ]

    def test_returns_all_if_model_not_in_tier(self):
        fallbacks = get_fallback_models("unknown/model", "primary")
        assert len(fallbacks) == 3

    def test_last_model_returns_empty(self):
        fallbacks = get_fallback_models(
            "groq/meta-llama/llama-4-scout-17b-16e-instruct", "primary"
        )
        assert fallbacks == []


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
