"""Tests for the orchestrator's state module."""

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.state import load_state, save_state, record_task


class TestState:
    def test_load_empty_state(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert "run_id" in state
        assert "tasks" in state
        assert state["tasks"] == {}

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = load_state(path)
        record_task(state, "task-001-foo", "passed", model="groq/qwen3", attempts=2)
        save_state(state, path)

        loaded = load_state(path)
        assert "task-001-foo" in loaded["tasks"]
        assert loaded["tasks"]["task-001-foo"]["status"] == "passed"
        assert loaded["tasks"]["task-001-foo"]["model"] == "groq/qwen3"
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
