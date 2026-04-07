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
    _parse_usage,
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

    def test_large_spec_still_uses_first_primary(self):
        # The historical "large context" branch was dead code: spec text is
        # now attached as a read-only file, not embedded in the prompt, so
        # length no longer routes to a different model.
        model = select_model_for_spec("x" * 20_000)
        assert model == "groq/qwen/qwen3-32b"


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
