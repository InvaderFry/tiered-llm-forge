"""Classify failure output so the pipeline can route the right kind of fix.

The orchestrator and any downstream fixer only get raw pytest/aider output.
A small set of keyword rules is enough to distinguish the common buckets:

    invalid_model_config -- provider rejected the configured model id
    dependency_cache_missing -- offline build/test expected deps that were never fetched
    rate_limit       -- provider throttled us; waiting or swapping tiers helps
    request_too_large -- spec exceeds the model's TPM cap; must compress or split
    forbidden_file_edit -- model edited files outside the task write scope
    test_file_bug  -- evidence says the planner-written task test/spec is wrong
    collection_error -- pytest could not even import the test file
    missing_symbol   -- AttributeError / ImportError / NameError against target
    assertion        -- tests ran but produced wrong answers
    timeout          -- pytest timed out or killed a hung process
    regression_guard -- our own sanity check tripped (file shrank, markers gone)
    unknown          -- fall-through bucket
"""

import re

_RULES = [
    ("invalid_model_config", ('"status": "NOT_FOUND"', "is not found for API version", "Unknown model")),
    (
        "dependency_cache_missing",
        (
            "offline mode and the artifact",
            "has not been downloaded from it before",
            "DependencyResolutionException",
        ),
    ),
    # gemini_quota_exhausted must precede rate_limit — RESOURCE_EXHAUSTED messages
    # can also match the generic rate-limit needles.
    ("gemini_quota_exhausted", ("RESOURCE_EXHAUSTED", "daily quota", "Quota exceeded")),
    ("forbidden_file_edit", ("[FORBIDDEN EDIT]", "forbidden_file_edit")),
    ("test_file_bug", ("test_file_bug",)),
    ("rate_limit", ("rate_limit_exceeded", "Rate limit reached", "429")),
    ("request_too_large", ("Request too large",)),
    ("regression_guard", ("REGRESSION GUARD",)),
    ("timeout", ("Timeout", "timed out", "pytest-timeout")),
    ("collection_error", ("ERROR collecting", "errors during collection", "ImportError while importing test")),
    ("missing_symbol", ("AttributeError", "ImportError", "NameError", "ModuleNotFoundError")),
    ("assertion", ("AssertionError", "assert ")),
]


def classify(output):
    """Return the failure class string for a given block of output."""
    if not output:
        return "unknown"
    for label, needles in _RULES:
        for needle in needles:
            if needle in output:
                return label
    return "unknown"


_LLM_REASON_TO_CLASS = {
    "invalid_model_config": "invalid_model_config",
    "forbidden_file_edit": "forbidden_file_edit",
    "dependency_owned_forbidden_edit": "forbidden_file_edit",
    "pre_screen_too_large": "request_too_large",
    "request_too_large": "request_too_large",
    "gemini_quota_exhausted": "gemini_quota_exhausted",
}

_TEST_FILE_BUG_HINT_RE = re.compile(r"^HINT:\s*(.+)$", re.MULTILINE)


def classify_terminal(output, llm_fail_reasons=None):
    """Return the most actionable terminal failure class for a task."""
    direct = classify(output)
    if direct not in {"assertion", "unknown"}:
        return direct

    for reason in llm_fail_reasons or []:
        mapped = _LLM_REASON_TO_CLASS.get(reason)
        if mapped:
            return mapped

    return direct


def extract_test_file_bug_hint(output):
    """Return an operator hint that supports classifying a failure as test-side."""
    if not output:
        return None

    match = _TEST_FILE_BUG_HINT_RE.search(output)
    if match:
        return match.group(1).strip()

    needles = (
        "planner-written test",
        "task test is wrong",
        "spec bug",
        "test_file_bug",
        "expected value in the test is incorrect",
    )
    for line in output.splitlines():
        lower = line.lower()
        if any(needle in lower for needle in needles):
            return line.strip()

    return None
