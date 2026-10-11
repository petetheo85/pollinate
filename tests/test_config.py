"""Tests for configuration path resolution, setting validation, precedence, and static repo constraints."""

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import dotenv
import pytest

import config


@pytest.fixture
def clean_config_env():
    """Isolates tests from environment mutations and asserts defaults on teardown."""
    with mock.patch.dict(os.environ, clear=True), mock.patch("dotenv.load_dotenv"):
        yield
    # Reload config only after os.environ is fully restored, isolated from real .env
    with mock.patch.dict(os.environ, clear=True), mock.patch("dotenv.load_dotenv"):
        importlib.reload(config)
        assert config.CAPTURE_PORT == 8765
        assert config.MODEL_NAME is None
        assert config.DB_PATH == (config.PROJECT_ROOT / "pollinate.db").resolve()


def test_resolve_db_path_function(tmp_path):
    # Default when empty or None
    assert config.resolve_db_path(None) == (config.PROJECT_ROOT / "pollinate.db").resolve()
    assert config.resolve_db_path("") == (config.PROJECT_ROOT / "pollinate.db").resolve()
    assert config.resolve_db_path("   ") == (config.PROJECT_ROOT / "pollinate.db").resolve()

    # Relative path always resolves against PROJECT_ROOT
    rel_expected = (config.PROJECT_ROOT / "custom/test.db").resolve()
    assert config.resolve_db_path("custom/test.db") == rel_expected

    # Absolute path preserves target location using tmp_path
    abs_path = (tmp_path / "absolute_test.db").resolve()
    assert config.resolve_db_path(str(abs_path)) == abs_path


def test_db_path_in_subprocess_from_different_cwd(tmp_path):
    cmd = [
        sys.executable,
        "-c",
        "import config; print(str(config.DB_PATH))",
    ]
    env = dict(os.environ)
    env.pop("DB_PATH", None)
    env["PYTHONPATH"] = str(config.PROJECT_ROOT)
    proc = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    resolved_stdout = proc.stdout.strip()
    expected_path = str((config.PROJECT_ROOT / "pollinate.db").resolve())
    assert resolved_stdout == expected_path


def test_env_reload_precedence(clean_config_env):
    os.environ["CAPTURE_PORT"] = "9000"
    os.environ["MODEL_NAME"] = "custom-reasoning-model"
    os.environ["DB_PATH"] = "env_override.db"

    importlib.reload(config)

    assert config.CAPTURE_PORT == 9000
    assert config.MODEL_NAME == "custom-reasoning-model"
    assert config.DB_PATH == (config.PROJECT_ROOT / "env_override.db").resolve()


def test_precedence_env_over_dotenv_over_defaults(clean_config_env, tmp_path, monkeypatch):
    # Layer 1: Config defaults when neither env nor .env specifies values
    importlib.reload(config)
    assert config.CAPTURE_PORT == 8765
    assert config.MODEL_NAME is None

    # Layer 2: .env file overrides defaults
    fake_dotenv = tmp_path / "test.env"
    fake_dotenv.write_text("CAPTURE_PORT=8888\nMODEL_NAME=model-from-dotenv\n", encoding="utf-8")

    def load_fake_dotenv(dotenv_path=None, override=False):  # noqa: ARG001
        dotenv.main.load_dotenv(fake_dotenv, override=override)

    monkeypatch.setattr("dotenv.load_dotenv", load_fake_dotenv)
    importlib.reload(config)
    assert config.CAPTURE_PORT == 8888
    assert config.MODEL_NAME == "model-from-dotenv"

    # Layer 3: Shell environment overrides .env values
    os.environ["CAPTURE_PORT"] = "9999"
    importlib.reload(config)
    assert config.CAPTURE_PORT == 9999
    # Unset env var retains .env value
    assert config.MODEL_NAME == "model-from-dotenv"


