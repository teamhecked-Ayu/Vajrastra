"""Provider adapter contract and registry.

The current router is a model cascade inside one provider. This module
defines the multi-provider boundary the blueprint requires: one adapter
contract (generate / usage / errors / capabilities), honest failure
when a provider or capability is unavailable, and fallback to another
provider only when that provider supports the task AND the user's data
policy allows it. Usage is reported as measured only when the provider
returns it; otherwise it stays None.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaError(ProviderError):
    pass


class AuthError(ProviderError):
    pass


class TransientError(ProviderError):
    pass


class UnsupportedCapabilityError(ProviderError):
    pass


@dataclass
class Usage:
    input_tokens: int | None = None     # None = provider did not report
    output_tokens: int | None = None


@dataclass
class ProviderResponse:
    text: str
    usage: Usage
    model: str


@dataclass
class TaskSpec:
    task_type: str = "general"          # general | code | research | long_context
    needs_tools: bool = False
    min_context_tokens: int = 0
    allow_data_share: bool = True       # user data policy gate for fallback


class ProviderAdapter(ABC):
    name: str = "abstract"
    models: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset({"generate"})

    @abstractmethod
    def generate(self, prompt: str, *, model: str | None = None,
                 system: str = "") -> ProviderResponse:
        ...

    def supports(self, task: TaskSpec) -> bool:
        if (task.task_type not in self.capabilities
                and "general" not in self.capabilities):
            return False
        if task.needs_tools and "tools" not in self.capabilities:
            return False
        return True

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)   # estimate, never a measurement


class GeminiProvider(ProviderAdapter):
    name = "gemini"
    models = ("gemini-2.0-flash", "gemini-1.5-flash", "gemma-2-9b-it")
    capabilities = frozenset({"generate", "tools", "long_context"})

    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        if api_key is None:
            api_key = os.environ.get("VAJASTRA_GEMINI_API_KEY", "")
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def _classify(status: int, body: str) -> ProviderError:
        if status == 429:
            retry = None
            try:
                retry = float(json.loads(body).get("error", {}).get("retryAfter", 0)) or None
            except Exception:
                pass
            return QuotaError(f"gemini quota exceeded (HTTP {status})", retry_after=retry)
        if status in (401, 403):
            return AuthError(f"gemini auth failed (HTTP {status})")
        if status >= 500:
            return TransientError(f"gemini server error (HTTP {status})")
        return ProviderError(f"gemini request failed (HTTP {status}): {body[:200]}")

    def generate(self, prompt: str, *, model: str | None = None,
                 system: str = "") -> ProviderResponse:
        if not self.api_key:
            raise AuthError("VAJASTRA_GEMINI_API_KEY is not set; failing honestly instead of guessing")
        model = model or self.models[0]
        payload: dict = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={self.api_key}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise self._classify(e.code, e.read().decode(errors="replace")) from None
        except urllib.error.URLError as e:
            raise TransientError(f"network error reaching gemini: {e.reason}") from None
        text = "".join(
            part.get("text", "")
            for cand in data.get("candidates", [])
            for part in cand.get("content", {}).get("parts", [])
        )
        um = data.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=um.get("promptTokenCount"),
            output_tokens=um.get("candidatesTokenCount"),
        )
        return ProviderResponse(text=text, usage=usage, model=model)


class ProviderRegistry:
    """Capability routing with quota-aware, policy-gated fallback."""

    def __init__(self, data_policy_allows_fallback: bool = True):
        self._adapters: dict[str, ProviderAdapter] = {}
        self._order: list[str] = []
        self.data_policy = data_policy_allows_fallback

    def register(self, adapter: ProviderAdapter, primary: bool = False) -> None:
        if adapter.name not in self._adapters:
            self._order.append(adapter.name)
        self._adapters[adapter.name] = adapter
        if primary:
            self._order.remove(adapter.name)
            self._order.insert(0, adapter.name)

    def route(self, prompt: str, task: TaskSpec, system: str = "") -> ProviderResponse:
        candidates = [self._adapters[n] for n in self._order]
        if not candidates:
            raise ProviderError("no provider adapters registered")
        last_quota: QuotaError | None = None
        used_fallback = False
        for adapter in candidates:
            if not adapter.supports(task):
                continue
            if used_fallback and not self.data_policy:
                raise ProviderError("fallback blocked by user data policy")
            try:
                return adapter.generate(prompt, system=system)
            except QuotaError as e:
                last_quota = e
                if e.retry_after:
                    time.sleep(min(e.retry_after, 30.0))   # bounded backoff
                used_fallback = True
                continue
            except TransientError:
                used_fallback = True
                continue
        if last_quota:
            raise last_quota
        raise ProviderError("no registered provider supports this task; failing honestly")
