"""Tests for orchestrator.failure_class."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.failure_class import classify


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
