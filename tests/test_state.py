"""Tests for the orchestrator's state module."""

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.state import (
    load_state,
    save_state,
    record_task,
)


class TestState:
    def test_load_empty_state(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert "run_id" in state
        assert "tasks" in state
        assert state["tasks"] == {}

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = load_state(path)
        record_task(state, "task-001-foo", "passed", model="groq/openai/gpt-oss-20b", attempts=2)
        save_state(state, path)

        loaded = load_state(path)
        assert "task-001-foo" in loaded["tasks"]
        assert loaded["tasks"]["task-001-foo"]["status"] == "passed"
        assert loaded["tasks"]["task-001-foo"]["model"] == "groq/openai/gpt-oss-20b"
        assert loaded["tasks"]["task-001-foo"]["attempts"] == 2

    def test_record_multiple_tasks(self, tmp_path):
        state = load_state(tmp_path / "s.json")
        record_task(state, "task-001", "passed")
        record_task(state, "task-002", "failed", attempts=5)
        record_task(state, "task-003", "skipped")

        assert state["tasks"]["task-001"]["status"] == "passed"
        assert state["tasks"]["task-002"]["status"] == "failed"
        assert state["tasks"]["task-003"]["status"] == "skipped"

    def test_save_updates_timestamp(self, tmp_path):
        path = tmp_path / "state.json"
        state = load_state(path)
        original_time = state["updated_at"]
        save_state(state, path)
        loaded = load_state(path)
        # updated_at should be present and valid ISO format
        assert "updated_at" in loaded
        assert "T" in loaded["updated_at"]

    def test_state_file_is_valid_json(self, tmp_path):
        path = tmp_path / "state.json"
        state = load_state(path)
        record_task(state, "task-001", "passed")
        save_state(state, path)

        # Should be parseable JSON
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_skip_preserves_previous_attempt_metadata(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(
            state,
            "task-001",
            "failed",
            model="groq/openai/gpt-oss-20b",
            attempts=3,
            models_tried=["groq/openai/gpt-oss-20b"],
        )
        record_task(state, "task-001", "skipped", duration_seconds=0.2)

        assert state["tasks"]["task-001"]["status"] == "skipped"
        assert state["tasks"]["task-001"]["model"] == "groq/openai/gpt-oss-20b"
        assert state["tasks"]["task-001"]["attempts"] == 3

    def test_skip_can_store_verification_status(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(
            state,
            "task-001",
            "skipped",
            verification_status="already_passing_existing_branch",
        )

        assert state["tasks"]["task-001"]["verification_status"] == "already_passing_existing_branch"

    def test_recovered_task_clears_stale_failure_metadata(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(
            state,
            "task-001",
            "failed",
            failure_class="request_too_large",
            test_failure_class="assertion",
            llm_fail_reasons=["request_too_large"],
        )
        record_task(state, "task-001", "passed", attempts=4)

        entry = state["tasks"]["task-001"]
        assert entry["status"] == "passed"
        assert "failure_class" not in entry
        assert "test_failure_class" not in entry
        assert "llm_fail_reasons" not in entry

    def test_failed_task_clears_stale_verification_status(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(
            state,
            "task-001",
            "skipped",
            verification_status="recovered_after_prior_failure",
        )
        record_task(state, "task-001", "failed", failure_class="assertion")

        entry = state["tasks"]["task-001"]
        assert entry["status"] == "failed"
        assert "verification_status" not in entry

    def test_failed_task_clears_stale_failure_note_when_new_failure_has_none(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        record_task(
            state,
            "task-001",
            "failed",
            failure_class="merge_conflict",
            failure_note="Resolve the dependency graph first.",
        )
        record_task(
            state,
            "task-001",
            "failed",
            failure_class="assertion",
        )

        entry = state["tasks"]["task-001"]
        assert entry["status"] == "failed"
        assert entry["failure_class"] == "assertion"
        assert "failure_note" not in entry