def test_subsequent_test_sees_default_values(clean_config_env):
    """Verifies that a test running after precedence tests observes clean restored defaults."""
    assert config.CAPTURE_PORT == 8765
    assert config.MODEL_NAME is None
    assert config.DB_PATH == (config.PROJECT_ROOT / "pollinate.db").resolve()


def test_validate_settings_token_and_key_boundaries():
    valid_key = "FakeKeyValid1234567890123456"
    valid_model = "custom-test-model"

    # Token length boundary: 23 rejected, 24 accepted
    token_23 = "a" * 23
    token_24 = "a" * 24

    with pytest.raises(ValueError, match="CAPTURE_TOKEN is too short"):
        config.validate_settings(api_key=valid_key, token=token_23, model=valid_model)

    # 24 chars succeeds
    config.validate_settings(api_key=valid_key, token=token_24, model=valid_model)

    # Whitespace-only values rejected
    with pytest.raises(ValueError, match="CAPTURE_TOKEN is missing or empty"):
        config.validate_settings(api_key=valid_key, token="   ", model=valid_model)

    with pytest.raises(ValueError, match="LLM_API_KEY is missing or empty"):
        config.validate_settings(api_key="   ", token=token_24, model=valid_model)


def test_validate_settings_model_name_boundaries():
    valid_key = "FakeKeyValid1234567890123456"
    valid_token = "a" * 24

    # MODEL_NAME missing, empty, or whitespace-only rejected
    with pytest.raises(ValueError, match="MODEL_NAME is missing or empty"):
        config.validate_settings(api_key=valid_key, token=valid_token, model="")

    with pytest.raises(ValueError, match="MODEL_NAME is missing or empty"):
        config.validate_settings(api_key=valid_key, token=valid_token, model="   ")


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

    assert "LLM_API_KEY" in placeholders
    assert "MODEL_NAME" in placeholders
    assert "CAPTURE_TOKEN" in placeholders

    with pytest.raises(ValueError, match="placeholder"):
        config.validate_settings(api_key=placeholders["LLM_API_KEY"], token="a" * 32, model="custom-model")

    with pytest.raises(ValueError, match="placeholder"):
        config.validate_settings(api_key="valid_key_12345", token=placeholders["CAPTURE_TOKEN"], model="custom-model")

    with pytest.raises(ValueError, match="placeholder"):
        config.validate_settings(api_key="valid_key_12345", token="a" * 32, model=placeholders["MODEL_NAME"])


