"""Provider-agnostic LLM clients.

One tiny interface, multiple backends. Screening and drafting each pick
their own provider from env vars.
Pin selection to current chat prompt (Ctrl+Alt+X) | Don't

    complete(system, user)            -> str   (every provider)

Nothing here parses JSON or knows what a Job is. That lives in llm.py.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import requests

TIMEOUT = 120


class LLMError(RuntimeError):
    """Anything that came back wrong from a provider."""



# ---------------------------------------------------------------------------


class Provider:
    name = "base"
    required_env: str | None = None

    def preflight(self) -> None:
        """Fail before the first call, not on batch 1 of 40."""
        if self.required_env:
            self._env(self.required_env)

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        """`json_mode` asks the provider to guarantee valid JSON where it can."""
        raise NotImplementedError



    @staticmethod
    def _env(key: str) -> str:
        value = (os.environ.get(key) or "").strip()
        if not value:
            raise LLMError(f"{key} is not set (see .env.example)")
        return value


class GeminiProvider(Provider):
    name = "gemini"
    required_env = "GEMINI_API_KEY"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def _post(self, model: str, body: dict) -> str:
        r = requests.post(
            f"{self.BASE}/{model}:generateContent",
            params={"key": self._env("GEMINI_API_KEY")},
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise LLMError(f"gemini HTTP {r.status_code}: {r.text[:300]}")
        try:
            candidate = r.json()["candidates"][0]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"gemini returned no candidates: {r.text[:300]}") from e

        reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if reason == "MAX_TOKENS" or (not text and reason not in (None, "STOP")):
            raise LLMError(
                f"gemini stopped early (finishReason={reason}) with "
                f"{len(text)} chars of output — raise max_tokens for this stage"
            )
        if not text:
            raise LLMError(f"gemini returned no text: {r.text[:300]}")
        return text

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens, "temperature": 0.2}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        return self._post(model, body)




class OpenAICompatProvider(Provider):
    name = "openai-compatible"
    required_env = "OPENAI_API_KEY"
    default_base = "https://api.openai.com/v1"
    key_env = "OPENAI_API_KEY"

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        base = os.getenv("LLM_BASE_URL", self.default_base).rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {self._env(self.key_env)}"},
            json=payload,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise LLMError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.name} malformed reply: {r.text[:300]}") from e


class OllamaProvider(Provider):
    name = "ollama"

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        base = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = requests.post(
                f"{base}/api/chat", json=payload, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise LLMError(
                f"ollama unreachable at {base} - is `ollama serve` running?") from e
        if r.status_code != 200:
            raise LLMError(f"ollama HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["message"]["content"]
        except (KeyError, ValueError) as e:
            raise LLMError(f"ollama malformed reply: {r.text[:300]}") from e


PROVIDERS = {
    "gemini": GeminiProvider,
    "openai-compatible": OpenAICompatProvider,
    "ollama": OllamaProvider,
}

DEFAULT_MODELS = {
    "gemini": {"screen": "gemini-3.6-flash", "draft": "gemini-3.6-flash"},
    "openai-compatible": {"screen": "gpt-4o-mini", "draft": "gpt-4o"},
    "ollama": {"screen": "llama3.1", "draft": "llama3.1"},
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise LLMError(
            f"unknown provider {name!r}; pick one of {', '.join(PROVIDERS)}"
        ) from None


def resolve(stage: str, check: bool = True) -> tuple[Provider, str]:
    name = (os.getenv(f"{stage.upper()}_PROVIDER")
            or os.getenv("LLM_PROVIDER")
            or "gemini").strip().lower()
    provider = get_provider(name)
    model = (os.getenv(f"{stage.upper()}_MODEL") or "").strip() \
        or DEFAULT_MODELS.get(name, {}).get(stage)
    if not model:
        raise LLMError(f"set {stage.upper()}_MODEL for provider {name!r}")
    if check:
        provider.preflight()
    return provider, model
