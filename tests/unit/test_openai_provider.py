from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import OpenAI

from branchops.application.interfaces.model_provider import (
    ChatMessage,
    CompletionRequest,
    ModelRequestError,
    ModelUnavailableError,
    OutputMode,
    UnsupportedOutputModeError,
)
from branchops.config.settings import LLMSettings, ModelCapabilities, NetworkSettings
from branchops.infrastructure.llm.openai_compatible import OpenAICompatibleModelProvider
from branchops.infrastructure.security.network_guard import ExternalEndpointError

BASE_URL = "https://llm-gateway.ai-platform.svc.cluster.local/v1"


def completion(content: str) -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "GLM-5.3-Flash",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response], caps: ModelCapabilities | None = None
) -> tuple[OpenAICompatibleModelProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = LLMSettings(
        provider="openai-compatible",
        base_url=BASE_URL,
        max_retries=0,
        capabilities=caps or ModelCapabilities(),
    )
    client = OpenAI(
        api_key="k",
        base_url=BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(record)),
    )
    return OpenAICompatibleModelProvider(settings, NetworkSettings(), client=client), seen


def request(mode: OutputMode = OutputMode.JSON_OBJECT) -> CompletionRequest:
    return CompletionRequest(
        messages=[ChatMessage("system", "s"), ChatMessage("user", "u")],
        agent="classification",
        correlation_id="corr-123",
        output_mode=mode,
        json_schema={"type": "object"},
        schema_name="ClassificationOutput",
    )


def test_request_shape_and_reasoning_stripped() -> None:
    provider, seen = make_provider(
        lambda r: httpx.Response(200, json=completion('<think>gizli</think>{"a": 1}'))
    )
    result = provider.complete(request())
    body = json.loads(seen[0].content)
    assert body["model"] == "GLM-5.3-Flash"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in body
    assert seen[0].headers["X-Correlation-ID"] == "corr-123"
    assert result.content == '{"a": 1}'
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7


def test_json_schema_mode_and_prompt_only() -> None:
    provider, seen = make_provider(lambda r: httpx.Response(200, json=completion("{}")))
    provider.complete(request(OutputMode.JSON_SCHEMA))
    provider.complete(request(OutputMode.PROMPT_ONLY))
    assert json.loads(seen[0].content)["response_format"]["type"] == "json_schema"
    assert "response_format" not in json.loads(seen[1].content)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (503, {"error": {"message": "overloaded"}}, ModelUnavailableError),
        (429, {"error": {"message": "slow down"}}, ModelUnavailableError),
        (401, {"error": {"message": "bad key"}}, ModelRequestError),
        (
            400,
            {"error": {"message": "response_format json_object is not supported"}},
            UnsupportedOutputModeError,
        ),
        (400, {"error": {"message": "context length exceeded"}}, ModelRequestError),
    ],
)
def test_error_mapping(status: int, body: dict[str, Any], error: type[Exception]) -> None:
    provider, _ = make_provider(lambda r: httpx.Response(status, json=body))
    with pytest.raises(error) as exc:
        provider.complete(request())
    assert "bad key" not in str(exc.value) or status != 401


def test_connection_error_is_unavailable() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider, _ = make_provider(boom)
    with pytest.raises(ModelUnavailableError):
        provider.complete(request())


def test_health_and_probe() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "GLM-5.3-Flash", "object": "model", "created": 0, "owned_by": "bank"}],
                },
            )
        fmt = json.loads(r.content).get("response_format", {}).get("type")
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema response_format unsupported"}})
        return httpx.Response(200, json=completion('{"ok": true}'))

    provider, _ = make_provider(handler)
    assert provider.health().healthy
    caps = provider.probe_capabilities()
    assert caps.structured_outputs is False
    assert caps.json_mode is True


def test_public_endpoint_refused() -> None:
    with pytest.raises(ExternalEndpointError):
        OpenAICompatibleModelProvider(
            LLMSettings(provider="openai-compatible", base_url="https://api.openai.com/v1"), NetworkSettings()
        )


def test_tls_verification_always_enabled() -> None:
    from branchops.infrastructure.llm.openai_compatible import build_ssl_context

    ctx = build_ssl_context(None)
    assert ctx.check_hostname
    assert ctx.verify_mode.name == "CERT_REQUIRED"
