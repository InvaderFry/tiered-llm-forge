"""Tests for orchestrator.preflight."""

import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator import config as config_mod
from orchestrator.model_router import _estimate_request_tokens
import orchestrator.preflight as preflight
from orchestrator.preflight import (
    maybe_prime_maven_cache,
    normalize_provider_env,
    run_startup_preflight,
    validate_config,
    validate_pytest_collection,
    validate_provider_env,
    validate_runtime_prereqs,
)


@pytest.fixture
def loaded_config(tmp_path):
    config_yaml = tmp_path / "models.yaml"
    config_yaml.write_text(textwrap.dedent("""\
        tiers:
          - name: primary
            models:
              - groq/openai/gpt-oss-20b
            retries: 3
          - name: escalation
            models:
              - groq/openai/gpt-oss-120b
              - groq/llama-3.3-70b-versatile
            retries: 1
          - name: gemini
            models:
              - gemini/gemini-2.5-flash
            retries: 1
        weak_model: groq/llama-3.1-8b-instant
    """))
    config_mod._config = None
    yield config_mod.load_config(config_yaml)
    config_mod._config = None
    preflight._WARMED_MAVEN_ROOTS.clear()


def test_normalize_provider_env_mirrors_legacy_alias(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")

    warnings = normalize_provider_env()

    assert os.environ["GOOGLE_API_KEY"] == "legacy-key"
    assert warnings


def test_validate_provider_env_accepts_legacy_gemini_alias(monkeypatch, loaded_config):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")

    errors, warnings = validate_provider_env(loaded_config)

    assert errors == []
    assert os.environ["GOOGLE_API_KEY"] == "legacy-key"


def test_validate_provider_env_reports_missing_keys(monkeypatch, loaded_config):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    errors, warnings = validate_provider_env(loaded_config)

    assert any("GROQ_API_KEY" in err for err in errors)
    assert any("GOOGLE_API_KEY" in err for err in errors)


@pytest.mark.parametrize(
    "tier_name,model_id",
    [
        ("primary", "groq/moonshotai/kimi-k2-instruct"),
        ("gemini", "gemini/gemini-3.0-flash"),
        ("gemini", "gemini/gemini-3-flash-preview"),
        ("gemini", "gemini/gemini-2.5-pro"),
        ("gemini", "gemini/gemini-3.1-pro"),
    ],
)
def test_validate_config_rejects_known_bad_model_id(tier_name, model_id):
    cfg = {"tiers": [{"name": tier_name, "models": [model_id], "retries": 1}]}

    errors, warnings = validate_config(cfg)

    assert any(model_id in err for err in errors)


def test_template_model_configs_validate():
    repo_root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((repo_root / "templates" / "models.yaml").read_text())
    errors, warnings = validate_config(cfg)
    assert errors == []


def test_template_groq_caps_clear_default_context_budget(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((repo_root / "templates" / "models.yaml").read_text())
    context_limits = cfg["context_limits"]
    max_total_bytes = int(context_limits["max_total_bytes"])

    spec_file = tmp_path / "task.md"
    spec_file.write_text("s" * 16_000)
    test_file = tmp_path / "test_task.py"
    test_file.write_text("t" * (max_total_bytes - 16_000))
    target_file = tmp_path / "target.py"
    target_file.write_text("x" * 4_096)

    message = (
        f"Implement the task described in {spec_file.name}. "
        f"All requirements (function signatures, types, constraints) live in "
        f"that file. Edit {target_file.name} so that the tests in {test_file.name} pass. "
        f"Do not modify the test file."
    )
    estimated_tokens = _estimate_request_tokens(
        str(target_file),
        message,
        read_files=[str(spec_file), str(test_file)],
    )

    caps = {}
    for tier in cfg["tiers"]:
        for model in tier["models"]:
            if isinstance(model, dict):
                caps[model["id"]] = model.get("max_input_tokens")

    assert caps["groq/openai/gpt-oss-20b"] > estimated_tokens
    assert caps["groq/openai/gpt-oss-120b"] > estimated_tokens
    assert caps["groq/llama-3.3-70b-versatile"] > estimated_tokens
    assert caps["gemini/gemini-2.5-flash"] > estimated_tokens


def test_validate_config_warns_on_duplicate_model_across_tiers():
    cfg = {
        "tiers": [
            {"name": "escalation", "models": ["groq/openai/gpt-oss-120b"], "retries": 1},
            {"name": "gemini", "models": ["groq/openai/gpt-oss-120b"], "retries": 1},
        ]
    }

    errors, warnings = validate_config(cfg)

    assert errors == []
    assert any("groq/openai/gpt-oss-120b" in warning for warning in warnings)


def test_validate_runtime_prereqs_reports_missing_aider(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None if name == "aider" else "/usr/bin/ok")

    errors, warnings = validate_runtime_prereqs(repo_root=tmp_path)

    assert any("aider" in err for err in errors)


def test_validate_pytest_collection_skips_empty_input(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    errors, warnings = validate_pytest_collection([], repo_root=tmp_path)

    assert errors == []
    assert warnings == []
    assert calls == []


def test_validate_pytest_collection_reports_collection_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "returncode": 2,
                "stdout": "ERROR collecting tests/test_005_weather.py\n",
                "stderr": "",
            },
        )(),
    )

    errors, warnings = validate_pytest_collection(["tests/test_005_weather.py"], repo_root=tmp_path)

    assert warnings == []
    assert len(errors) == 1
    assert "tests/test_005_weather.py" in errors[0]


def test_validate_pytest_collection_turns_timeout_into_warning(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise preflight.subprocess.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    errors, warnings = validate_pytest_collection(["tests/test_001_example.py"], repo_root=tmp_path)

    assert errors == []
    assert len(warnings) == 1
    assert "timed out" in warnings[0]


def test_validate_runtime_prereqs_warms_maven_when_pom_exists(monkeypatch, tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>\n")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/ok")
    calls = []
    monkeypatch.setattr(
        preflight,
        "maybe_prime_maven_cache",
        lambda repo_root, reason=None: calls.append((str(repo_root), reason)) or (True, ""),
    )

    errors, warnings = validate_runtime_prereqs(repo_root=tmp_path)

    assert errors == []
    assert calls == [(str(tmp_path.resolve()), "startup preflight")]


def test_run_startup_preflight_includes_runtime_errors(monkeypatch, loaded_config, tmp_path):
    monkeypatch.setattr(preflight, "validate_runtime_prereqs", lambda repo_root=None: (["runtime bad"], []))
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    errors, warnings = run_startup_preflight(repo_root=tmp_path)

    assert "runtime bad" in errors


def test_maybe_prime_maven_cache_is_synchronized(monkeypatch, tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>\n")
    preflight._WARMED_MAVEN_ROOTS.clear()
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs["cwd"])
        time.sleep(0.05)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    threads = [
        threading.Thread(target=maybe_prime_maven_cache, args=(tmp_path, f"thread-{i}"))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [str(tmp_path.resolve())]
