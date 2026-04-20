"""Tests for orchestrator.task_runner helpers."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

import orchestrator.task_runner as task_runner
from orchestrator.task_runner import (
    _build_read_context,
    _classify_test_file_bug,
    _compute_resume_starts,
    _final_failure_label,
    _forbidden_changed_files,
    _forbidden_edit_subtype,
    _format_scope_guidance,
    _pytest_failure_digest,
    _restore_retry_context,
    _run_tier_attempts,
    _should_resume_existing_branch,
    _target_file_is_trivial,
    run_task,
)
from orchestrator.state import load_state, record_task


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


class TestForbiddenEditSubtype:
    def test_marks_dependency_owned_forbidden_edits(self):
        subtype, matches = _forbidden_edit_subtype(
            ["pom.xml", "README.md"],
            ["pom.xml", "src/shared.py"],
        )
        assert subtype == "dependency_target"
        assert matches == ["pom.xml"]

    def test_leaves_unrelated_drift_generic(self):
        subtype, matches = _forbidden_edit_subtype(
            ["README.md"],
            ["pom.xml"],
        )
        assert subtype == "generic"
        assert matches == []


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

        read_files, attached, omitted = _build_read_context(
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
        assert attached == ["src/a.py", "src/b.py"]
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

        read_files, attached, omitted = _build_read_context(
            {"path": spec_path},
            "tests/test_004_main.py",
            ["task-001-a"],
            specs,
            cwd=str(tmp_path),
        )

        assert read_files == [str(spec_path), "tests/test_004_main.py"]
        assert attached == []
        assert omitted == ["src/a.py"]


class TestScopeGuidance:
    def test_mentions_only_attached_dependency_targets(self):
        guidance = _format_scope_guidance(
            {"src/app.py"},
            attached_dependency_target_files=["pom.xml"],
        )

        assert "pom.xml" in guidance
        assert "application.yml" not in guidance


class TestTrivialTargetDetection:
    def test_detects_zero_byte_file(self, tmp_path):
        target = tmp_path / "src" / "empty.py"
        target.parent.mkdir()
        target.write_text("")

        assert _target_file_is_trivial("src/empty.py", cwd=str(tmp_path)) is True

    def test_detects_whitespace_only_file(self, tmp_path):
        target = tmp_path / "src" / "empty.py"
        target.parent.mkdir()
        target.write_text("   \n\t")

        assert _target_file_is_trivial("src/empty.py", cwd=str(tmp_path)) is True

    def test_allows_legitimate_tiny_file(self, tmp_path):
        target = tmp_path / "src" / "tiny.py"
        target.parent.mkdir()
        target.write_text("x=1\n")

        assert _target_file_is_trivial("src/tiny.py", cwd=str(tmp_path)) is False


class TestExistingBranchRecoverability:
    def test_resumes_empty_target_branch(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("")

        should_resume, reason = _should_resume_existing_branch(
            state,
            "task-001",
            "src/example.py",
            cwd=str(tmp_path),
        )

        assert should_resume is True
        assert reason == "empty_target_file"

    def test_resumes_nonproductive_history(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("print('hello')\n")
        state["tasks"]["task-001"] = {
            "attempts_log": [
                {
                    "attempt": 1,
                    "tier": "primary",
                    "model": "m",
                    "aider_success": False,
                    "tests_passed": False,
                    "post_check_reason": "no_commit",
                    "model_attempts": [{"model": "m", "reason": "error", "success": False}],
                }
            ]
        }

        should_resume, reason = _should_resume_existing_branch(
            state,
            "task-001",
            "src/example.py",
            cwd=str(tmp_path),
        )

        assert should_resume is True
        assert reason == "non_productive_latest_attempt"

    def test_escalates_meaningful_prior_attempt(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("print('hello')\n")
        state["tasks"]["task-001"] = {
            "attempts_log": [
                {
                    "attempt": 1,
                    "tier": "primary",
                    "model": "m",
                    "aider_success": True,
                    "tests_passed": False,
                    "model_attempts": [{"model": "m", "reason": "ok", "success": True}],
                }
            ]
        }

        should_resume, reason = _should_resume_existing_branch(
            state,
            "task-001",
            "src/example.py",
            cwd=str(tmp_path),
        )

        assert should_resume is False
        assert reason == "meaningful_prior_attempt"

    def test_oversized_request_still_escalates(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("print('hello')\n")
        state["tasks"]["task-001"] = {
            "attempts_log": [
                {
                    "attempt": 1,
                    "tier": "primary",
                    "model": "m",
                    "aider_success": False,
                    "tests_passed": False,
                    "model_attempts": [{"model": "m", "reason": "request_too_large", "success": False}],
                }
            ]
        }

        should_resume, reason = _should_resume_existing_branch(
            state,
            "task-001",
            "src/example.py",
            cwd=str(tmp_path),
        )

        assert should_resume is False
        assert reason == "oversized_request"


class TestTestFileBugClassification:
    def test_requires_operator_hint(self):
        output = """
