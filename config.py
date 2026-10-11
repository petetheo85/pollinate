"""Loads and validates configuration and environment settings for Pollinate."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# override=False preserves variables already defined in the environment over .env file entries
load_dotenv(PROJECT_ROOT / ".env", override=False)

BIND_HOST = "127.0.0.1"

LLM_API_KEY_PLACEHOLDER = "your_llm_api_key_here"
MODEL_NAME_PLACEHOLDER = "your_model_name_here"
CAPTURE_TOKEN_PLACEHOLDER = "generate_a_token_and_paste_it_here"
# Minimum length ensures sufficient entropy for bearer authorization tokens
MIN_TOKEN_LENGTH = 24

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
CAPTURE_TOKEN = os.getenv("CAPTURE_TOKEN", "").strip()

raw_model = os.getenv("MODEL_NAME")
MODEL_NAME: Optional[str] = raw_model.strip() if raw_model and raw_model.strip() else None
CAPTURE_PORT = int(os.getenv("CAPTURE_PORT", "8765"))

def resolve_db_path(raw_path: Optional[str] = None) -> Path:
    """Resolves database path against PROJECT_ROOT unless already absolute."""
    if not raw_path or not raw_path.strip():
        return (PROJECT_ROOT / "pollinate.db").resolve()
    target = Path(raw_path.strip())
    if target.is_absolute():
        return target
    return (PROJECT_ROOT / target).resolve()


# Resolving against PROJECT_ROOT prevents split-brain SQLite files when processes run from different working directories
DB_PATH: Path = resolve_db_path(os.getenv("DB_PATH", ""))

# Limits and timeouts for inline extraction and drafting
MAX_SELECTION_CHARS = 15000
MAX_PRIOR_COMMENTS_IN_PROMPT = 5
LLM_TIMEOUT_SECONDS = 30.0
# Hard deadline capping cumulative LLM round-trips for repeat author drafting
TOTAL_DEADLINE_SECONDS = 60.0

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
    key_to_check = LLM_API_KEY if api_key is None else api_key.strip()
    token_to_check = CAPTURE_TOKEN if token is None else token.strip()
    model_to_check = (
        MODEL_NAME
        if model is None
        else (model.strip() if model and model.strip() else "")
    )

    if not key_to_check:
        raise ValueError(
            "LLM_API_KEY is missing or empty. "
            "Set LLM_API_KEY in your .env file or environment."
        )

    if key_to_check == LLM_API_KEY_PLACEHOLDER:
        raise ValueError(
            "LLM_API_KEY is still set to the placeholder value. "
            "Replace it with your actual LLM API key in .env."
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
        raise ValueError(
            "MODEL_NAME is missing or empty. "
            "Set MODEL_NAME in your .env file or environment."
        )

    if model_to_check == MODEL_NAME_PLACEHOLDER:
        raise ValueError(
            "MODEL_NAME is still set to the placeholder value. "
            "Replace it with your actual model name in .env."
        )
