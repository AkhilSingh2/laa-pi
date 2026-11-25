"""Structured JSON from whichever AI provider is configured, validated against a Pydantic model.

Anthropic is called with its own tool-use API. DeepSeek, RouterPlus (keys starting tm_vk_) and any other
OpenAI-compatible endpoint are called with chat-completions tool calling. Parsing failures are retried;
transport failures raise LLMUnavailable so callers can fall back to the offline heuristic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import httpx
from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.logging import log
from core.settings import LLMConfig

T = TypeVar("T", bound=BaseModel)
Wire = Literal["anthropic", "openai"]


class LLMUnavailable(RuntimeError):
    """Raised when no API key is configured or the API cannot be reached."""


class _Retryable(RuntimeError):
    """Transient provider failure. Retried, then reported as LLMUnavailable."""


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    wire: Wire
    api_key_env: str
    default_model: str
    base_url: str
    key_prefix: str
    hint: str


PROVIDERS: dict[str, ProviderPreset] = {
    "anthropic": ProviderPreset(
        "anthropic",
        "Anthropic",
        "anthropic",
        "ANTHROPIC_API_KEY",
        "claude-sonnet-4-6",
        "",
        "sk-ant-",
        "Direct Claude API. Keys start with sk-ant-.",
    ),
    "deepseek": ProviderPreset(
        "deepseek",
        "DeepSeek",
        "openai",
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        "https://api.deepseek.com",
        "sk-",
        "DeepSeek's own API. deepseek-chat is the cheap model; deepseek-reasoner is the slower one.",
    ),
    "routerplus": ProviderPreset(
        "routerplus",
        "RouterPlus",
        "openai",
        "TM_API_KEY",
        "deepseek/deepseek-v4.1-flash",
        "https://api.routerplus.com/v1",
        "tm_vk_",
        "One key (tm_vk_…) for every model in the RouterPlus catalog, including cheap DeepSeek models. "
        "Change the model id to switch.",
    ),
    "openai": ProviderPreset(
        "openai",
        "Other (OpenAI-compatible)",
        "openai",
        "LLM_API_KEY",
        "",
        "",
        "",
        "Groq, OpenRouter, Together, or any endpoint that accepts /chat/completions. Paste its base URL.",
    ),
}


def preset_for(provider: str) -> ProviderPreset:
    return PROVIDERS.get(provider, PROVIDERS["openai"])


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def key_problem(provider: str, key: str) -> str | None:
    """None when the key looks right for this provider."""
    key = key.strip()
    if len(key) < 12:
        return "That key looks too short."
    preset = preset_for(provider)
    if preset.key_prefix and not key.startswith(preset.key_prefix):
        if key.startswith("tm_vk_"):
            return "That is a RouterPlus key. Choose the RouterPlus provider."
        return f"{preset.label} keys start with {preset.key_prefix}."
    return None


class LLMClient:
    def __init__(
        self, cfg: LLMConfig, client: Anthropic | None = None, http: httpx.Client | None = None
    ) -> None:
        self.cfg = cfg
        self._client = client
        self._http = http
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    @property
    def preset(self) -> ProviderPreset:
        return preset_for(self.cfg.provider)

    @property
    def wire(self) -> Wire:
        return self.preset.wire

    @property
    def base_url(self) -> str:
        return (self.cfg.base_url or self.preset.base_url).rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.cfg.api_key) or self._client is not None

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            if not self.cfg.api_key:
                raise LLMUnavailable(f"{self.cfg.api_key_env} is not set; add it in Settings or .env")
            kwargs: dict[str, Any] = {"api_key": self.cfg.api_key, "max_retries": 0}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = Anthropic(**kwargs)
        return self._client

    def _http_client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=120)
        return self._http

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, _Retryable)),
    )
    def _call(self, *, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        try:
            if self.wire == "anthropic":
                return self._call_anthropic(system=system, user=user, tool=tool)
            return self._call_openai(system=system, user=user, tool=tool)
        except _Retryable:
            raise
        except (RateLimitError, APIConnectionError):
            raise
        except APIStatusError as e:
            raise LLMUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e

    def _call_anthropic(self, *, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        self.total_input_tokens += getattr(resp.usage, "input_tokens", 0)
        self.total_output_tokens += getattr(resp.usage, "output_tokens", 0)
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                return dict(block.input)
        raise ValueError("The model returned no tool call")

    def _call_openai(self, *, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.api_key:
            raise LLMUnavailable(f"{self.cfg.api_key_env} is not set; add it in Settings or .env")
        if not self.base_url:
            raise LLMUnavailable("No API base URL. Set one for this provider in Settings.")
        body = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
        }
        try:
            resp = self._http_client().post(
                chat_completions_url(self.base_url),
                headers={"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise _Retryable(str(e)) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMUnavailable(f"{self.preset.label} API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        self.total_input_tokens += int(usage.get("prompt_tokens") or 0)
        self.total_output_tokens += int(usage.get("completion_tokens") or 0)
        return parse_chat_completion(data, tool["name"])

    def structured(self, *, system: str, user: str, schema: type[T], tool_name: str, description: str) -> T:
        tool = {
            "name": tool_name,
            "description": description,
            "input_schema": schema.model_json_schema(),
        }
        last_err: Exception | None = None
        prompt = user
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                payload = self._call(system=system, user=prompt, tool=tool)
                return schema.model_validate(payload)
            except ValidationError as e:
                last_err = e
                log.warning(
                    "LLM schema validation failed (attempt {}/{}): {}", attempt, self.cfg.max_retries, e
                )
                prompt = (
                    user
                    + "\n\nYour previous answer failed validation:\n"
                    + json.dumps(e.errors(), default=str)[:2000]
                )
            except ValueError as e:
                last_err = e
                log.warning(
                    "LLM returned unreadable JSON (attempt {}/{}): {}", attempt, self.cfg.max_retries, e
                )
                prompt = user + f"\n\nYour previous answer was not valid JSON ({e}). Call the tool again."
            except _Retryable as e:
                raise LLMUnavailable(f"{self.preset.label} did not respond: {e}") from e
            except (RateLimitError, APIConnectionError) as e:
                raise LLMUnavailable(f"{self.preset.label} did not respond: {e}") from e
        raise ValueError(f"LLM output failed schema validation after retries: {last_err}")


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_chat_completion(data: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Pull the tool arguments, or a JSON object, out of an OpenAI-style chat response."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("response had no choices") from e
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name") in (tool_name, None, "") or not tool_name:
            raw = fn.get("arguments") or "{}"
            if isinstance(raw, dict):
                return raw
            return json.loads(raw)
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    fenced = _FENCE.search(content)
    blob = fenced.group(1) if fenced else content[content.find("{") : content.rfind("}") + 1]
    if not blob.strip():
        raise ValueError("response had no tool call or JSON")
    return json.loads(blob)
