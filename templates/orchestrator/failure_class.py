"""Classify failure output so the pipeline can route the right kind of fix.

The orchestrator and any downstream fixer only get raw pytest/aider output.
A small set of keyword rules is enough to distinguish the common buckets:

    rate_limit       -- provider throttled us; waiting or swapping tiers helps
    request_too_large -- spec exceeds the model's TPM cap; must compress or split
    collection_error -- pytest could not even import the test file
    missing_symbol   -- AttributeError / ImportError / NameError against target
    assertion        -- tests ran but produced wrong answers
    timeout          -- pytest timed out or killed a hung process
    regression_guard -- our own sanity check tripped (file shrank, markers gone)
    unknown          -- fall-through bucket
"""

_RULES = [
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
