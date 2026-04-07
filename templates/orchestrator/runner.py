"""Test execution and regression detection."""

import subprocess
from pathlib import Path


def run_tests(test_file):
    """
    Run a specific test file with pytest.

    Returns (passed, output). Fails if the test file doesn't exist
    or if pytest collects 0 items (vacuous pass guard).
    """
    test_path = Path(test_file)
    if not test_path.exists():
        return False, f"Test file {test_file} does not exist."

    cmd = ["pytest", test_file, "-x", "--tb=short"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    # Guard against vacuous passes
    if "no tests ran" in output or "collected 0 items" in output:
        return False, f"Vacuous pass: pytest collected 0 tests from {test_file}.\n\n{output}"

    return result.returncode == 0, output


def run_full_suite(tests_dir="tests"):
    """
    Run the full pytest suite under ``tests_dir``.

    Returns (passed, output). Used by the integration gate after all
    per-task tests pass to catch cross-task regressions before merge.
    """
    tests_path = Path(tests_dir)
    if not tests_path.exists():
        return False, f"Tests directory {tests_dir} does not exist."

    cmd = ["pytest", str(tests_path), "--tb=short", "-q"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    if "no tests ran" in output or "collected 0 items" in output:
        return False, f"Vacuous pass: pytest collected 0 tests from {tests_dir}.\n\n{output}"

    return result.returncode == 0, output


def file_size(path):
    """Return file size in bytes, or 0 if the file doesn't exist."""
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


def check_regression(target_file, baseline_size):
    """
    Return True if the target file looks corrupted after a model edit.

    Checks:
    1. Size regression — file shrank by >80% vs baseline.
    2. Content sanity — file no longer contains expected language markers.
    """
    if baseline_size >= 50:
        current = file_size(target_file)
        if current < baseline_size * 0.2:
            return True

    if not _content_looks_valid(target_file):
        return True

    return False


# Content markers for sanity-checking source files by extension
_CONTENT_MARKERS = {
    ".py":   ["def ", "class ", "import ", "from "],
    ".java": ["class ", "interface ", "package ", "public ", "import "],
    ".xml":  ["<", "<?xml"],
    ".yml":  [":"],
    ".yaml": [":"],
    ".json": ["{", "["],
}


def _content_looks_valid(target_file):
    """
    Quick sanity check: does the file still look like source code?

    Returns True if the file contains at least one expected marker for its
    extension, or if the extension is unknown.
    """
    ext = Path(target_file).suffix.lower()
    markers = _CONTENT_MARKERS.get(ext)
    if not markers:
        return True

    p = Path(target_file)
    if not p.exists() or p.stat().st_size == 0:
        return True

    try:
        content = p.read_text(errors="replace")
    except Exception:
        return True

    return any(marker in content for marker in markers)
