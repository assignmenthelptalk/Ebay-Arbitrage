"""Provider-agnostic model layer for the AI Product Scorer (§4A.3).

The scorer never touches a provider SDK directly — it calls
`get_provider().complete(system_prompt, user_content)` and gets back parsed
JSON. Swapping providers is a `.env` change (`SCORER_PROVIDER`/`SCORER_MODEL`),
not a code change.
"""

import json
import os
import re

import httpx

DEFAULT_TIMEOUT = 30
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 1024


class ProviderError(Exception):
    """Raised when a provider call fails or returns unparseable output."""


def _extract_json(raw_text: str) -> dict:
    """Defensively pull a JSON object out of a model's raw text reply.

    Models asked for "only JSON" still sometimes wrap it in ``` fences or add
    a sentence of preamble/postamble — strip that before json.loads, and
    raise ProviderError (not a silent {}) if nothing parseable is found.
    """
    text = raw_text.strip()

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(f"Could not parse JSON from model response: {exc}") from exc


class ModelProvider:
    """Interface every provider adapter implements."""

    async def complete(self, system_prompt: str, user_content: str) -> dict:
        raise NotImplementedError


class _AnthropicMessagesProvider(ModelProvider):
    """Shared implementation of the Anthropic Messages API wire format.

    Used directly by AnthropicProvider (api.anthropic.com) and by
    KimiProvider, which speaks the same Messages format at a different
    base_url (confirmed via OpenClaw's non-secret config + transport source:
    Kimi Coding's `anthropic-messages` transport hits `<base_url>/v1/messages`).
    """

    def __init__(self, base_url: str, api_key: str, model: str, *, temperature: float = DEFAULT_TEMPERATURE,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    async def complete(self, system_prompt: str, user_content: str) -> dict:
        if not self.api_key:
            raise ProviderError(f"{self.__class__.__name__}: no API key configured")

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.__class__.__name__} request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"{self.__class__.__name__} API {resp.status_code}: {resp.text}")

        data = resp.json()
        content = data.get("content", [])
        raw_text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        if not raw_text:
            raise ProviderError(f"{self.__class__.__name__}: no text content in response")

        return _extract_json(raw_text)


class AnthropicProvider(_AnthropicMessagesProvider):
    """Anthropic Messages API (api.anthropic.com).

    Written, mock-tested, NOT yet live-verified (no key on hand) — verify
    with a real call when a key is available.
    """

    def __init__(self, model: str = "", **kwargs):
        if not model:
            raise ProviderError("AnthropicProvider: SCORER_MODEL is required (no default model)")
        super().__init__(
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=model,
            **kwargs,
        )


class KimiProvider(_AnthropicMessagesProvider):
    """Kimi Coding endpoint — same Anthropic Messages wire format, different
    base_url/key/model. Base URL and model id confirmed from OpenClaw's
    (non-secret) provider config: base https://api.kimi.com/coding/,
    model id kimi-for-coding.
    """

    def __init__(self, model: str = "kimi-for-coding", **kwargs):
        super().__init__(
            base_url=os.getenv("KIMI_BASE_URL", "https://api.kimi.com/coding"),
            api_key=os.getenv("KIMI_API_KEY", ""),
            model=model,
            **kwargs,
        )


class OpenAIProvider(ModelProvider):
    """OpenAI Chat Completions API (api.openai.com).

    Written, mock-tested, NOT yet live-verified (no key on hand) — verify
    with a real call when a key is available.
    """

    def __init__(self, model: str = "", *, temperature: float = DEFAULT_TEMPERATURE,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: int = DEFAULT_TIMEOUT):
        if not model:
            raise ProviderError("OpenAIProvider: SCORER_MODEL is required (no default model)")
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    async def complete(self, system_prompt: str, user_content: str) -> dict:
        if not self.api_key:
            raise ProviderError("OpenAIProvider: no API key configured")

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAIProvider request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"OpenAIProvider API {resp.status_code}: {resp.text}")

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise ProviderError("OpenAIProvider: no choices in response")
        raw_text = choices[0].get("message", {}).get("content", "")
        if not raw_text:
            raise ProviderError("OpenAIProvider: empty message content in response")

        return _extract_json(raw_text)


_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "kimi": KimiProvider,
    "openai": OpenAIProvider,
}


def get_provider() -> ModelProvider:
    """Factory: reads SCORER_PROVIDER + SCORER_MODEL from .env and returns
    the configured adapter. Raises ProviderError on an unknown provider name
    rather than silently defaulting."""
    provider_name = os.getenv("SCORER_PROVIDER", "kimi").strip().lower()
    model = os.getenv("SCORER_MODEL", "").strip()

    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ProviderError(
            f"Unknown SCORER_PROVIDER '{provider_name}' — must be one of {sorted(_PROVIDERS)}"
        )

    if model:
        return provider_cls(model=model)
    return provider_cls()
