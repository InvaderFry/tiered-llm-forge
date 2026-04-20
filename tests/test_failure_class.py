"""Tests for orchestrator.failure_class."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.failure_class import classify, classify_terminal, extract_test_file_bug_hint


class TestFailureClass:
    def test_rate_limit(self):
        assert classify("groq rate_limit_exceeded: try again in 1.2s") == "rate_limit"

    def test_request_too_large(self):
        assert classify("Request too large for TPM cap") == "request_too_large"

    def test_regression_guard(self):
        assert classify("  [REGRESSION GUARD] file shrank") == "regression_guard"

    def test_timeout(self):
        assert classify("test timed out after 30s") == "timeout"

    def test_collection_error(self):
        assert classify("ERROR collecting tests/test_foo.py") == "collection_error"

    def test_missing_symbol(self):
        assert classify("ImportError: cannot import name 'foo'") == "missing_symbol"

    def test_assertion(self):
        assert classify("E       AssertionError: assert 1 == 2") == "assertion"

    def test_unknown(self):
        assert classify("") == "unknown"
        assert classify("random noise") == "unknown"

    def test_rate_limit_precedence_over_assertion(self):
        # Rate limit rule sits above assertion in the rule list so mixed
        # output is classified by the more actionable signal.
        mixed = "rate_limit_exceeded ... later an AssertionError somewhere"
        assert classify(mixed) == "rate_limit"

    def test_dependency_cache_missing(self):
        msg = "offline mode and the artifact x has not been downloaded from it before"
        assert classify(msg) == "dependency_cache_missing"

    def test_invalid_model_config(self):
        msg = '"status": "NOT_FOUND" model is not found for API version'
        assert classify(msg) == "invalid_model_config"

    def test_terminal_class_uses_llm_reason_when_test_output_is_generic(self):
        assert classify_terminal("AssertionError: assert 1 == 2", ["invalid_model_config"]) == "invalid_model_config"

    def test_extracts_test_file_bug_hint(self):
        msg = "AssertionError\nHINT: planner-written test expects the wrong literal value\n"
        assert extract_test_file_bug_hint(msg) == "planner-written test expects the wrong literal value"
