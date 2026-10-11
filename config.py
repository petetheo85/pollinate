"""Loads and validates configuration and environment settings for Pollinate."""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# override=False preserves variables already defined in the environment over .env file entries
load_dotenv(PROJECT_ROOT / ".env", override=False)

BIND_HOST = "127.0.0.1"

GEMINI_API_KEY_PLACEHOLDER = "your_gemini_api_key_here"
CAPTURE_TOKEN_PLACEHOLDER = "generate_a_token_and_paste_it_here"
# Minimum length ensures sufficient entropy for bearer authorization tokens
MIN_TOKEN_LENGTH = 24

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
CAPTURE_TOKEN = os.getenv("CAPTURE_TOKEN", "").strip()

SUPPORTED_MODELS = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
}

MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash").strip()
CAPTURE_PORT = int(os.getenv("CAPTURE_PORT", "8765"))

# Resolving against PROJECT_ROOT prevents split-brain SQLite files when processes run from different working directories
_env_db_path = os.getenv("DB_PATH", "").strip()
if _env_db_path:
    _raw_path = Path(_env_db_path)
    DB_PATH: Path = _raw_path if _raw_path.is_absolute() else (PROJECT_ROOT / _raw_path).resolve()
else:
    DB_PATH: Path = (PROJECT_ROOT / "pollinate.db").resolve()

# Derived weight guarantees weights sum to 1.0 without config drift
REL_WEIGHT = 0.6
OPP_WEIGHT = 1.0 - REL_WEIGHT

RETRY_LIMIT = 3
OUTREACH_MIN_COMMENTS = 2
OUTREACH_MIN_OPPORTUNITY_SCORE = 7
GROUNDING_FUZZY_THRESHOLD = 0.85
SQLITE_BUSY_TIMEOUT_MS = 5000

PROFILE_PATH = PROJECT_ROOT / "context" / "profile.md"


def validate_settings(
    api_key: Optional[str] = None,
    token: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Validates required credentials, token length, and model name at startup."""
    key_to_check = GEMINI_API_KEY if api_key is None else api_key.strip()
    token_to_check = CAPTURE_TOKEN if token is None else token.strip()
    model_to_check = MODEL_NAME if model is None else model.strip()

    if not key_to_check:
        raise ValueError(
            "GEMINI_API_KEY is missing or empty. "
            "Set GEMINI_API_KEY in your .env file or environment."
        )

    if key_to_check == GEMINI_API_KEY_PLACEHOLDER:
        raise ValueError(
            "GEMINI_API_KEY is still set to the placeholder value. "
            "Replace it with your actual Gemini API key in .env."
        )

    if not token_to_check:
        raise ValueError(
            "CAPTURE_TOKEN is missing or empty. "
            "Set CAPTURE_TOKEN in your .env file or environment."
        )

    if token_to_check == CAPTURE_TOKEN_PLACEHOLDER:
        raise ValueError(
            "CAPTURE_TOKEN is still set to the placeholder value. "
            "Generate a token using `python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and set it in .env."
        )

    if len(token_to_check) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"CAPTURE_TOKEN is too short ({len(token_to_check)} chars). "
            f"Expected at least {MIN_TOKEN_LENGTH} characters. "
            "Generate a secure token using `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`."
        )

    if not model_to_check:
        raise ValueError("MODEL_NAME cannot be empty.")

    if not (model_to_check.startswith("gemini-") or model_to_check in SUPPORTED_MODELS):
        raise ValueError(
            f"Configured MODEL_NAME '{model_to_check}' is not a recognized Gemini model name. "
            f"Expected a model like 'gemini-2.5-flash' or 'gemini-2.5-pro'."
        )
