"""Tests for orchestrator.preflight."""

import os
import sys
import textwrap
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "templates"))

from orchestrator import config as config_mod
import orchestrator.preflight as preflight
from orchestrator.preflight import (
    maybe_prime_maven_cache,
    normalize_provider_env,
    run_startup_preflight,
    validate_config,
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
              - groq/qwen/qwen3-32b
            retries: 3
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


def test_validate_config_rejects_known_bad_model_id():
    cfg = {
        "tiers": [
            {"name": "gemini", "models": ["gemini/gemini-3.0-flash"], "retries": 1},
        ]
    }

    errors, warnings = validate_config(cfg)

    assert any("gemini/gemini-3.0-flash" in err for err in errors)


def test_validate_runtime_prereqs_reports_missing_aider(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None if name == "aider" else "/usr/bin/ok")

    errors, warnings = validate_runtime_prereqs(repo_root=tmp_path)

    assert any("aider" in err for err in errors)


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
