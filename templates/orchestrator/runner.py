"""Test execution and regression detection."""

import shutil
import subprocess
import sys
from pathlib import Path


def _resolve(path, cwd=None):
    """Resolve a relative path against an optional working directory."""
    p = Path(path)
    return Path(cwd) / p if cwd and not p.is_absolute() else p


def _pytest_cmd():
    """Return the command prefix for invoking pytest.

    Prefers a bare ``pytest`` when it is on PATH (the common case inside a
    virtualenv).  Falls back to ``sys.executable -m pytest`` so the
    orchestrator works even when the ``pytest`` console-script was installed
    into a directory that isn't on PATH.
    """
    if shutil.which("pytest"):
        return ["pytest"]
    return [sys.executable, "-m", "pytest"]

# Timeout for per-task test runs (seconds)
TEST_TIMEOUT = 120
# Timeout for the full integration test suite (seconds)
SUITE_TIMEOUT = 300


def run_tests(test_file, cwd=None):
    """
    Run a specific test file with pytest.

    Returns (passed, output). Fails if the test file doesn't exist
    or if pytest collects 0 items (vacuous pass guard).
    """
    test_path = _resolve(test_file, cwd)
    if not test_path.exists():
        return False, f"Test file {test_file} does not exist."

    cmd = [*_pytest_cmd(), test_file, "-x", "--tb=short"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TEST_TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, f"Timeout: pytest exceeded {TEST_TIMEOUT}s on {test_file}."

    output = result.stdout + result.stderr

    # Guard against vacuous passes
    if "no tests ran" in output or "collected 0 items" in output:
        return False, f"Vacuous pass: pytest collected 0 tests from {test_file}.\n\n{output}"

    return result.returncode == 0, output


def run_full_suite(tests_dir="tests", cwd=None):
    """
    Run the full pytest suite under ``tests_dir``.

    Returns (passed, output). Used by the integration gate after all
    per-task tests pass to catch cross-task regressions before merge.
    """
    tests_path = _resolve(tests_dir, cwd)
    if not tests_path.exists():
        return False, f"Tests directory {tests_dir} does not exist."

    cmd = [*_pytest_cmd(), str(tests_dir), "--tb=short", "-q"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUITE_TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, f"Timeout: full test suite exceeded {SUITE_TIMEOUT}s."

    output = result.stdout + result.stderr

    if "no tests ran" in output or "collected 0 items" in output:
        return False, f"Vacuous pass: pytest collected 0 tests from {tests_dir}.\n\n{output}"

    return result.returncode == 0, output


def file_size(path, cwd=None):
    """Return file size in bytes, or 0 if the file doesn't exist."""
    p = _resolve(path, cwd)
    return p.stat().st_size if p.exists() else 0


def check_regression(target_file, baseline_size, cwd=None):
    """
    Return True if the target file looks corrupted after a model edit.

    Checks:
    1. Size regression — file shrank by >80% vs baseline.
    2. Content sanity — file no longer contains expected language markers,
       but *only* when the file also shrank by >50% vs baseline. Running the
       content check unconditionally produced false positives on legitimate
       small files (e.g. ``src/constants.py`` with only assignments, or a
       pure-data ``__init__.py``) that contain none of the marker strings. By
       tying the content check to a size regression we make it a *confirmation*
       of shrinkage rather than an independent tripwire.

    Special case: when ``baseline_size == 0`` (the file did not exist before
    the model ran), an empty-or-still-missing file is **not** a regression —
    it's a no-progress attempt. Returning True here would trigger the
    task_runner's revert, which historically called ``git reset --hard
    HEAD~1`` and ate the dependency branch's history. The caller handles
    no-progress separately via a HEAD-move check.
    """
    current = file_size(target_file, cwd=cwd)

    if baseline_size >= 50 and current < baseline_size * 0.2:
        return True

    if baseline_size == 0 and current == 0:
        return False

    # Only run the content check when the file has shrunk by more than half —
    # that's the signal that something went wrong, and the content markers
    # then serve as confirmation.
    if baseline_size >= 50 and current < baseline_size * 0.5:
        if not _content_looks_valid(target_file, cwd=cwd):
            return True

    return False


# Content markers for sanity-checking source files by extension.
#
_CONTENT_MARKERS = {
    ".py":    ["def ", "class ", "import ", "from "],
    ".java":  ["class ", "interface ", "package ", "public ", "import "],
    ".ts":    ["import ", "export ", "function ", "const ", "interface "],
    ".tsx":   ["import ", "export ", "function ", "const ", "interface "],
    ".js":    ["import ", "export ", "function ", "const ", "require("],
    ".jsx":   ["import ", "export ", "function ", "const ", "require("],
    ".go":    ["package ", "func ", "import "],
    ".rs":    ["fn ", "use ", "mod ", "struct ", "impl "],
    ".rb":    ["def ", "class ", "module ", "require "],
    ".kt":    ["fun ", "class ", "package ", "import "],
    ".cs":    ["class ", "namespace ", "using "],
    ".swift": ["func ", "class ", "import ", "struct "],
    ".c":     ["#include", "int ", "void "],
    ".cpp":   ["#include", "class ", "namespace "],
    ".h":     ["#include", "#ifndef", "#pragma"],
    ".xml":   ["<", "<?xml"],
    ".yml":   [":"],
    ".yaml":  [":"],
    ".json":  ["{", "["],
}


def _content_looks_valid(target_file, cwd=None):
    """
    Quick sanity check: does the file still look like source code?

    Returns True if the file contains at least one expected marker for its
    extension, or if the extension is unknown.
    """
    ext = Path(target_file).suffix.lower()
    markers = _CONTENT_MARKERS.get(ext)
    if not markers:
        return True

    p = _resolve(target_file, cwd)
    if not p.exists() or p.stat().st_size == 0:
        return False

    try:
        content = p.read_text(errors="replace")
    except Exception:
        return True

    return any(marker in content for marker in markers)
