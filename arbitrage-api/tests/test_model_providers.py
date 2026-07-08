import asyncio
import json

import pytest

from services import model_providers as mp


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text if text is not None else json.dumps(self._json_data)

    def json(self):
        return self._json_data


def _fake_async_client(response):
    """Returns a class that stands in for httpx.AsyncClient, always
    returning `response` from .post(), with no real network I/O."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            return response

    return _FakeAsyncClient


def _run(coro):
    return asyncio.run(coro)


# --- Anthropic / Kimi (shared Anthropic Messages wire format) ---


def test_anthropic_provider_parses_clean_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "content": [{"type": "text", "text": '{"should_list": true, "risk_level": "low"}'}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.AnthropicProvider(model="claude-test")
    result = _run(provider.complete("sys", "user"))
    assert result == {"should_list": True, "risk_level": "low"}


def test_kimi_provider_parses_fenced_json(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "content": [{"type": "text", "text": '```json\n{"should_list": false, "risk_level": "high"}\n```'}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.KimiProvider()
    assert provider.model == "kimi-for-coding"
    result = _run(provider.complete("sys", "user"))
    assert result == {"should_list": False, "risk_level": "high"}


def test_kimi_provider_parses_json_with_preamble(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "content": [{"type": "text", "text": 'Sure, here is my answer: {"should_list": true} — hope that helps!'}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.KimiProvider()
    result = _run(provider.complete("sys", "user"))
    assert result == {"should_list": True}


def test_anthropic_provider_malformed_json_raises_provider_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "content": [{"type": "text", "text": "this is not json at all, sorry"}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.AnthropicProvider(model="claude-test")
    with pytest.raises(mp.ProviderError):
        _run(provider.complete("sys", "user"))


def test_anthropic_provider_missing_key_raises_without_network(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _explode(*a, **kw):
        raise AssertionError("must not attempt a network call without an API key")

    monkeypatch.setattr(mp.httpx, "AsyncClient", _explode)

    provider = mp.AnthropicProvider(model="claude-test")
    with pytest.raises(mp.ProviderError):
        _run(provider.complete("sys", "user"))


def test_anthropic_provider_http_error_raises_provider_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeResponse(status_code=401, text="unauthorized")
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.AnthropicProvider(model="claude-test")
    with pytest.raises(mp.ProviderError):
        _run(provider.complete("sys", "user"))


# --- OpenAI (Chat Completions wire format) ---


def test_openai_provider_parses_clean_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "choices": [{"message": {"content": '{"should_list": true, "risk_level": "med"}'}}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.OpenAIProvider(model="gpt-test")
    result = _run(provider.complete("sys", "user"))
    assert result == {"should_list": True, "risk_level": "med"}


def test_openai_provider_parses_fenced_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "choices": [{"message": {"content": '```json\n{"should_list": false}\n```'}}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.OpenAIProvider(model="gpt-test")
    result = _run(provider.complete("sys", "user"))
    assert result == {"should_list": False}


def test_openai_provider_malformed_json_raises_provider_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeResponse(json_data={
        "choices": [{"message": {"content": "no json here"}}]
    })
    monkeypatch.setattr(mp.httpx, "AsyncClient", _fake_async_client(response))

    provider = mp.OpenAIProvider(model="gpt-test")
    with pytest.raises(mp.ProviderError):
        _run(provider.complete("sys", "user"))


def test_openai_provider_requires_model():
    with pytest.raises(mp.ProviderError):
        mp.OpenAIProvider()


# --- Factory ---


def test_get_provider_defaults_to_kimi(monkeypatch):
    monkeypatch.delenv("SCORER_PROVIDER", raising=False)
    monkeypatch.delenv("SCORER_MODEL", raising=False)
    provider = mp.get_provider()
    assert isinstance(provider, mp.KimiProvider)


def test_get_provider_selects_anthropic_with_model(monkeypatch):
    monkeypatch.setenv("SCORER_PROVIDER", "anthropic")
    monkeypatch.setenv("SCORER_MODEL", "claude-test")
    provider = mp.get_provider()
    assert isinstance(provider, mp.AnthropicProvider)
    assert provider.model == "claude-test"


def test_get_provider_selects_openai_with_model(monkeypatch):
    monkeypatch.setenv("SCORER_PROVIDER", "openai")
    monkeypatch.setenv("SCORER_MODEL", "gpt-test")
    provider = mp.get_provider()
    assert isinstance(provider, mp.OpenAIProvider)
    assert provider.model == "gpt-test"


def test_get_provider_unknown_name_raises(monkeypatch):
    monkeypatch.setenv("SCORER_PROVIDER", "not-a-real-provider")
    with pytest.raises(mp.ProviderError):
        mp.get_provider()


# --- Mock provider (zero-spend dev convenience) ---


def test_get_provider_selects_mock(monkeypatch):
    monkeypatch.setenv("SCORER_PROVIDER", "mock")
    monkeypatch.delenv("SCORER_MODEL", raising=False)
    provider = mp.get_provider()
    assert isinstance(provider, mp.MockProvider)


def test_mock_provider_returns_expected_schema_with_no_network_call(monkeypatch):
    def _explode(*a, **kw):
        raise AssertionError("MockProvider must never touch httpx.AsyncClient")

    monkeypatch.setattr(mp.httpx, "AsyncClient", _explode)

    provider = mp.MockProvider()
    result = _run(provider.complete("sys", "user"))

    assert set(result) == {"should_list", "risk_level", "confidence", "reason", "competition_score"}
    assert result["should_list"] is True
    assert result["risk_level"] == "low"
    assert result["competition_score"] is None
    assert "MOCK" in result["reason"]