@pytest.mark.anyio
async def test_startup_behavioral_validation(monkeypatch, tmp_path):
    import capture_server

    valid_token = "a" * 32
    valid_key = "valid_key_1234567890123456"
    valid_model = "test-reasoning-model"

    # 1. Missing LLM_API_KEY (with valid token and model)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "CAPTURE_TOKEN", valid_token)
    monkeypatch.setattr(config, "MODEL_NAME", valid_model)
    with pytest.raises(ValueError, match="LLM_API_KEY is missing or empty"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 2. Placeholder LLM_API_KEY (with valid token and model)
    monkeypatch.setattr(config, "LLM_API_KEY", config.LLM_API_KEY_PLACEHOLDER)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", valid_token)
    monkeypatch.setattr(config, "MODEL_NAME", valid_model)
    with pytest.raises(ValueError, match="LLM_API_KEY is still set to the placeholder value"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 3. Missing CAPTURE_TOKEN (with valid key and model)
    monkeypatch.setattr(config, "LLM_API_KEY", valid_key)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", "")
    monkeypatch.setattr(config, "MODEL_NAME", valid_model)
    with pytest.raises(ValueError, match="CAPTURE_TOKEN is missing or empty"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 4. Placeholder CAPTURE_TOKEN (with valid key and model)
    monkeypatch.setattr(config, "LLM_API_KEY", valid_key)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", config.CAPTURE_TOKEN_PLACEHOLDER)
    monkeypatch.setattr(config, "MODEL_NAME", valid_model)
    with pytest.raises(ValueError, match="CAPTURE_TOKEN is still set to the placeholder value"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 5. Missing MODEL_NAME (with valid key and token)
    monkeypatch.setattr(config, "LLM_API_KEY", valid_key)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", valid_token)
    monkeypatch.setattr(config, "MODEL_NAME", None)
    with pytest.raises(ValueError, match="MODEL_NAME is missing or empty"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 6. Placeholder MODEL_NAME (with valid key and token)
    monkeypatch.setattr(config, "LLM_API_KEY", valid_key)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", valid_token)
    monkeypatch.setattr(config, "MODEL_NAME", config.MODEL_NAME_PLACEHOLDER)
    with pytest.raises(ValueError, match="MODEL_NAME is still set to the placeholder value"):
        async with capture_server.lifespan(capture_server.app):
            pass

    # 7. Valid settings allow clean startup and database initialization
    test_db = tmp_path / "startup_test.db"
    monkeypatch.setattr(config, "LLM_API_KEY", valid_key)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", valid_token)
    monkeypatch.setattr(config, "MODEL_NAME", valid_model)
    monkeypatch.setattr(config, "DB_PATH", test_db)

    async with capture_server.lifespan(capture_server.app):
        assert test_db.exists()


def test_static_check_no_environ_reads_outside_config():
    """Statically verifies that only config.py reads environment variables."""
    violations = []

    for py_file in config.PROJECT_ROOT.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == Path(__file__).resolve():
            continue
        if ".venv" in resolved.parts or "venv" in resolved.parts or "tests" in resolved.parts:
            continue
        if resolved.name == "config.py":
            continue

        with open(resolved, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(resolved))

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in {"environ", "getenv"}:
                    if isinstance(node.value, ast.Name) and node.value.id == "os":
                        violations.append(f"{py_file.name}:{node.lineno} accesses os.{node.attr}")

            elif isinstance(node, ast.ImportFrom):
                if node.module == "os":
                    for alias in node.names:
                        if alias.name in {"environ", "getenv"}:
                            violations.append(f"{py_file.name}:{node.lineno} imports {alias.name} from os")
                elif node.module == "dotenv":
                    violations.append(f"{py_file.name}:{node.lineno} imports from dotenv")

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "dotenv":
                        violations.append(f"{py_file.name}:{node.lineno} imports dotenv")

    assert not violations, f"Environment access violations found: {violations}"


def test_static_check_no_browser_automation_imports():
    """Statically verifies that no Python file imports browser automation libraries."""
    violations = []
    banned_modules = {"playwright", "selenium", "pyppeteer", "puppeteer"}

    for py_file in config.PROJECT_ROOT.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == Path(__file__).resolve():
            continue
        if ".venv" in resolved.parts or "venv" in resolved.parts:
            continue

        with open(resolved, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(resolved))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg in banned_modules:
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    if root_pkg in banned_modules:
                        violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")

    assert not violations, f"Browser automation imports found: {violations}"



def test_bind_host_and_server_binding():
    # Listeners bind to 127.0.0.1 and this is not configurable
    assert config.BIND_HOST == "127.0.0.1"

    server_path = config.PROJECT_ROOT / "capture_server.py"
    with open(server_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(server_path))

    uvicorn_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
            ):
                uvicorn_calls.append(node)

    assert len(uvicorn_calls) == 1, "Expected exactly one uvicorn.run call in capture_server.py"
    call = uvicorn_calls[0]
    host_kw = next((kw for kw in call.keywords if kw.arg == "host"), None)
    assert host_kw is not None, "uvicorn.run call must specify host keyword argument"

    # Host expression must be BIND_HOST or the literal '127.0.0.1'
    is_bind_host = isinstance(host_kw.value, ast.Name) and host_kw.value.id == "BIND_HOST"
    is_literal_localhost = isinstance(host_kw.value, ast.Constant) and host_kw.value.value == "127.0.0.1"
    assert is_bind_host or is_literal_localhost, (
        f"Host argument must be BIND_HOST or '127.0.0.1', got {ast.dump(host_kw.value)}"
    )
