"""Tests for configuration resolution, validation, precedence, and environment hygiene."""

import os
from pathlib import Path
import pytest
from sqlalchemy.engine import Engine

import config
from db import get_engine


def test_db_path_absolute_and_independent_of_cwd(tmp_path):
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert Path.cwd() == tmp_path

        assert config.DB_PATH.is_absolute()
        assert config.DB_PATH == (config.PROJECT_ROOT / "pollinate.db").resolve()

        engine = get_engine()
        assert isinstance(engine, Engine)
        expected_db_file = str((config.PROJECT_ROOT / "pollinate.db").resolve())
        assert expected_db_file in str(engine.url)
    finally:
        os.chdir(original_cwd)


def test_relative_db_path_from_env_resolves_against_project_root(tmp_path, monkeypatch):
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        rel_name = "test_sub/custom.db"
        monkeypatch.setenv("DB_PATH", rel_name)

        raw_path = Path(os.getenv("DB_PATH"))
        resolved = raw_path if raw_path.is_absolute() else (config.PROJECT_ROOT / raw_path).resolve()
        assert resolved == (config.PROJECT_ROOT / "test_sub/custom.db").resolve()
        assert tmp_path not in resolved.parents
    finally:
        os.chdir(original_cwd)


def test_validate_settings_success():
    valid_key = "AIzaSyFakeKeyForTesting1234567890"
    valid_token = "a" * 32
    config.validate_settings(api_key=valid_key, token=valid_token, model="gemini-2.5-flash")


def test_validate_settings_rejects_missing_empty_short_and_invalid():
    valid_key = "AIzaSyFakeKeyForTesting1234567890"
    valid_token = "b" * 32

    with pytest.raises(ValueError, match="GEMINI_API_KEY is missing or empty"):
        config.validate_settings(api_key="", token=valid_token)

    with pytest.raises(ValueError, match="GEMINI_API_KEY is still set to the placeholder"):
        config.validate_settings(api_key=config.GEMINI_API_KEY_PLACEHOLDER, token=valid_token)

    with pytest.raises(ValueError, match="CAPTURE_TOKEN is missing or empty"):
        config.validate_settings(api_key=valid_key, token="")

    with pytest.raises(ValueError, match="CAPTURE_TOKEN is still set to the placeholder"):
        config.validate_settings(api_key=valid_key, token=config.CAPTURE_TOKEN_PLACEHOLDER)

    with pytest.raises(ValueError, match="CAPTURE_TOKEN is too short"):
        config.validate_settings(api_key=valid_key, token="too_short_secret")

    with pytest.raises(ValueError, match="not a recognized Gemini model name"):
        config.validate_settings(api_key=valid_key, token=valid_token, model="gpt-4o")

    with pytest.raises(ValueError, match="MODEL_NAME cannot be empty"):
        config.validate_settings(api_key=valid_key, token=valid_token, model="")


def test_validate_settings_rejects_env_example_placeholders():
    env_example_path = config.PROJECT_ROOT / ".env.example"
    assert env_example_path.exists()

    placeholders = {}
    with open(env_example_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                placeholders[k.strip()] = v.strip()

    assert "GEMINI_API_KEY" in placeholders
    assert "CAPTURE_TOKEN" in placeholders

    with pytest.raises(ValueError, match="placeholder"):
        config.validate_settings(
            api_key=placeholders["GEMINI_API_KEY"],
            token="a" * 32,
        )

    with pytest.raises(ValueError, match="placeholder"):
        config.validate_settings(
            api_key="AIzaSyFakeKeyForTesting1234567890",
            token=placeholders["CAPTURE_TOKEN"],
        )


def test_static_check_no_env_reads_outside_config():
    forbidden_tokens = ["os.environ", "os.getenv", "dotenv"]
    this_file = Path(__file__).resolve()

    violations = []
    for py_file in config.PROJECT_ROOT.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == this_file:
            continue
        if ".venv" in resolved.parts or "venv" in resolved.parts:
            continue
        if resolved.name == "config.py":
            continue

        with open(resolved, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for token in forbidden_tokens:
                    if token in line:
                        violations.append(f"{py_file.relative_to(config.PROJECT_ROOT)}:{line_no} contains '{token}'")

    assert not violations, f"Environment reads found outside config.py: {violations}"


def test_precedence_env_overrides_dotenv_overrides_defaults(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CAPTURE_PORT=9999\nMODEL_NAME=gemini-2.0-flash\n", encoding="utf-8")

    from dotenv import load_dotenv

    monkeypatch.delenv("CAPTURE_PORT", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    load_dotenv(env_file, override=False)
    assert os.getenv("CAPTURE_PORT") == "9999"
    assert os.getenv("MODEL_NAME") == "gemini-2.0-flash"

    monkeypatch.setenv("CAPTURE_PORT", "1234")
    load_dotenv(env_file, override=False)
    assert os.getenv("CAPTURE_PORT") == "1234"