=================================== FAILURES ===================================
tests/test_001_example.py:12: in test_example
    assert value == 2
E   assert 1 == 2
=========================== short test summary info ============================
FAILED tests/test_001_example.py::test_example - assert 1 == 2
"""
        assert _classify_test_file_bug(output, "tests/test_001_example.py") is None

    def test_returns_hint_when_task_test_and_hint_are_present(self):
        output = """
=================================== FAILURES ===================================
tests/test_001_example.py:12: in test_example
    assert value == 2
E   assert 1 == 2
HINT: planner-written test expects the wrong literal value
=========================== short test summary info ============================
FAILED tests/test_001_example.py::test_example - assert 1 == 2
"""
        assert _classify_test_file_bug(
            output,
            "tests/test_001_example.py",
        ) == "planner-written test expects the wrong literal value"


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


class TestPytestFailureDigest:
    def test_extracts_compact_assertion_digest(self):
        output = """
=================================== FAILURES ===================================
________________________ test_maps_temperature ________________________
tests/test_weather.py:34: in test_maps_temperature
    assert response["temperature"] == 72.5
E   assert 0.0 == 72.5
=========================== short test summary info ============================
FAILED tests/test_weather.py::test_maps_temperature - assert 0.0 == 72.5
============================== 1 failed in 0.12s ===============================
"""
        digest = _pytest_failure_digest(output)

        assert "Failing test: tests/test_weather.py::test_maps_temperature" in digest
        assert 'Assertion: assert response["temperature"] == 72.5' in digest
        assert "Observed actual value: 0.0" in digest
        assert "Expected value: 72.5" in digest

    def test_returns_none_for_multi_failure_output(self):
        output = """
