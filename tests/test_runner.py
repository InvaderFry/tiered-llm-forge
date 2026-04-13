"""Tests for orchestrator.runner — test execution and regression detection."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.runner import (
    _repo_root_for_tests,
    run_tests,
    run_full_suite,
    file_size,
    check_regression,
    _maybe_prepare_runtime_for_suite,
    _maybe_prepare_runtime_for_tests,
    _content_looks_valid,
    _CONTENT_MARKERS,
)


class TestFileSize:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("print('hi')\n")
        assert file_size(str(f)) == len("print('hi')\n")

    def test_missing_file(self, tmp_path):
        assert file_size(str(tmp_path / "nope.py")) == 0


class TestContentLooksValid:
    def test_valid_python(self, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("import os\n\ndef main():\n    pass\n")
        assert _content_looks_valid(str(f)) is True

    def test_invalid_python_no_markers(self, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("x = 1\n")
        assert _content_looks_valid(str(f)) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert _content_looks_valid(str(tmp_path / "gone.py")) is False

    def test_empty_file_returns_false(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        assert _content_looks_valid(str(f)) is False

    def test_unknown_extension_returns_true(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02")
        assert _content_looks_valid(str(f)) is True

    def test_typescript_markers(self, tmp_path):
        f = tmp_path / "index.ts"
        f.write_text("import { foo } from './bar';\nexport const x = 1;\n")
        assert _content_looks_valid(str(f)) is True

    def test_typescript_invalid(self, tmp_path):
        f = tmp_path / "index.ts"
        f.write_text("x = 1\n")
        assert _content_looks_valid(str(f)) is False

    def test_go_markers(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("package main\n\nfunc main() {}\n")
        assert _content_looks_valid(str(f)) is True

    def test_rust_markers(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use std::io;\n\nfn main() {}\n")
        assert _content_looks_valid(str(f)) is True

    def test_java_markers(self, tmp_path):
        f = tmp_path / "Main.java"
        f.write_text("package com.example;\n\npublic class Main {}\n")
        assert _content_looks_valid(str(f)) is True

    def test_json_markers(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}\n')
        assert _content_looks_valid(str(f)) is True

    def test_yaml_markers(self, tmp_path):
        f = tmp_path / "config.yml"
        f.write_text("key: value\n")
        assert _content_looks_valid(str(f)) is True

    def test_all_known_extensions_have_markers(self):
        """Every extension in _CONTENT_MARKERS should have at least one marker."""
        for ext, markers in _CONTENT_MARKERS.items():
            assert len(markers) > 0, f"Extension {ext} has no markers"

    def test_c_markers(self, tmp_path):
        f = tmp_path / "main.c"
        f.write_text('#include <stdio.h>\nint main() { return 0; }\n')
        assert _content_looks_valid(str(f)) is True

    def test_ruby_markers(self, tmp_path):
        f = tmp_path / "app.rb"
        f.write_text("class App\n  def run\n  end\nend\n")
        assert _content_looks_valid(str(f)) is True

    def test_swift_markers(self, tmp_path):
        f = tmp_path / "main.swift"
        f.write_text("import Foundation\nfunc greet() {}\n")
        assert _content_looks_valid(str(f)) is True

    def test_kotlin_markers(self, tmp_path):
        f = tmp_path / "Main.kt"
        f.write_text("package com.example\nfun main() {}\n")
        assert _content_looks_valid(str(f)) is True

    def test_csharp_markers(self, tmp_path):
        f = tmp_path / "Program.cs"
        f.write_text("using System;\nnamespace App { class Program {} }\n")
        assert _content_looks_valid(str(f)) is True


class TestCheckRegression:
    def test_no_regression_when_file_grows(self, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("import os\n\ndef foo():\n    pass\n" * 10)
        baseline = file_size(str(f))
        f.write_text("import os\n\ndef foo():\n    pass\n" * 20)
        assert check_regression(str(f), baseline) is False

    def test_regression_when_file_shrinks_below_20_pct(self, tmp_path):
        f = tmp_path / "module.py"
        content = "import os\n\ndef foo():\n    pass\n" * 10
        f.write_text(content)
        baseline = file_size(str(f))
        f.write_text("import os\n")  # much smaller but has markers
        assert check_regression(str(f), baseline) is True

    def test_no_regression_for_small_baseline(self, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("import os\n")
        # baseline < 50 bytes: size check skipped
        assert check_regression(str(f), 10) is False

    def test_no_regression_new_file_with_no_markers(self, tmp_path):
        # A pure-data file (e.g. constants.py with only assignments) created
        # from scratch (baseline=0) should NOT trigger a false positive.
        # Content-marker check only runs when the file also shrank vs baseline.
        f = tmp_path / "module.py"
        f.write_text("x = 1\n")  # no def/class/import markers — was wrongly flagged
        assert check_regression(str(f), 0) is False

    def test_regression_when_size_shrinks_over_80_pct(self, tmp_path):
        # >80% size reduction triggers the first (size-only) check regardless
        # of content — this is the fast path.
        f = tmp_path / "module.py"
        f.write_text("x = 1\n")  # 6 bytes; baseline=500 → 6/500 < 0.2
        assert check_regression(str(f), 500) is True

    def test_regression_when_content_invalid_and_shrank_50_to_80_pct(self, tmp_path):
        # Shrank >50% but <=80% (misses the >80% size check) AND no content markers.
        # This exercises the content-marker gate specifically.
        # baseline=100, current=30 bytes → 30% of baseline → in (20%, 50%) range.
        f = tmp_path / "module.py"
        f.write_text("x = 1\ny = 2\nz = 3\na = 4\nb = 5\n")  # 30 bytes, no markers
        assert check_regression(str(f), 100) is True

    def test_no_regression_when_content_valid_despite_moderate_shrinkage(self, tmp_path):
        # Shrank >50% but <=80% AND file still has valid Python markers → not a regression.
        # baseline=100, current=30 bytes, but the content has "import" marker.
        f = tmp_path / "module.py"
        f.write_text("import os\nfoo = 1\nbar = 2\n")  # ~24 bytes, has "import" marker
        assert check_regression(str(f), 100) is False

    def test_no_regression_when_file_never_existed(self, tmp_path):
        # baseline=0 AND file still missing: aider produced no output, which
        # is "no progress" -- the task_runner handles that via a HEAD-move
        # check. Flagging it as a regression here caused git reset --hard
        # HEAD~1 to chew through dependency history (see FIX-SUMMARY).
        f = tmp_path / "module.py"
        assert check_regression(str(f), 0) is False

    def test_no_regression_when_file_empty_and_baseline_zero(self, tmp_path):
        f = tmp_path / "module.py"
        f.write_text("")
        assert check_regression(str(f), 0) is False

    def test_regression_when_file_emptied_from_real_baseline(self, tmp_path):
        # baseline > 0 but file now empty: this IS a real regression.
        f = tmp_path / "module.py"
        f.write_text("")
        assert check_regression(str(f), 500) is True


class TestRunTests:
    def test_passing_test(self, tmp_path):
        test_file = tmp_path / "test_ok.py"
        test_file.write_text("def test_pass():\n    assert 1 + 1 == 2\n")
        passed, output = run_tests(str(test_file))
        assert passed is True

    def test_failing_test(self, tmp_path):
        test_file = tmp_path / "test_fail.py"
        test_file.write_text("def test_fail():\n    assert 1 == 2\n")
        passed, output = run_tests(str(test_file))
        assert passed is False

    def test_missing_test_file(self, tmp_path):
        passed, output = run_tests(str(tmp_path / "nope.py"))
        assert passed is False
        assert "does not exist" in output

    def test_vacuous_pass_detected(self, tmp_path):
        test_file = tmp_path / "test_empty.py"
        test_file.write_text("# no tests here\n")
        passed, output = run_tests(str(test_file))
        assert passed is False
        assert "Vacuous pass" in output

    def test_offline_maven_test_triggers_runtime_preflight(self, tmp_path, monkeypatch):
        test_file = tmp_path / "test_maven.py"
        test_file.write_text('run_checked("mvn", "-o", "-q", "-DskipTests", "compile")\n')
        calls = []
        monkeypatch.setattr(
            "orchestrator.runner.maybe_prime_maven_cache",
            lambda repo_root, reason=None: calls.append((str(repo_root), reason)) or (True, ""),
        )

        _maybe_prepare_runtime_for_tests(test_file)

        assert calls == [(str(tmp_path), "offline Maven compile/package detected in task test")]

    def test_repo_root_for_tests_walks_up_to_project_root(self, tmp_path):
        project = tmp_path / "project"
        tests_dir = project / "tests"
        tests_dir.mkdir(parents=True)
        (project / "pom.xml").write_text("<project/>\n")
        test_file = tests_dir / "test_maven.py"
        test_file.write_text("pass\n")

        assert _repo_root_for_tests(test_file) == project


class TestRunFullSuite:
    def test_passing_suite(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_one.py").write_text("def test_ok():\n    assert True\n")
        passed, output = run_full_suite(str(tests_dir))
        assert passed is True

    def test_failing_suite(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_bad.py").write_text("def test_fail():\n    assert False\n")
        passed, output = run_full_suite(str(tests_dir))
        assert passed is False

    def test_missing_directory(self, tmp_path):
        passed, output = run_full_suite(str(tmp_path / "nope"))
        assert passed is False
        assert "does not exist" in output

    def test_suite_runtime_preflight_scans_for_offline_maven(self, tmp_path, monkeypatch):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_pkg.py").write_text('run_checked("mvn", "-o", "-q", "-DskipTests", "package")\n')
        calls = []
        monkeypatch.setattr(
            "orchestrator.runner.maybe_prime_maven_cache",
            lambda repo_root, reason=None: calls.append((str(repo_root), reason)) or (True, ""),
        )

        _maybe_prepare_runtime_for_suite(tests_dir)

        assert calls == [(str(tmp_path), "offline Maven compile/package detected in test suite")]
