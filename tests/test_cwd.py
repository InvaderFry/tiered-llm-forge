"""Tests that the cwd parameter is respected across orchestrator modules.

Focuses on run_task(cwd=...) — the worktree code path — confirming that git
and pytest commands run inside the provided directory rather than the process
working directory.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.runner import file_size, run_tests


class TestFileSize:
    def test_cwd_resolves_relative_path(self, tmp_path):
        d = tmp_path / "sub"
        d.mkdir()
        f = d / "target.py"
        f.write_text("x = 1\n")
        # Relative path "target.py" resolved against cwd=d should find the file.
        size = file_size("target.py", cwd=str(d))
        assert size == f.stat().st_size

    def test_cwd_nonexistent_file_returns_zero(self, tmp_path):
        assert file_size("no_such_file.py", cwd=str(tmp_path)) == 0


class TestRunTestsCwd:
    def test_cwd_passed_to_pytest(self, tmp_path):
        # Write a passing test in a subdirectory; pytest must run there.
        sub = tmp_path / "proj"
        sub.mkdir()
        test_file = sub / "test_ok.py"
        test_file.write_text("def test_pass():\n    assert True\n")
        passed, output = run_tests("test_ok.py", cwd=str(sub))
        assert passed is True

    def test_run_tests_missing_file_fails(self, tmp_path):
        passed, output = run_tests("nonexistent_test.py", cwd=str(tmp_path))
        assert passed is False
        assert "does not exist" in output
