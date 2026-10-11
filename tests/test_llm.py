"""Tests for DefaultLLMProvider behavior using a simulated SDK client."""

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel

import config
import llm
from llm import DefaultLLMProvider, LLMError
from models import DraftAngle, InlineAnalysisResult, SingleDraft


class SampleSchema(BaseModel):
    message: str


class FakeResponse:
    def __init__(self, text: str = "", parsed=None):
        self.text = text
        self.parsed = parsed


@pytest.fixture(autouse=True)
def restore_provider():
    orig = llm._ACTIVE_PROVIDER
    yield
    llm._ACTIVE_PROVIDER = orig


def create_provider_with_fake_client(api_key: str = "fake_key_123456789012345678", model: str = "test-model"):
    provider = DefaultLLMProvider(model_name=model, api_key=api_key)
    fake_client = MagicMock()
    provider._client = fake_client
    return provider, fake_client


def test_default_llm_provider_requires_model_name():
    with pytest.raises(TypeError):
        DefaultLLMProvider()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="model_name is required"):
        DefaultLLMProvider(model_name="")
    with pytest.raises(ValueError, match="model_name is required"):
        DefaultLLMProvider(model_name="   ")


def test_missing_config_raises_value_error():
    provider = DefaultLLMProvider(model_name="test-model", api_key="")
    with pytest.raises(ValueError, match="LLM_API_KEY is not configured"):
        provider._get_client()


def test_valid_output_parses_into_schema():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.return_value = FakeResponse(text='{"message": "parsed successfully"}')

    result = provider.generate_structured("test prompt", SampleSchema)
    assert isinstance(result, SampleSchema)
    assert result.message == "parsed successfully"


def test_response_schema_is_passed_to_sdk():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.return_value = FakeResponse(text='{"message": "schema check"}')

    provider.generate_structured("test prompt", SampleSchema)

    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "test-model"
    assert call_kwargs["contents"] == "test prompt"
    assert call_kwargs["config"].response_mime_type == "application/json"
    assert call_kwargs["config"].response_schema == SampleSchema


def test_malformed_json_triggers_retry():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = [
        FakeResponse(text="malformed json not closing"),
        FakeResponse(text='{"message": "recovered on attempt 2"}'),
    ]

    result = provider.generate_structured("test prompt", SampleSchema, max_retries=3)
    assert result.message == "recovered on attempt 2"
    assert fake_client.models.generate_content.call_count == 2


def test_valid_json_wrong_shape_triggers_retry_then_fails():
    provider, fake_client = create_provider_with_fake_client()
    # Valid JSON but missing required SampleSchema fields
    fake_client.models.generate_content.side_effect = [
        FakeResponse(text='{"unrelated_field": 1}'),
        FakeResponse(text='{"unrelated_field": 2}'),
        FakeResponse(text='{"unrelated_field": 3}'),
    ]

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_response_text_none_triggers_retry():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = [
        FakeResponse(text=None, parsed=None),
        FakeResponse(text='{"message": "recovered"}'),
    ]

    result = provider.generate_structured("test prompt", SampleSchema, max_retries=3)
    assert result.message == "recovered"
    assert fake_client.models.generate_content.call_count == 2


