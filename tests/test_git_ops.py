"""Tests for orchestrator.git_ops — git branch management."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.git_ops import (
    get_default_branch,
    ensure_default_branch_exists,
    branch_exists,
    changed_files_between,
    checkout,
    current_branch,
    merge_branch,
    delete_branch,
    branch_tip,
    validate_tracked_clean,
)
from orchestrator.task_runner import revert_last_commit


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Create a temporary git repo and cd into it."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    # Initial commit so we have a valid HEAD
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    return tmp_path


class TestGetDefaultBranch:
    def test_falls_back_to_current_branch(self, git_repo):
        # No origin, no init.defaultBranch config guaranteed, but current
        # branch is "main" which is not task/* or integration/*
        result = get_default_branch()
        assert result in ("main", "master")

    def test_ignores_task_branches(self, git_repo):
        checkout("task/foo", create=True)
        result = get_default_branch()
        # Should NOT return "task/foo"
        assert not result.startswith("task/")

    def test_ignores_integration_branches(self, git_repo):
        checkout("integration/run-test", create=True)
        result = get_default_branch()
        assert not result.startswith("integration/")


class TestEnsureDefaultBranchExists:
    def test_noop_when_commits_exist(self, git_repo):
        # Already has a commit from fixture
        tip_before = branch_tip("HEAD")
        ensure_default_branch_exists()
        tip_after = branch_tip("HEAD")
        assert tip_before == tip_after

    def test_creates_initial_commit_in_empty_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        ensure_default_branch_exists()
        # Should now have at least one commit
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0


class TestBranchExists:
    def test_existing_branch(self, git_repo):
        assert branch_exists("main") is True

    def test_nonexistent_branch(self, git_repo):
        assert branch_exists("no-such-branch") is False


class TestCheckout:
    def test_create_and_switch(self, git_repo):
        checkout("feature-x", create=True)
        assert current_branch() == "feature-x"

    def test_switch_existing_branch(self, git_repo):
        checkout("feature-y", create=True)
        checkout("main")
        assert current_branch() == "main"
        checkout("feature-y")
        assert current_branch() == "feature-y"

    def test_create_from_start_point(self, git_repo):
        # Add a commit on main
        (git_repo / "file.txt").write_text("content\n")
        subprocess.run(["git", "add", "file.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file"], check=True, capture_output=True)
        main_tip = branch_tip("main")

        checkout("from-main", create=True, start_point="main")
        assert branch_tip("HEAD") == main_tip


class TestMergeBranch:
    def test_clean_merge(self, git_repo):
        checkout("feature", create=True)
        (git_repo / "new.txt").write_text("hello\n")
        subprocess.run(["git", "add", "new.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add new"], check=True, capture_output=True)

        checkout("main")
        result = merge_branch("feature")
        assert result is True
        assert (git_repo / "new.txt").exists()

    def test_merge_conflict_returns_false(self, git_repo):
        # Create diverging changes on the same file
        (git_repo / "conflict.txt").write_text("original\n")
        subprocess.run(["git", "add", "conflict.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], check=True, capture_output=True)

        checkout("branch-a", create=True)
        (git_repo / "conflict.txt").write_text("branch-a change\n")
        subprocess.run(["git", "add", "conflict.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "a change"], check=True, capture_output=True)

        checkout("main")
        (git_repo / "conflict.txt").write_text("main change\n")
        subprocess.run(["git", "add", "conflict.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "main change"], check=True, capture_output=True)

        result = merge_branch("branch-a")
        assert result is False
        # Working tree should be clean (merge aborted)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
        )
        assert status.stdout.strip() == ""


class TestDeleteBranch:
    def test_delete_merged_branch(self, git_repo):
        checkout("to-delete", create=True)
        checkout("main")
        delete_branch("to-delete")
        assert branch_exists("to-delete") is False

    def test_force_delete_unmerged(self, git_repo):
        checkout("unmerged", create=True)
        (git_repo / "x.txt").write_text("x\n")
        subprocess.run(["git", "add", "x.txt"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "x"], check=True, capture_output=True)
        checkout("main")
        delete_branch("unmerged", force=True)
        assert branch_exists("unmerged") is False


class TestBranchTip:
    def test_valid_branch(self, git_repo):
        tip = branch_tip("main")
        assert len(tip) == 40  # full SHA

    def test_nonexistent_branch(self, git_repo):
        assert branch_tip("no-such-branch") == ""


class TestChangedFilesBetween:
    def test_reports_repo_relative_changed_files(self, git_repo):
        before = branch_tip("HEAD")
        (git_repo / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "src.py"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], check=True, capture_output=True)
        after = branch_tip("HEAD")

        assert changed_files_between(before, after) == ["src.py"]


class TestValidateTrackedClean:
    def test_clean_tracked_paths_pass(self, git_repo):
        p = git_repo / "specs"
        p.mkdir()
        spec = p / "task-001.md"
        spec.write_text("hello\n")
        subprocess.run(["git", "add", "specs/task-001.md"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add spec"], check=True, capture_output=True)

        assert validate_tracked_clean(["specs/task-001.md"]) == []

    def test_untracked_path_fails(self, git_repo):
        p = git_repo / "specs"
        p.mkdir()
        (p / "task-001.md").write_text("hello\n")

        errors = validate_tracked_clean(["specs/task-001.md"])
        assert len(errors) == 1
        assert "not tracked by git" in errors[0]

    def test_dirty_tracked_path_fails(self, git_repo):
        p = git_repo / "tests"
        p.mkdir()
        test_file = p / "test_task.py"
        test_file.write_text("def test_ok():\n    assert True\n")
        subprocess.run(["git", "add", "tests/test_task.py"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test"], check=True, capture_output=True)

        test_file.write_text("def test_ok():\n    assert False\n")
        errors = validate_tracked_clean(["tests/test_task.py"])
        assert len(errors) == 1
        assert "uncommitted changes" in errors[0]


class TestRevertLastCommit:
    def test_reverts_to_previous_state(self, git_repo):
        f = git_repo / "src.py"
        f.write_text("import os\n\ndef foo():\n    pass\n")
        subprocess.run(["git", "add", "src.py"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], check=True, capture_output=True)
        baseline = os.path.getsize(str(f))

        f.write_text("x\n")
        subprocess.run(["git", "add", "src.py"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "shrink"], check=True, capture_output=True)

        revert_last_commit(str(f), baseline)
        # File should be restored
        assert f.read_text() == "import os\n\ndef foo():\n    pass\n"
