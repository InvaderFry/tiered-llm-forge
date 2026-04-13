"""Test execution and regression detection."""

import subprocess
import sys
from pathlib import Path

from .preflight import maybe_prime_maven_cache


def _resolve(path, cwd=None):
    """Resolve a relative path against an optional working directory."""
    p = Path(path)
    return Path(cwd) / p if cwd and not p.is_absolute() else p


def _pytest_cmd():
    """Return the command prefix for invoking pytest.

    Always use the active interpreter so the orchestrator and pytest share
    the same virtualenv, plugin set, and marker registrations.
    """
    return [sys.executable, "-m", "pytest"]


def _repo_root_for_tests(test_path, cwd=None):
    """Return the repo root for a test path, preferring the explicit cwd.

    In the default sequential path ``cwd`` is usually ``None``, so a test file
    under ``tests/`` must walk upward to find the project root rather than
    treating ``tests/`` itself as the repo root. This matters for runtime
    preflight helpers such as Maven cache warmup, which need to run where
    ``pom.xml`` lives.
    """
    if cwd:
        return Path(cwd)
    start = Path(test_path).resolve()
    if start.is_file():
        start = start.parent

    for candidate in (start, *start.parents):
        if (candidate / "pom.xml").exists() or (candidate / ".git").exists():
            return candidate

    if start.name == "tests" and start.parent != start:
        return start.parent

    return start


def _text_requests_offline_maven(text):
    """Return True if a test body appears to run Maven in offline mode."""
    return "mvn" in text and "-o" in text and ("compile" in text or "package" in text)


def _test_requests_offline_maven(test_path):
    """Return True if the single test file appears to use offline Maven commands."""
    try:
        return _text_requests_offline_maven(Path(test_path).read_text(encoding="utf-8"))
    except OSError:
        return False


def _suite_requests_offline_maven(tests_path):
    """Return True if any test file under tests_path appears to use offline Maven."""
    for path in Path(tests_path).rglob("test_*.py"):
        if _test_requests_offline_maven(path):
            return True
    return False


def _maybe_prepare_runtime_for_tests(test_path, cwd=None):
    """Warm known dependency caches needed by the test before pytest runs."""
    repo_root = _repo_root_for_tests(test_path, cwd=cwd)
    if _test_requests_offline_maven(test_path):
        maybe_prime_maven_cache(repo_root, reason="offline Maven compile/package detected in task test")


def _maybe_prepare_runtime_for_suite(tests_path, cwd=None):
    """Warm known dependency caches needed by the full suite before pytest runs."""
    repo_root = _repo_root_for_tests(tests_path, cwd=cwd)
    if _suite_requests_offline_maven(tests_path):
        maybe_prime_maven_cache(repo_root, reason="offline Maven compile/package detected in test suite")

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

    _maybe_prepare_runtime_for_tests(test_path, cwd=cwd)
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

    _maybe_prepare_runtime_for_suite(tests_path, cwd=cwd)
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
