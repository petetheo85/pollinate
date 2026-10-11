"""Sole module communicating with an LLM provider. Current implementation uses Google's SDK."""

import logging
from typing import Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

import config
from config import LLM_API_KEY, RETRY_LIMIT
from models import InlineAnalysisResult

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when an LLM provider operation fails."""


def _extract_sdk_status(exc: Exception) -> Tuple[Optional[int], Optional[str]]:
    """Extracts integer HTTP status code and status text from SDK errors."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if isinstance(code, int):
        return code, str(status) if status else None
    return None, None


def _is_recognized_exception(exc: Exception) -> bool:
    """Returns True if exc is an expected SDK API, transport, or validation error."""
    from google.genai import errors
    import httpx

    return isinstance(
        exc,
        (
            errors.APIError,
            httpx.RequestError,
            TimeoutError,
            ConnectionError,
            ValueError,
            KeyError,
        ),
    )


def _is_retriable(exc: Exception, code: Optional[int]) -> bool:
    """Returns True if the failure is transient and eligible for retry."""
    import httpx

    # Parsing and validation failures on response shape
    if isinstance(exc, (ValueError, KeyError)):
        return True

    # Network connection issues and request timeouts
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return True
    if isinstance(exc, (httpx.NetworkError, ConnectionError)):
        return True

    # Rate limits (429) and server errors (5xx)
    if code is not None:
        return code == 429 or (500 <= code < 600)

    return False


class DefaultLLMProvider:
    """Default provider implementation using Google's GenAI SDK."""

    def __init__(self, model_name: str, api_key: str = LLM_API_KEY) -> None:
        if not model_name or not model_name.strip():
            raise ValueError("model_name is required.")
        self.model_name = model_name.strip()
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        # Lazy initialization defers client creation until the first inference call
        if self._client is None:
            if not self.api_key:
                raise ValueError("LLM_API_KEY is not configured.")
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        max_retries: int = RETRY_LIMIT,
    ) -> T:
        from google.genai import types

        client = self._get_client()
        last_error_msg: Optional[str] = None
        immediate_error_msg: Optional[str] = None

        for attempt in range(max_retries):
            raw_err: Optional[Exception] = None
            try:
                gen_config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.2,
                )
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=gen_config,
                )
                if hasattr(response, "parsed") and response.parsed is not None:
                    if isinstance(response.parsed, response_schema):
                        return response.parsed
                    return response_schema.model_validate(response.parsed)

                if response.text:
                    return response_schema.model_validate_json(response.text)

                raw_err = ValueError("LLM response did not contain text or structured payload.")
            except Exception as exc:
                raw_err = exc

            if raw_err is not None:
                # Unrecognized errors (programming bugs, system crashes) propagate unchanged
                if not _is_recognized_exception(raw_err):
                    raise raw_err

                err_str = str(raw_err)
                err_cls = raw_err.__class__.__name__
                sanitized_body = err_str.replace(self.api_key, "[REDACTED]") if self.api_key else err_str
                code, status_name = _extract_sdk_status(raw_err)

                # Model-not-found (404)
                if code == 404:
                    status_part = f"{code} "
                    immediate_error_msg = (
                        f"Model '{self.model_name}' was rejected by the Gemini API ({status_part}{err_cls}): {sanitized_body}"
                    )
                    break

                # Authentication error (401 or 403)
                if code in (401, 403):
                    status_part = f"{code} "
                    immediate_error_msg = (
                        f"Gemini API authentication failed ({status_part}{err_cls}): {sanitized_body}"
                    )
                    break

                # Bad request (400) notes that 400 can also indicate an invalid API key
                if code == 400:
                    status_part = f"{code} "
                    immediate_error_msg = (
                        f"Gemini API bad request ({status_part}{err_cls}): {sanitized_body}. "
                        "Note: a 400 error can also indicate an invalid API key or malformed request parameters."
                    )
                    break

                # Other non-retriable failure
                if not _is_retriable(raw_err, code):
                    status_part = f"{code} " if code else ""
                    immediate_error_msg = (
                        f"Gemini API request failed ({status_part}{err_cls}): {sanitized_body}"
                    )
                    break

                # Transient retriable failure: 429, 5xx, timeout, network, or parse failure
                status_part = f"{code} " if code else ""
                last_error_msg = f"({status_part}{err_cls}) {sanitized_body}"
                logger.warning("Structured generation attempt %d failed: %s", attempt + 1, last_error_msg)

        # Raise LLMError strictly outside except blocks to guarantee __context__ is None
        if immediate_error_msg is not None:
            raise LLMError(immediate_error_msg)

        raise LLMError(
            f"Failed to generate valid structured response after {max_retries} attempts: {last_error_msg}"
        )


_ACTIVE_PROVIDER: Optional[DefaultLLMProvider] = None


def get_provider() -> DefaultLLMProvider:
    """Returns the active provider, defaulting to DefaultLLMProvider."""
    global _ACTIVE_PROVIDER
    if _ACTIVE_PROVIDER is None:
        if not config.MODEL_NAME:
            raise ValueError("MODEL_NAME is not configured.")
        _ACTIVE_PROVIDER = DefaultLLMProvider(model_name=config.MODEL_NAME)
    return _ACTIVE_PROVIDER


def set_provider(provider: DefaultLLMProvider) -> None:
    """Overrides the active LLM provider for testing."""
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


def generate_structured(
    prompt: str,
    response_schema: Type[T],
    max_retries: int = RETRY_LIMIT,
) -> T:
    """Generates a structured Pydantic object using the configured provider."""
    return get_provider().generate_structured(prompt, response_schema, max_retries=max_retries)


def analyze_and_draft_post(prompt: str) -> InlineAnalysisResult:
    """Performs unified post extraction, existing comment summary, scoring, and 3-angle drafting."""
    return generate_structured(prompt, InlineAnalysisResult)
