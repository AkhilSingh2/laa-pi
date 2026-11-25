"""Provider selection and the OpenAI-compatible tool-call parser (DeepSeek, RouterPlus, others)."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from core.llm import LLMClient, chat_completions_url, key_problem, parse_chat_completion
from core.settings import LLMConfig


class _Note(BaseModel):
    text: str


def test_key_prefix_routes_routerplus_and_deepseek():
    assert key_problem("routerplus", "tm_vk_" + "a" * 20) is None
    assert "RouterPlus" in key_problem("anthropic", "tm_vk_" + "a" * 20)
    assert key_problem("deepseek", "sk-" + "b" * 20) is None
    assert key_problem("anthropic", "sk-" + "b" * 20)  # Anthropic keys are sk-ant-


def test_chat_url_and_presets():
    assert chat_completions_url("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert chat_completions_url("https://api.routerplus.com/v1") == (
        "https://api.routerplus.com/v1/chat/completions"
    )
    cfg = LLMConfig(provider="routerplus", model="deepseek/deepseek-v4.1-flash", api_key_env="TM_API_KEY")
    client = LLMClient(cfg)
    assert client.wire == "openai"
    assert client.base_url == "https://api.routerplus.com/v1"


def test_parse_tool_call_and_fenced_json():
    parsed = parse_chat_completion(
        {
            "choices": [
                {"message": {"tool_calls": [{"function": {"name": "save", "arguments": '{"text": "hi"}'}}]}}
            ]
        },
        "save",
    )
    assert parsed == {"text": "hi"}
    parsed = parse_chat_completion(
        {"choices": [{"message": {"content": 'sure\n```json\n{"text": "ok"}\n```'}}]},
        "save",
    )
    assert parsed == {"text": "ok"}


def test_openai_compatible_call_uses_bearer_and_base(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        body = request.read().decode()
        seen["body"] = body
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"text": "scored"}'}}], "usage": {}}
        )

    transport = httpx.MockTransport(handler)
    cfg = LLMConfig(
        provider="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-key")
    http = httpx.Client(transport=transport)
    out = LLMClient(cfg, http=http).structured(
        system="sys", user="score this", schema=_Note, tool_name="save", description="save"
    )
    assert out.text == "scored"
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer sk-deepseek-test-key"
    assert "deepseek-chat" in seen["body"]
