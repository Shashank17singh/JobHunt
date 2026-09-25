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
        import time
        for attempt in range(5):
            r = requests.post(
                f"{self.BASE}/{model}:generateContent",
                params={"key": self._env("GEMINI_API_KEY")},
                json=body,
                timeout=TIMEOUT,
            )
            if r.status_code in (429, 503, 500) and attempt < 4:
                print(f"  gemini HTTP {r.status_code}, retrying in {4 * (attempt + 1)}s...")
                time.sleep(4 * (attempt + 1))
                continue
            elif r.status_code != 200:
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
            
            time.sleep(4)  # Prevent burst rate limits
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







PROVIDERS = {
    "gemini": GeminiProvider,
}

DEFAULT_MODELS = {
    "gemini": {"screen": "gemini-3.8-flash", "draft": "gemini-3.1-pro-preview"},
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
