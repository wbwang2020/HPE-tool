"""OpenAI-compatible provider used by API mode.

No SDK dependency: standard-library HTTP only. Retries are limited to transient
status codes; authentication and parameter errors are not retried. Usage is
recorded from the response when the provider reports it.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import ModelSpec, read_api_key, resolve_model
from .errors import ConfigError, CredentialError, ProviderError
from .util import estimate_tokens

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


@dataclass
class CompletionResult:
    text: str
    model: str
    usage: dict[str, int]
    finish_reason: str = "stop"
    request_id: str | None = None


class OpenAICompatibleProvider:
    def __init__(self, spec: ModelSpec, *, api_key: str | None, timeout: float = 600.0, max_retries: int = 3) -> None:
        if not spec.base_url:
            raise ConfigError(f"Provider {spec.provider} has no base_url")
        self.spec = spec
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.spec.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise ProviderError(f"HTTP {exc.code} from {self.spec.provider}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Network error calling {self.spec.provider}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"Timeout calling {self.spec.provider}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Provider returned invalid JSON: {exc}") from exc

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 8000,
        json_mode: bool = True,
        extra_body: dict[str, Any] | None = None,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode and self.spec.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.spec.reasoning_effort:
            payload["reasoning_effort"] = self.spec.reasoning_effort
        if extra_body:
            payload.update(extra_body)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._request(payload)
                return self._parse(data)
            except ProviderError as exc:
                message = str(exc)
                # Some compatible endpoints reject response_format; retry once
                # without it before treating the request as a hard failure.
                if "response_format" in payload and ("400" in message or "response_format" in message.lower()):
                    payload.pop("response_format", None)
                    continue
                status = self._status_from_error(message)
                if status is None or status not in RETRY_STATUS or attempt >= self.max_retries:
                    last_error = exc
                    break
                delay = min(30.0, (2 ** attempt) + random.random())
                time.sleep(delay)
                last_error = exc
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError(f"Provider call failed: {last_error}")

    @staticmethod
    def _status_from_error(message: str) -> int | None:
        marker = "HTTP "
        index = message.find(marker)
        if index < 0:
            return None
        digits = ""
        for char in message[index + len(marker) :]:
            if char.isdigit():
                digits += char
            else:
                break
        return int(digits) if digits else None

    def _parse(self, data: dict[str, Any]) -> CompletionResult:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"Provider response has no choices: {str(data)[:500]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Provider returned an empty message")
        usage = data.get("usage") or {}
        return CompletionResult(
            text=content,
            model=str(data.get("model") or self.spec.model),
            usage={
                "tokens_in": int(usage.get("prompt_tokens") or 0),
                "tokens_out": int(usage.get("completion_tokens") or 0),
                "tokens_total": int(usage.get("total_tokens") or 0),
            },
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            request_id=data.get("id"),
        )

    def estimate_cost(self, system: str, user: str, max_tokens: int) -> tuple[int, int, float]:
        tokens_in = estimate_tokens(system) + estimate_tokens(user)
        cost = self.spec.estimate_cost(tokens_in, max_tokens)
        return tokens_in, max_tokens, cost


def provider_for(
    provider: str | None,
    model: str | None,
    *,
    env_file: str | None = None,
    timeout: float = 600.0,
) -> OpenAICompatibleProvider:
    spec = resolve_model(provider, model)
    api_key = read_api_key(spec.api_key_env, env_file)
    if spec.api_key_env and not api_key:
        raise CredentialError(
            f"Missing credential variable {spec.api_key_env} for provider {spec.provider}. "
            "Set it in the environment or in ~/.hpe-paper-evaluator/.env; never pass it on the command line."
        )
    return OpenAICompatibleProvider(spec, api_key=api_key, timeout=timeout)


def provider_for_backend(spec: Any, *, timeout: float = 600.0) -> OpenAICompatibleProvider:
    """Create a provider from an llm_config.BackendSpec (key file based)."""

    from .llm_config import BackendSpec, require_api_key

    if not isinstance(spec, BackendSpec) or spec.is_host:
        raise ConfigError("provider_for_backend requires a non-host BackendSpec")
    require_api_key(spec)
    model_spec = ModelSpec(
        provider=str(spec.provider),
        model=str(spec.model),
        base_url=str(spec.base_url or ""),
        api_key_env=spec.api_key_env,
        context_window=int(spec.context_window),
        max_output_tokens=int(spec.max_output_tokens),
        json_mode=bool(spec.json_mode),
        reasoning_effort=spec.reasoning_effort,
        price_input_per_mtok=float(spec.price_input_per_mtok),
        price_output_per_mtok=float(spec.price_output_per_mtok),
    )
    return OpenAICompatibleProvider(model_spec, api_key=spec.api_key, timeout=timeout)
