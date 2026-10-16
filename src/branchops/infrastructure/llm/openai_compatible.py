"""Provider for the bank's internal OpenAI-compatible inference gateway (e.g. GLM-5.3-Flash).

Only the minimal, widely-implemented subset of the Chat Completions API is used by default. Optional
features (``response_format`` JSON schema / JSON object, reasoning parameters) are sent only when the
corresponding capability is enabled, either explicitly in configuration or by ``probe_capabilities``.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from typing import Any

import openai
from openai import OpenAI

from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelAccessError,
    ModelHealth,
    ModelRequestError,
    ModelUnavailableError,
    OutputMode,
    TokenUsage,
    UnsupportedOutputModeError,
)
from branchops.config.settings import LLMSettings, ModelCapabilities, NetworkSettings
from branchops.infrastructure.security.network_guard import assert_internal_url

logger = logging.getLogger(__name__)

# Some reasoning models inline their hidden reasoning; it must never be stored or displayed.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_RESPONSE_FORMAT_HINTS = ("response_format", "json_schema", "json_object", "guided", "structured")


def strip_reasoning(content: str) -> str:
    content = _THINK_RE.sub("", content)
    # Unterminated reasoning block (truncated output): drop everything up to the closing tag if any.
    if "<think>" in content.lower():
        content = re.split(r"<think>", content, flags=re.IGNORECASE)[0]
    return content.strip()


def build_ssl_context(ca_bundle: str | None) -> ssl.SSLContext:
    """TLS context trusting the internal CA. Verification is always on."""
    ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class OpenAICompatibleModelProvider:
    def __init__(
        self,
        settings: LLMSettings,
        network: NetworkSettings,
        *,
        client: OpenAI | None = None,
    ) -> None:
        if not settings.base_url:
            raise ValueError("llm.base_url is required for the openai-compatible provider")
        assert_internal_url(settings.base_url, network, purpose="llm.base_url")
        self._settings = settings
        self._capabilities = settings.capabilities.model_copy()
        api_key = settings.api_key()
        self._client = client or OpenAI(
            api_key=api_key.get_secret_value() if api_key else "not-required",
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=openai.DefaultHttpxClient(verify=build_ssl_context(settings.ca_bundle)),
        )

    @property
    def model_id(self) -> str:
        return self._settings.model

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def _response_format(self, request: CompletionRequest) -> dict[str, Any] | None:
        if request.output_mode is OutputMode.JSON_SCHEMA and request.json_schema is not None:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name or "output",
                    "schema": request.json_schema,
                    "strict": False,
                },
            }
        if request.output_mode is OutputMode.JSON_OBJECT:
            return {"type": "json_object"}
        return None

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        max_tokens = min(
            request.max_output_tokens or self._settings.max_output_tokens,
            self._capabilities.max_output_tokens,
        )
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": self._settings.temperature,
            "max_tokens": max_tokens,
            "extra_headers": {"X-Correlation-ID": request.correlation_id, "X-Agent": request.agent},
        }
        response_format = self._response_format(request)
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self._capabilities.reasoning_parameters and self._settings.reasoning_effort:
            kwargs["reasoning_effort"] = self._settings.reasoning_effort
        if self._settings.extra_body:
            kwargs["extra_body"] = dict(self._settings.extra_body)
        if timeout is not None:
            kwargs["timeout"] = timeout

        started = time.perf_counter()
        try:
            response = self._client.chat.completions.create(**kwargs)
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise ModelUnavailableError(f"model endpoint unreachable: {type(exc).__name__}") from exc
        except openai.RateLimitError as exc:
            raise ModelUnavailableError("model endpoint rate limited") from exc
        except openai.InternalServerError as exc:
            raise ModelUnavailableError(f"model endpoint error {exc.status_code}") from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            # Never include request headers / key material in the message.
            raise ModelAccessError(f"model endpoint rejected credentials ({exc.status_code})") from exc
        except openai.BadRequestError as exc:
            message = str(getattr(exc, "message", "")) or "bad request"
            if response_format is not None and any(h in message.lower() for h in _RESPONSE_FORMAT_HINTS):
                raise UnsupportedOutputModeError(message[:300]) from exc
            raise ModelRequestError(f"model request rejected: {message[:300]}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 429):
                raise ModelUnavailableError(f"model endpoint error {exc.status_code}") from exc
            if exc.status_code == 404:
                raise ModelAccessError("model endpoint or model not found (404)") from exc
            if response_format is not None and exc.status_code in (400, 422):
                raise UnsupportedOutputModeError(f"status {exc.status_code}") from exc
            raise ModelRequestError(f"model endpoint error {exc.status_code}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not response.choices:
            raise ModelRequestError("model returned no choices")
        choice = response.choices[0]
        content = strip_reasoning(choice.message.content or "")
        usage = response.usage
        return CompletionResult(
            content=content,
            model_id=response.model or self._settings.model,
            output_mode=request.output_mode,
            usage=TokenUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            latency_ms=latency_ms,
            finish_reason=choice.finish_reason,
        )

    def health(self) -> ModelHealth:
        started = time.perf_counter()
        try:
            models = self._client.with_options(timeout=10.0, max_retries=0).models.list()
            ids = [m.id for m in models.data]
        except openai.APIStatusError as exc:
            return ModelHealth(False, self.model_id, f"models endpoint returned {exc.status_code}")
        except openai.APIError as exc:
            return ModelHealth(False, self.model_id, f"unreachable: {type(exc).__name__}")
        latency = int((time.perf_counter() - started) * 1000)
        if ids and self.model_id not in ids:
            return ModelHealth(False, self.model_id, "configured model not served by gateway", latency)
        return ModelHealth(True, self.model_id, "ok", latency)

    def probe_capabilities(self) -> ModelCapabilities:
        """Detect JSON schema / JSON object support with two tiny requests; updates and returns caps."""
        from branchops.application.interfaces.model_provider import ChatMessage

        caps = self._capabilities.model_copy()
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        probe_messages = [ChatMessage("user", 'Return the JSON object {"ok": true} and nothing else.')]
        for mode, attr in (
            (OutputMode.JSON_SCHEMA, "structured_outputs"),
            (OutputMode.JSON_OBJECT, "json_mode"),
        ):
            request = CompletionRequest(
                messages=probe_messages,
                agent="capability-probe",
                correlation_id="capability-probe",
                output_mode=mode,
                json_schema=schema,
                schema_name="probe",
                max_output_tokens=64,
            )
            try:
                self.complete(request, timeout=30.0)
                setattr(caps, attr, True)
            except UnsupportedOutputModeError:
                setattr(caps, attr, False)
            except ModelRequestError:
                setattr(caps, attr, False)
        self._capabilities = caps
        logger.info(
            "model capability probe complete",
            extra={"structured_outputs": caps.structured_outputs, "json_mode": caps.json_mode},
        )
        return caps
