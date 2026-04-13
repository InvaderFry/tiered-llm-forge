"""Tests for orchestrator.task_runner helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

import orchestrator.task_runner as task_runner
from orchestrator.task_runner import (
    _build_read_context,
    _compute_resume_starts,
    _final_failure_label,
    _forbidden_changed_files,
)


class TestComputeResumeStarts:
    def test_fresh_run_returns_defaults(self):
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(None)
        assert skip is False
        assert p_start == 1
        assert e_start == 1
        assert skip_gemini is False

    def test_primary_exhausted_skip_to_escalation(self):
        # last attempt was primary#3 (last attempt of a 3-retry tier)
        rp = {"tier": "primary", "attempt": 3, "total_attempts": 3}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is False
        assert p_start == 4   # one past the last attempt (will be >= retries+1, so loop is skipped)
        assert e_start == 1
        assert skip_gemini is False

    def test_mid_primary_resumes_at_next_attempt(self):
        rp = {"tier": "primary", "attempt": 2, "total_attempts": 2}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is False
        assert p_start == 3
        assert e_start == 1
        assert skip_gemini is False

    def test_escalation_tier_skips_primary(self):
        # Any resume from escalation should skip the primary loop entirely.
        rp = {"tier": "escalation", "attempt": 1, "total_attempts": 4}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert e_start == 2
        assert skip_gemini is False

    def test_escalation_mid_tier_resumes_at_next(self):
        # Crashed mid-escalation at attempt 2 → next run starts at attempt 3.
        rp = {"tier": "escalation", "attempt": 2, "total_attempts": 5}
        skip, _, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert e_start == 3
        assert skip_gemini is False

    def test_gemini_tier_skips_all_and_sets_skip_gemini(self):
        # If last attempt was in the gemini tier, all tiers are skipped
        # so --resume falls straight to Stage 3 (Claude review).
        rp = {"tier": "gemini", "attempt": 1, "total_attempts": 6}
        skip, p_start, e_start, skip_gemini = _compute_resume_starts(rp)
        assert skip is True
        assert p_start == 1
        assert e_start == 1
        assert skip_gemini is True


class TestForbiddenChangedFiles:
    def test_allows_only_target_file(self):
        forbidden = _forbidden_changed_files(
            ["src/app.py", "tests/test_app.py", "README.md"],
            {"src/app.py"},
        )
        assert forbidden == ["README.md", "tests/test_app.py"]

    def test_empty_when_only_allowed_file_changed(self):
        assert _forbidden_changed_files(["src/app.py"], {"src/app.py"}) == []


class TestBuildReadContext:
    def test_trims_dependency_context_to_budget(self, tmp_path, monkeypatch):
        specs = {
            "task-001-a": {"target": "src/a.py"},
            "task-002-b": {"target": "src/b.py"},
            "task-003-c": {"target": "src/c.py"},
        }
        (tmp_path / "specs").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "src").mkdir()
        spec_path = tmp_path / "specs" / "task-004-main.md"
        test_path = tmp_path / "tests" / "test_004_main.py"
        spec_path.write_text("spec\n")
        test_path.write_text("test\n")
        for name in ("a.py", "b.py", "c.py"):
            (tmp_path / "src" / name).write_text("x = 1\n")

        monkeypatch.setattr(
            task_runner,
            "get_config",
            lambda: {"context_limits": {"max_read_files": 4, "max_dependency_files": 2, "max_total_bytes": 9999}},
        )

        read_files, omitted = _build_read_context(
            {"path": spec_path},
            "tests/test_004_main.py",
            ["task-001-a", "task-002-b", "task-003-c"],
            specs,
            cwd=str(tmp_path),
        )

        assert read_files == [
            str(spec_path),
            "tests/test_004_main.py",
            "src/a.py",
            "src/b.py",
        ]
        assert omitted == ["src/c.py"]

    def test_omits_first_dependency_when_it_exceeds_byte_budget(self, tmp_path, monkeypatch):
        specs = {"task-001-a": {"target": "src/a.py"}}
        (tmp_path / "specs").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "src").mkdir()
        spec_path = tmp_path / "specs" / "task-004-main.md"
        test_path = tmp_path / "tests" / "test_004_main.py"
        spec_path.write_text("spec\n")
        test_path.write_text("test\n")
        (tmp_path / "src" / "a.py").write_text("x = 1\n" * 500)

        monkeypatch.setattr(
            task_runner,
            "get_config",
            lambda: {"context_limits": {"max_read_files": 4, "max_dependency_files": 2, "max_total_bytes": 100}},
        )

        read_files, omitted = _build_read_context(
            {"path": spec_path},
            "tests/test_004_main.py",
            ["task-001-a"],
            specs,
            cwd=str(tmp_path),
        )

        assert read_files == [str(spec_path), "tests/test_004_main.py"]
        assert omitted == ["src/a.py"]


class TestFinalFailureLabel:
    def test_uses_test_failure_when_a_model_successfully_edited(self):
        failure_label, test_failure_label = _final_failure_label(
            "AssertionError: assert 1 == 2",
            ["invalid_model_config"],
            had_successful_model_attempt=True,
        )
        assert failure_label == "assertion"
        assert test_failure_label == "assertion"

    def test_uses_llm_reason_when_no_model_produced_a_successful_edit(self):
        failure_label, test_failure_label = _final_failure_label(
            "AssertionError: assert 1 == 2",
            ["invalid_model_config"],
            had_successful_model_attempt=False,
        )
        assert failure_label == "invalid_model_config"
        assert test_failure_label == "assertion"