FAILED tests/test_one.py::test_a - assert 1 == 2
FAILED tests/test_two.py::test_b - assert 3 == 4
============================== 2 failed in 0.12s ===============================
"""
        assert _pytest_failure_digest(output) is None


class TestRunTierAttempts:
    def test_stops_early_when_no_remaining_tier_can_accept_request(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        ctx = {
            "baseline_size": 10,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "stopped_early": False,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 1})
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                False,
                None,
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
                [
                    {"model": "m1", "reason": "pre_screen_too_large", "success": False, "wall_seconds": 0.0},
                    {"model": "m2", "reason": "request_too_large", "success": False, "wall_seconds": 0.0},
                ],
            ),
        )
        heads = iter(["head-0", "head-0"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "_remaining_tiers_can_accept_request", lambda *args, **kwargs: False)

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            None,
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            [],
        )

        assert result == "terminal_failure"
        assert ctx["stopped_early"] is True
        assert ctx["terminal_failure_override"] == "request_too_large"
        assert "pre_screen_too_large" in ctx["llm_fail_reasons"]

    def test_continues_when_later_tier_can_accept_request(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        ctx = {
            "baseline_size": 10,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "stopped_early": False,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 1})
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                False,
                None,
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
                [{"model": "primary-model", "reason": "pre_screen_too_large", "success": False, "wall_seconds": 0.0}],
            ),
        )
        heads = iter(["head-0", "head-0"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "_remaining_tiers_can_accept_request", lambda *args, **kwargs: True)

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            None,
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            [],
        )

        assert result is None
        assert ctx["terminal_failure_override"] is None
        assert "pre_screen_too_large" in ctx["llm_fail_reasons"]

    def test_records_empty_target_file_as_recoverable_post_check(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        target = tmp_path / "src" / "app.py"
        target.parent.mkdir()
        target.write_text("")
        ctx = {
            "baseline_size": 0,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "stopped_early": False,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 1})
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 0.0}],
            ),
        )
        heads = iter(["head-0", "head-1"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "changed_files_between", lambda *args, **kwargs: ["src/app.py"])
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        reverted = {}
        monkeypatch.setattr(task_runner, "revert_last_commit", lambda *args, **kwargs: reverted.setdefault("called", True))

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            str(tmp_path),
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            [],
        )

        assert result is None
        assert reverted["called"] is True
        attempt = state["tasks"]["task-001"]["attempts_log"][0]
        assert attempt["post_check_reason"] == "empty_target_file"

    def test_collection_error_does_not_stop_remaining_retries(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        ctx = {
            "baseline_size": 10,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "stopped_early": False,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 2})
        calls = []
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                calls.append("attempt") or True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 1.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 1.0}],
            ),
        )
        heads = iter(["head-0", "head-0", "head-0", "head-0"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(
            task_runner,
            "run_tests",
            lambda *args, **kwargs: (False, "ERROR collecting tests/test_app.py\nImportError while importing test"),
        )
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            None,
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            [],
        )

        assert result is None
        assert calls == ["attempt", "attempt"]
        assert ctx["terminal_failure_override"] is None

    def test_stops_after_two_dependency_owned_forbidden_edits(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        ctx = {
            "baseline_size": 10,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 2})
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 1.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 1.0}],
            ),
        )
        heads = iter(["head-0", "head-1", "head-1", "head-2"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "changed_files_between", lambda *args, **kwargs: ["pom.xml"])
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        reverted = []
        monkeypatch.setattr(task_runner, "revert_last_commit", lambda *args, **kwargs: reverted.append(kwargs.get("reason")))

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            None,
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            ["pom.xml"],
        )

        assert result == "terminal_failure"
        assert ctx["dependency_forbidden_edit_count"] == 2
        assert "Reopen the dependency task" in ctx["failure_note"]
        assert len(reverted) == 2
        attempts = state["tasks"]["task-001"]["attempts_log"]
        assert len(attempts) == 2
        assert all(item["post_check_reason"] == "forbidden_file_edit" for item in attempts)
        assert all(item["forbidden_edit_subtype"] == "dependency_target" for item in attempts)

    def test_generic_forbidden_edit_does_not_trigger_dependency_stop(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        ctx = {
            "baseline_size": 10,
            "total_attempts": 0,
            "models_tried": [],
            "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
            "llm_fail_reasons": [],
            "had_successful_model_attempt": False,
            "last_forbidden_edit": None,
            "dependency_forbidden_edit_count": 0,
            "failure_note": None,
            "terminal_failure_override": None,
        }

        monkeypatch.setattr(task_runner, "get_tier", lambda _: {"retries": 1})
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 1.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 1.0}],
            ),
        )
        heads = iter(["head-0", "head-1"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "changed_files_between", lambda *args, **kwargs: ["README.md"])
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "revert_last_commit", lambda *args, **kwargs: None)

        result = _run_tier_attempts(
            "primary",
            1,
            lambda attempt: f"attempt {attempt}",
            "src/app.py",
            "tests/test_app.py",
            [],
            None,
            "task-001",
            state,
            False,
            "main",
            "primary-model",
            ctx,
            lambda: 0.1,
            "main",
            "base-sha",
            {"src/app.py"},
            ["pom.xml"],
        )

        assert result is None
        assert ctx["dependency_forbidden_edit_count"] == 0
        assert ctx["failure_note"] is None
        attempt = state["tasks"]["task-001"]["attempts_log"][0]
        assert attempt["forbidden_edit_subtype"] == "generic"

class TestRestoreRetryContext:
    def test_restores_dependency_forbidden_edit_history(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        state["tasks"]["task-001"] = {
            "attempts_log": [
                {
                    "aider_success": True,
                    "post_check_reason": "forbidden_file_edit",
                    "forbidden_files": ["pom.xml"],
                    "forbidden_edit_subtype": "dependency_target",
                    "model_attempts": [{"model": "m", "success": True, "reason": "ok"}],
                }
            ]
        }

        restored = _restore_retry_context(state, "task-001")

        assert restored["had_successful_model_attempt"] is True
        assert restored["dependency_forbidden_edit_count"] == 1
        assert restored["last_forbidden_edit"] == {
            "files": ["pom.xml"],
            "subtype": "dependency_target",
        }


class TestRunTaskVerificationStatus:
    def test_branch_check_raises_before_attempts_when_checkout_lands_on_wrong_branch(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "master")

        with pytest.raises(RuntimeError, match="expected 'task/task-001-example'"):
            run_task(spec, "main", state)

    def test_worktree_mode_skips_branch_check(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }

        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (True, ""))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)

        outcome = run_task(
            spec,
            "main",
            state,
            cwd=str(tmp_path),
            branch_preexisted=True,
        )

        assert outcome == "skipped"

    def test_existing_passing_branch_records_already_passing_status(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (True, ""))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)

        outcome = run_task(spec, "main", state)

        assert outcome == "skipped"
        assert state["tasks"]["task-001-example"]["verification_status"] == "already_passing_existing_branch"

    def test_existing_passing_branch_after_failure_records_recovered_status(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(state, "task-001-example", "failed", attempts=2, failure_class="assertion")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (True, ""))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)

        outcome = run_task(spec, "main", state)

        assert outcome == "skipped"
        assert state["tasks"]["task-001-example"]["verification_status"] == "recovered_after_prior_failure"

    def test_existing_failed_branch_with_empty_target_auto_resumes_without_resume_flag(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("")

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "resolve_dependency_base", lambda *args, **kwargs: ("main", []))
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: "sha-1")
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 0)
        monkeypatch.setattr(
            task_runner,
            "_build_read_context",
            lambda *args, **kwargs: ([str(spec["path"]), spec["test"]], [], []),
        )
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (False, "AssertionError: assert 1 == 2"))
        monkeypatch.setattr(task_runner, "reserve_log_path", lambda name: tmp_path / f"{name}.log")
        monkeypatch.setattr(task_runner, "write_timestamped_log", lambda *args, **kwargs: None)

        called = {}

        def fake_run_tier_attempts(*args, **kwargs):
            called["tier"] = args[0]
            return "terminal_failure"

        monkeypatch.setattr(task_runner, "_run_tier_attempts", fake_run_tier_attempts)
        monkeypatch.setattr(
            task_runner,
            "get_tier",
            lambda name: {"models": ["primary-model"], "retries": 1}
            if name == "primary"
            else {"models": ["escalation-model"], "retries": 0},
        )

        outcome = run_task(spec, "main", state, cwd=str(tmp_path), branch_preexisted=True)

        assert outcome == "failed"
        assert called["tier"] == "primary"

    def test_existing_failed_branch_with_meaningful_history_escalates(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        state["tasks"]["task-001-example"] = {
            "attempts_log": [
                {
                    "attempt": 1,
                    "tier": "primary",
                    "model": "primary-model",
                    "aider_success": True,
                    "tests_passed": False,
                    "model_attempts": [{"model": "primary-model", "reason": "ok", "success": True}],
                }
            ]
        }
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }
        target = tmp_path / "src" / "example.py"
        target.parent.mkdir()
        target.write_text("print('hello')\n")

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (False, "AssertionError: assert 1 == 2"))
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "reserve_log_path", lambda name: tmp_path / f"{name}.log")
        monkeypatch.setattr(task_runner, "write_timestamped_log", lambda *args, **kwargs: None)

        outcome = run_task(spec, "main", state, cwd=str(tmp_path), branch_preexisted=True)

        assert outcome == "failed"
        assert "meaningful_prior_attempt" in state["tasks"]["task-001-example"]["failure_note"]

class TestRunTaskFailureRouting:
    def test_early_stop_records_forbidden_file_edit_as_failure_class(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": ["task-000-dependency"],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "resolve_dependency_base", lambda *args, **kwargs: ("main", []))
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: "sha-1")
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            task_runner,
            "_build_read_context",
            lambda *args, **kwargs: ([str(spec["path"]), spec["test"]], [], []),
        )

        def fake_run_tier_attempts(*args, **kwargs):
            ctx = args[12]
            ctx["had_successful_model_attempt"] = True
            ctx["stopped_early"] = True
            ctx["terminal_failure_override"] = "forbidden_file_edit"
            ctx["failure_note"] = "Stopped after repeated dependency-owned forbidden edits."
            return "terminal_failure"

        monkeypatch.setattr(task_runner, "_run_tier_attempts", fake_run_tier_attempts)
        monkeypatch.setattr(
            task_runner,
            "get_tier",
            lambda name: {"models": ["primary-model"], "retries": 1}
            if name == "primary"
            else {"models": ["escalation-model"], "retries": 0},
        )
        monkeypatch.setattr(
            task_runner,
            "run_tests",
            lambda *args, **kwargs: (False, "AssertionError: assert 1 == 2"),
        )

        outcome = run_task(spec, "main", state, specs_by_name={"task-000-dependency": {"target": "pom.xml"}})

        assert outcome == "failed"
        entry = state["tasks"]["task-001-example"]
        assert entry["failure_class"] == "forbidden_file_edit"
        assert entry["test_failure_class"] == "assertion"

    def test_test_file_bug_stops_after_first_failing_attempt(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": [],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "resolve_dependency_base", lambda *args, **kwargs: ("main", []))
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: "sha-1")
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "reserve_log_path", lambda name: tmp_path / f"{name}.log")
        monkeypatch.setattr(task_runner, "write_timestamped_log", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            task_runner,
            "_build_read_context",
            lambda *args, **kwargs: ([str(spec["path"]), spec["test"]], [], []),
        )
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "changed_files_between", lambda *args, **kwargs: ["src/example.py"])
        monkeypatch.setattr(
            task_runner,
            "get_tier",
            lambda name: {"models": ["primary-model"], "retries": 2}
            if name == "primary"
            else {"models": ["escalation-model"], "retries": 1},
        )
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 0.0}],
            ),
        )
        monkeypatch.setattr(
            task_runner,
            "run_tests",
            lambda *args, **kwargs: (
                False,
                """tests/test_001_example.py:12: in test_example
    assert value == 2