def test_transport_exception_retries_and_asserts_call_count():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = httpx.ConnectError("Connection refused by peer")

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_rate_limit_429_retries():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = errors.ClientError(
        429, {"error": {"message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}
    )

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_server_error_500_retries():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = errors.ServerError(
        500, {"error": {"message": "Internal Server Error", "status": "INTERNAL"}}
    )

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_timeout_retries():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = httpx.ReadTimeout("Deadline exceeded")

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_model_not_found_surfaces_immediately_with_model_name():
    provider, fake_client = create_provider_with_fake_client(model="missing-reasoning-model")
    fake_client.models.generate_content.side_effect = errors.ClientError(
        404, {"error": {"message": "models/missing-reasoning-model not found", "status": "NOT_FOUND"}}
    )

    with pytest.raises(LLMError, match="Model 'missing-reasoning-model' was rejected by the Gemini API"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    # Must fail immediately on first attempt without retrying
    assert fake_client.models.generate_content.call_count == 1


def test_authentication_error_fails_immediately():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = errors.ClientError(
        401, {"error": {"message": "Invalid API key provided", "status": "UNAUTHENTICATED"}}
    )

    with pytest.raises(LLMError, match="Gemini API authentication failed"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 1


def test_bad_request_fails_immediately():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = errors.ClientError(
        400, {"error": {"message": "Invalid argument in request", "status": "INVALID_ARGUMENT"}}
    )

    with pytest.raises(LLMError, match="Gemini API bad request"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 1


def test_unrecognized_exception_propagates_unchanged():
    provider, fake_client = create_provider_with_fake_client()
    fake_client.models.generate_content.side_effect = RuntimeError("Unrecognized internal runtime bug")

    with pytest.raises(RuntimeError, match="Unrecognized internal runtime bug"):
        provider.generate_structured("test prompt", SampleSchema, max_retries=3)

    assert fake_client.models.generate_content.call_count == 1


def test_api_key_never_appears_in_logs_or_exceptions(caplog):
    secret_key = "SUPER_SECRET_KEY_THAT_MUST_NEVER_LEAK"

    # Transport/retry exhaustion path
    provider, fake_client = create_provider_with_fake_client(api_key=secret_key)
    fake_client.models.generate_content.side_effect = httpx.ConnectError(f"Simulated network exception for {secret_key}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError) as exc_info:
            provider.generate_structured("test prompt", SampleSchema, max_retries=2)

    err = exc_info.value
    assert secret_key not in str(err)
    assert secret_key not in repr(err)
    assert err.__cause__ is None
    assert err.__context__ is None
    assert secret_key not in caplog.text

    # Model-not-found path
    provider_mnf, fake_client_mnf = create_provider_with_fake_client(api_key=secret_key, model="missing-model")
    fake_client_mnf.models.generate_content.side_effect = errors.ClientError(
        404, {"error": {"message": f"Model not found for key {secret_key}", "status": "NOT_FOUND"}}
    )
    with pytest.raises(LLMError) as exc_info_mnf:
        provider_mnf.generate_structured("test prompt", SampleSchema, max_retries=2)

    err_mnf = exc_info_mnf.value
    assert secret_key not in str(err_mnf)
    assert secret_key not in repr(err_mnf)
    assert err_mnf.__cause__ is None
    assert err_mnf.__context__ is None

    # Invalid API key path
    provider_auth, fake_client_auth = create_provider_with_fake_client(api_key=secret_key)
    fake_client_auth.models.generate_content.side_effect = errors.ClientError(
        401, {"error": {"message": f"Unauthorized secret {secret_key}", "status": "UNAUTHENTICATED"}}
    )
    with pytest.raises(LLMError) as exc_info_auth:
        provider_auth.generate_structured("test prompt", SampleSchema, max_retries=2)

    err_auth = exc_info_auth.value
    assert secret_key not in str(err_auth)
    assert secret_key not in repr(err_auth)
    assert err_auth.__cause__ is None
    assert err_auth.__context__ is None


def test_schema_missing_headline_triggers_retry_then_fails():
    provider, fake_client = create_provider_with_fake_client()
    # Missing required author_headline_or_company
    bad_json = """{
        "author_name": "Bob",
        "post_body": "Post body text",
        "relevance_score": 8,
        "opportunity_score": 7,
        "one_line_reason": "Good post",
        "drafts": [
            {"angle": "experience", "text": "exp", "adds_beyond_existing": "a"},
            {"angle": "question", "text": "q", "adds_beyond_existing": "b"},
            {"angle": "counterpoint", "text": "c", "adds_beyond_existing": "c"}
        ]
    }"""
    fake_client.models.generate_content.return_value = FakeResponse(text=bad_json)

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", InlineAnalysisResult, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_schema_fewer_or_more_than_three_drafts_triggers_retry():
    provider, fake_client = create_provider_with_fake_client()
    # Only 2 drafts
    bad_json = """{
        "author_name": "Bob",
        "author_headline_or_company": "Engineer",
        "post_body": "Post body text",
        "relevance_score": 8,
        "opportunity_score": 7,
        "one_line_reason": "Good post",
        "drafts": [
            {"angle": "experience", "text": "exp", "adds_beyond_existing": "a"},
            {"angle": "question", "text": "q", "adds_beyond_existing": "b"}
        ]
    }"""
    fake_client.models.generate_content.return_value = FakeResponse(text=bad_json)

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", InlineAnalysisResult, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_schema_duplicate_or_missing_angles_triggers_retry():
    provider, fake_client = create_provider_with_fake_client()
    # 3 drafts but duplicate "experience" and missing "counterpoint"
    bad_json = """{
        "author_name": "Bob",
        "author_headline_or_company": "Engineer",
        "post_body": "Post body text",
        "relevance_score": 8,
        "opportunity_score": 7,
        "one_line_reason": "Good post",
        "drafts": [
            {"angle": "experience", "text": "exp 1", "adds_beyond_existing": "a"},
            {"angle": "experience", "text": "exp 2", "adds_beyond_existing": "b"},
            {"angle": "question", "text": "q", "adds_beyond_existing": "c"}
        ]
    }"""
    fake_client.models.generate_content.return_value = FakeResponse(text=bad_json)

    with pytest.raises(LLMError, match="Failed to generate valid structured response after 3 attempts"):
        provider.generate_structured("test prompt", InlineAnalysisResult, max_retries=3)

    assert fake_client.models.generate_content.call_count == 3


def test_schema_valid_three_distinct_angles_parses():
    provider, fake_client = create_provider_with_fake_client()
    valid_json = """{
        "author_name": "Alice",
        "author_headline_or_company": "VP of AI",
        "post_body": "Post body discussing models",
        "relevance_score": 9,
        "opportunity_score": 8,
        "one_line_reason": "Relevant discussion",
        "drafts": [
            {"angle": "experience", "text": "exp", "adds_beyond_existing": "a"},
            {"angle": "question", "text": "q", "adds_beyond_existing": "b"},
            {"angle": "counterpoint", "text": "c", "adds_beyond_existing": "c"}
        ]
    }"""
    fake_client.models.generate_content.return_value = FakeResponse(text=valid_json)

    res = provider.generate_structured("test prompt", InlineAnalysisResult, max_retries=2)
    assert isinstance(res, InlineAnalysisResult)
    assert res.author_name == "Alice"
    assert res.author_headline_or_company == "VP of AI"
    assert len(res.drafts) == 3


@pytest.mark.integration
def test_live_provider_smoke_integration():
    if not config.LLM_API_KEY or not config.MODEL_NAME:
        pytest.skip("LLM_API_KEY and MODEL_NAME required for live smoke integration.")

    provider = DefaultLLMProvider(model_name=config.MODEL_NAME, api_key=config.LLM_API_KEY)
    res = provider.generate_structured(
        "Analyze this post: Great thoughts on AI by John Doe, CTO at AI Org: https://example.com/post",
        InlineAnalysisResult,
    )
    assert isinstance(res, InlineAnalysisResult)
    assert res.author_name
    assert res.author_headline_or_company
    assert len(res.drafts) == 3


def test_static_check_no_gemini_model_names_outside_tests():
    """Statically verifies that 'gemini-' does not appear in any .py file outside tests."""
    violations = []

    for py_file in config.PROJECT_ROOT.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == Path(__file__).resolve():
            continue
        if ".venv" in resolved.parts or "venv" in resolved.parts or "tests" in resolved.parts:
            continue

        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
            if "gemini-" in content:
                violations.append(f"{py_file.name} contains 'gemini-'")

    assert not violations, f"Forbidden 'gemini-' model string found outside tests: {violations}"


def test_static_check_only_llm_imports_genai():
    """Statically verifies that only llm.py imports google.genai, covering both import forms."""
    violations = []

    for py_file in config.PROJECT_ROOT.rglob("*.py"):
        resolved = py_file.resolve()
        if resolved == Path(__file__).resolve():
            continue
        if ".venv" in resolved.parts or "venv" in resolved.parts or "tests" in resolved.parts:
            continue
        if resolved.name == "llm.py":
            continue

        with open(resolved, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(resolved))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "genai" in alias.name:
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and "genai" in node.module:
                    violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")
                for alias in node.names:
                    if "genai" in alias.name:
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name} from {node.module}")

    assert not violations, f"google.genai imported outside llm.py: {violations}"
