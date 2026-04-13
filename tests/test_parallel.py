"""Tests for orchestrator.parallel — wave grouping and concurrent execution."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.parallel import WorktreePool, find_parallel_groups


def _spec(name, deps=None):
    """Build a minimal spec dict for testing."""
    return {"task_name": name, "dependencies": deps or []}


class TestFindParallelGroups:
    def test_no_tasks_returns_empty(self):
        assert find_parallel_groups([]) == []

    def test_single_task_is_wave_0(self):
        groups = find_parallel_groups([_spec("A")])
        assert len(groups) == 1
        assert groups[0][0]["task_name"] == "A"

    def test_linear_chain_one_per_wave(self):
        # A → B → C: each must run after the previous
        specs = [_spec("A"), _spec("B", ["A"]), _spec("C", ["B"])]
        groups = find_parallel_groups(specs)
        assert len(groups) == 3
        assert [g[0]["task_name"] for g in groups] == ["A", "B", "C"]

    def test_fan_out_independent_tasks_in_same_wave(self):
        # A (no deps) and C (no deps) should both be in wave 0,
        # B (depends A) in wave 1 — this was the original bug.
        specs = [_spec("A"), _spec("B", ["A"]), _spec("C")]
        groups = find_parallel_groups(specs)
        assert len(groups) == 2
        wave0_names = {s["task_name"] for s in groups[0]}
        wave1_names = {s["task_name"] for s in groups[1]}
        assert wave0_names == {"A", "C"}
        assert wave1_names == {"B"}

    def test_diamond_shape(self):
        # A → B → D  and  A → C → D
        specs = [
            _spec("A"),
            _spec("B", ["A"]),
            _spec("C", ["A"]),
            _spec("D", ["B", "C"]),
        ]
        groups = find_parallel_groups(specs)
        assert len(groups) == 3
        assert {s["task_name"] for s in groups[0]} == {"A"}
        assert {s["task_name"] for s in groups[1]} == {"B", "C"}
        assert {s["task_name"] for s in groups[2]} == {"D"}

    def test_two_independent_chains(self):
        # Chain 1: A → B   Chain 2: C → D  (no cross-deps)
        specs = [_spec("A"), _spec("B", ["A"]), _spec("C"), _spec("D", ["C"])]
        groups = find_parallel_groups(specs)
        assert len(groups) == 2
        assert {s["task_name"] for s in groups[0]} == {"A", "C"}
        assert {s["task_name"] for s in groups[1]} == {"B", "D"}

    def test_all_independent_one_wave(self):
        specs = [_spec("A"), _spec("B"), _spec("C"), _spec("D")]
        groups = find_parallel_groups(specs)
        assert len(groups) == 1
        assert {s["task_name"] for s in groups[0]} == {"A", "B", "C", "D"}

    def test_dependency_not_in_specs_is_ignored(self):
        # A dep that references a task not in the list shouldn't crash.
        specs = [_spec("A"), _spec("B", ["MISSING"])]
        groups = find_parallel_groups(specs)
        # B references MISSING which is not in wave_of, so max() gets default -1
        # → B goes into wave 0 alongside A.
        assert len(groups) == 1
        assert {s["task_name"] for s in groups[0]} == {"A", "B"}

    def test_preserves_topo_order_within_wave(self):
        # Within a wave, the original topo order should be preserved.
        specs = [_spec("C"), _spec("A"), _spec("B")]
        groups = find_parallel_groups(specs)
        assert len(groups) == 1
        assert [s["task_name"] for s in groups[0]] == ["C", "A", "B"]


class TestWorktreePool:
    def test_default_base_dir_is_absolute_and_outside_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pool = WorktreePool()
        try:
            assert pool.base_dir.is_absolute()
            assert str(pool.base_dir).startswith(tempfile.gettempdir())
            assert tmp_path not in pool.base_dir.parents
        finally:
            pool.cleanup()