E   assert 1 == 2
HINT: planner-written test expects the wrong literal value
=========================== short test summary info ============================
FAILED tests/test_001_example.py::test_example - assert 1 == 2
""",
            ),
        )

        outcome = run_task(spec, "main", state)

        assert outcome == "failed"
        entry = state["tasks"]["task-001-example"]
        assert entry["failure_class"] == "test_file_bug"
        assert "planner-written task test" in entry["failure_note"]

    def test_resume_counts_previous_dependency_forbidden_edit_before_retrying(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        state["tasks"]["task-001-example"] = {
            "attempts_log": [
                {
                    "attempt": 1,
                    "tier": "primary",
                    "model": "primary-model",
                    "aider_success": True,
                    "tests_passed": False,
                    "post_check_reason": "forbidden_file_edit",
                    "forbidden_files": ["pom.xml"],
                    "forbidden_edit_subtype": "dependency_target",
                    "model_attempts": [{"model": "primary-model", "success": True, "reason": "ok", "wall_seconds": 1.0}],
                }
            ]
        }
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": ["task-000-dependency"],
        }

        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        heads = iter(["base-sha", "head-before", "head-after"])
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: next(heads))
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(
            task_runner,
            "_build_read_context",
            lambda *args, **kwargs: ([str(spec["path"]), spec["test"]], ["pom.xml"], []),
        )
        monkeypatch.setattr(task_runner, "changed_files_between", lambda *args, **kwargs: ["pom.xml"])
        monkeypatch.setattr(task_runner, "check_regression", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "revert_last_commit", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            task_runner,
            "get_tier",
            lambda name: {"models": ["primary-model"], "retries": 2}
            if name == "primary"
            else {"models": ["escalation-model"], "retries": 0},
        )

        test_outputs = iter([
            (False, "still failing before resume"),
            (False, "still failing during retry prompt"),
            (False, "AssertionError: assert 1 == 2"),
        ])
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: next(test_outputs))
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda *args, **kwargs: (
                True,
                "primary-model",
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 1.0},
                [{"model": "primary-model", "reason": "ok", "success": True, "wall_seconds": 1.0}],
            ),
        )

        outcome = run_task(
            spec,
            "main",
            state,
            specs_by_name={"task-000-dependency": {"target": "pom.xml"}},
            resume=True,
        )

        assert outcome == "failed"
        entry = state["tasks"]["task-001-example"]
        assert entry["failure_class"] == "forbidden_file_edit"
        assert "Reopen the dependency task" in entry["failure_note"]

    def test_first_attempt_prompt_mentions_only_attached_dependency_targets(self, monkeypatch, tmp_path):
        state = load_state(tmp_path / "state.json")
        spec = {
            "task_name": "task-001-example",
            "target": "src/example.py",
            "test": "tests/test_001_example.py",
            "path": tmp_path / "specs" / "task-001-example.md",
            "dependencies": ["task-000-dependency", "task-000-config"],
        }

        captured_messages = []
        monkeypatch.setattr(task_runner, "branch_exists", lambda *args, **kwargs: False)
        monkeypatch.setattr(task_runner, "resolve_dependency_base", lambda *args, **kwargs: ("main", []))
        monkeypatch.setattr(task_runner, "branch_tip", lambda *args, **kwargs: "sha-1")
        monkeypatch.setattr(task_runner, "checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(task_runner, "current_branch", lambda *args, **kwargs: "task/task-001-example")
        monkeypatch.setattr(task_runner, "file_size", lambda *args, **kwargs: 10)
        monkeypatch.setattr(task_runner, "save_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            task_runner,
            "_build_read_context",
            lambda *args, **kwargs: ([str(spec["path"]), spec["test"], "pom.xml"], ["pom.xml"], ["application.yml"]),
        )
        def fake_get_tier(name):
            if name == "gemini":
                raise ValueError("no gemini tier")
            if name == "primary":
                return {"models": ["primary-model"], "retries": 1}
            return {"models": ["escalation-model"], "retries": 0}

        monkeypatch.setattr(task_runner, "get_tier", fake_get_tier)
        monkeypatch.setattr(
            task_runner,
            "run_with_tier_fallback",
            lambda tier_name, message, *args, **kwargs: (
                captured_messages.append(message) or False,
                None,
                {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0},
                [],
            ),
        )
        monkeypatch.setattr(task_runner, "run_tests", lambda *args, **kwargs: (False, "AssertionError: assert 1 == 2"))

        run_task(
            spec,
            "main",
            state,
            specs_by_name={
                "task-000-dependency": {"target": "pom.xml"},
                "task-000-config": {"target": "application.yml"},
            },
        )

        first_message = captured_messages[0]
        assert "Dependency target files are attached only as read-only context: pom.xml." in first_message
        assert "Dependency target files are attached only as read-only context: pom.xml, application.yml." not in first_message
        assert "Omitted upstream target files: application.yml." in first_message
