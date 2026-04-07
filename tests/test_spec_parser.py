"""Tests for the orchestrator's spec_parser module."""

import sys
import os
import textwrap
from pathlib import Path

import pytest

# Add templates/ to path so we can import the orchestrator package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator.spec_parser import (
    parse_frontmatter,
    parse_target_file,
    load_spec,
    compress_spec,
    validate_specs,
    topological_sort,
)
from orchestrator import config as config_mod


@pytest.fixture(autouse=True)
def mock_config(monkeypatch, tmp_path):
    """Provide a default config for all tests."""
    config_yaml = tmp_path / "models.yaml"
    config_yaml.write_text(textwrap.dedent("""\
        tiers:
          - name: primary
            models: ["groq/qwen/qwen3-32b"]
            retries: 3
          - name: escalation
            models: ["groq/openai/gpt-oss-120b"]
            retries: 2
        weak_model: groq/llama-3.1-8b-instant
        spec_limits:
          soft_limit_chars: 100
          hard_limit_chars: 200
        cooldown_seconds: 0
    """))
    config_mod._config = None
    config_mod.load_config(config_yaml)
    yield
    config_mod._config = None


class TestParseFrontmatter:
    def test_parses_yaml_frontmatter(self):
        spec = textwrap.dedent("""\
            ---
            task: "001"
            name: "user-models"
            target: src/models/user.py
            test: tests/test_001_user_models.py
            dependencies: []
            ---

            # Task 001: User Models

            ## Functions to implement
            - create_user()
        """)
        meta, body = parse_frontmatter(spec)
        assert meta["task"] == "001"
        assert meta["name"] == "user-models"
        assert meta["target"] == "src/models/user.py"
        assert meta["test"] == "tests/test_001_user_models.py"
        assert meta["dependencies"] == []
        assert "# Task 001" in body

    def test_legacy_fallback_no_frontmatter(self):
        spec = textwrap.dedent("""\
            # Task 001: User Models

            ## Target file
            src/models/user.py

            ## Test file
            tests/test_001_user_models.py
        """)
        meta, body = parse_frontmatter(spec)
        assert meta["target"] == "src/models/user.py"
        assert meta["test"] == "tests/test_001_user_models.py"

    def test_empty_frontmatter(self):
        spec = "---\n---\n# Hello"
        meta, body = parse_frontmatter(spec)
        assert meta == {}
        assert "# Hello" in body


class TestParseTargetFile:
    def test_from_frontmatter(self):
        spec = "---\ntarget: src/foo.py\n---\n# Body"
        assert parse_target_file(spec) == "src/foo.py"

    def test_from_legacy_header(self):
        spec = "# Task\n\n## Target file\nsrc/bar.py\n"
        assert parse_target_file(spec) == "src/bar.py"

    def test_missing_returns_empty(self):
        spec = "# Task\n\n## Nothing here"
        assert parse_target_file(spec) == ""


class TestLoadSpec:
    def test_loads_with_frontmatter(self, tmp_path):
        spec_file = tmp_path / "task-001-models.md"
        spec_file.write_text(textwrap.dedent("""\
            ---
            task: "001"
            name: "models"
            target: src/models.py
            test: tests/test_001_models.py
            dependencies: []
            ---
            # Task 001
        """))
        spec = load_spec(spec_file)
        assert spec["task_name"] == "task-001-models"
        assert spec["task_id"] == "001"
        assert spec["target"] == "src/models.py"
        assert spec["test"] == "tests/test_001_models.py"
        assert spec["dependencies"] == []

    def test_derives_test_from_filename(self, tmp_path):
        spec_file = tmp_path / "task-002-auth.md"
        spec_file.write_text("---\ntarget: src/auth.py\n---\n# Body")
        spec = load_spec(spec_file)
        assert spec["test"] == "tests/test_002_auth.py"


class TestCompressSpec:
    def test_no_compression_under_soft_limit(self):
        spec = "short spec"
        assert compress_spec(spec, "task-001") == spec

    def test_strips_context_section(self):
        # Soft limit is 100 in our test config
        spec = "A" * 50 + "\n## Context\nLong context here\n## Next\n" + "B" * 60
        result = compress_spec(spec, "task-001")
        assert "## Context" not in result
        assert "Long context" not in result

    def test_hard_truncates_when_still_too_large(self):
        # Hard limit is 200 in our test config
        spec = "A" * 300
        result = compress_spec(spec, "task-001")
        assert len(result) == 200


class TestValidateSpecs:
    def test_empty_specs_dir(self, tmp_path):
        errors, warnings = validate_specs(tmp_path)
        assert len(errors) == 1
        assert "No spec files" in errors[0]

    def test_missing_target(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "task-001-test.md").write_text("---\ntest: tests/test_001_test.py\n---\n# Body")
        # Need the test file too
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_001_test.py").write_text("def test_x(): pass")
        errors, _ = validate_specs(specs)
        assert any("missing target" in e for e in errors)

    def test_valid_spec_passes(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        tests = tmp_path / "tests"
        tests.mkdir()
        (specs / "task-001-foo.md").write_text(
            "---\ntarget: src/foo.py\ntest: tests/test_001_foo.py\n---\n# Body"
        )
        (tests / "test_001_foo.py").write_text("def test_x(): pass")
        # Change cwd so relative test path resolves
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            errors, warnings = validate_specs(specs)
            assert len(errors) == 0
        finally:
            os.chdir(old_cwd)


class TestTopologicalSort:
    def test_sorts_by_dependencies(self, tmp_path):
        (tmp_path / "task-002-api.md").write_text(
            '---\ntarget: src/api.py\ntest: t.py\ndependencies: ["task-001-models"]\n---\n# Body'
        )
        (tmp_path / "task-001-models.md").write_text(
            "---\ntarget: src/models.py\ntest: t.py\ndependencies: []\n---\n# Body"
        )
        files = [tmp_path / "task-002-api.md", tmp_path / "task-001-models.md"]
        ordered = topological_sort(files)
        names = [o.stem for o in ordered]
        assert names.index("task-001-models") < names.index("task-002-api")

    def test_detects_cycle(self, tmp_path):
        (tmp_path / "task-001-a.md").write_text(
            '---\ntarget: a.py\ntest: t.py\ndependencies: ["task-002-b"]\n---\n'
        )
        (tmp_path / "task-002-b.md").write_text(
            '---\ntarget: b.py\ntest: t.py\ndependencies: ["task-001-a"]\n---\n'
        )
        files = list(tmp_path.glob("task-*.md"))
        with pytest.raises(ValueError, match="cycle"):
            topological_sort(files)

    def test_no_deps_preserves_alpha_order(self, tmp_path):
        (tmp_path / "task-001-a.md").write_text(
            "---\ntarget: a.py\ntest: t.py\n---\n"
        )
        (tmp_path / "task-002-b.md").write_text(
            "---\ntarget: b.py\ntest: t.py\n---\n"
        )
        files = sorted(tmp_path.glob("task-*.md"))
        ordered = topological_sort(files)
        assert [o.stem for o in ordered] == ["task-001-a", "task-002-b"]
