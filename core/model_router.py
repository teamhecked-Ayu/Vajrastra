"""Zero-cost LLM routing across the Google Gemini free tier (stdlib only).

The router is deliberately small and boring so it runs anywhere Python runs:

* one HTTPS client built from :mod:`urllib.request`, no SDK, no pip install;
* the API key comes from ``GEMINI_API_KEY``/``GOOGLE_API_KEY`` in the
  environment, or -- on Google Colab -- from Colab Secrets;
* simple work is pinned to the lite model to preserve the daily quota, deeper
  work starts on the reasoning model;
* HTTP 429 (``RESOURCE_EXHAUSTED``) and transient server errors retry with
  exponential backoff on the current model and then cascade to the next free
  model in priority order, so a single exhausted quota never fails the run.

Priority order::

    gemini-3.8-flash       deep reasoning, complex coding, architecture
    gemini-3.6-flash       general coding and fallback
    gemini-3.5-flash-lite  fast extraction, summaries, lightweight steps
    gemma-4-31b-it         open-weight fallback

The capability pragma below is what lets :mod:`core.security_guard` waive the
network finding for this file; it is the narrowest possible grant.
"""

from __future__ import annotations

# vajra-allow: network, exfiltration

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
ENDPOINT_TEMPLATE = API_ROOT + "/models/{model}:generateContent"

DEEP_MODEL = "gemini-3.8-flash"
GENERAL_MODEL = "gemini-3.6-flash"
LIGHT_MODEL = "gemini-3.5-flash-lite"
OPEN_MODEL = "gemma-4-31b-it"

#: Free-tier priority order; the cascade never skips a more capable model.
MODEL_CHAIN: Tuple[str, ...] = (DEEP_MODEL, GENERAL_MODEL, LIGHT_MODEL, OPEN_MODEL)

KEY_ENV_VARS: Tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
USER_AGENT = "vajrastra/0.2 (+stdlib; zero-cost-model-router)"

DEFAULT_TIMEOUT = 90.0
DEFAULT_RETRIES = 2
DEFAULT_BASE_DELAY = 1.0
MAX_BACKOFF = 20.0

NO_KEY_MESSAGE = (
    "no GEMINI_API_KEY found in the environment or Colab Secrets; "
    "export GEMINI_API_KEY to enable model routing"
)

JSON_SYSTEM = (
    "You are a precise engineering assistant. Reply with a single JSON object "
    "and nothing else: no prose, no markdown fences."
)

# (status, lower-cased headers, body text)
Transport = Callable[[str, Mapping[str, str], bytes, float], Tuple[int, Mapping[str, str], str]]
Sleeper = Callable[[float], None]


class ModelRouterError(RuntimeError):
    """Raised when no model in the cascade could answer."""

    def __init__(self, message: str, trace: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.trace: Tuple[Mapping[str, Any], ...] = tuple(trace)


class ModelTier(str, Enum):
    """How much reasoning a task deserves."""

    DEEP = "deep"
    GENERAL = "general"
    LIGHT = "light"
    OPEN = "open"


TIER_MODELS: Mapping[ModelTier, str] = {
    ModelTier.DEEP: DEEP_MODEL,
    ModelTier.GENERAL: GENERAL_MODEL,
    ModelTier.LIGHT: LIGHT_MODEL,
    ModelTier.OPEN: OPEN_MODEL,
}

# Tasks that must not spend the expensive quota.
LIGHT_KEYWORDS: Tuple[str, ...] = (
    "extract", "summar", "summary", "classify", "label", "format", "rename",
    "translate", "quick", "boilerplate", "docstring", "typo", "lint", "snippet",
    "one-liner", "list the", "convert",
)

# Tasks that need the reasoning model first.
DEEP_KEYWORDS: Tuple[str, ...] = (
    "architecture", "architect", "design", "refactor", "algorithm", "prove",
    "proof", "optimi", "reason", "complex", "debug", "root cause", "security",
    "concurren", "distributed", "compiler", "mathemat", "derive", "invariant",
)


def resolve_api_key(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the Gemini key from the environment, then from Colab Secrets."""
    source = env if env is not None else os.environ
    for name in KEY_ENV_VARS:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return _colab_secret()


def _colab_secret() -> Optional[str]:
    """Read Colab Secrets lazily; absence of ``google.colab`` is normal."""
    try:  # pragma: no cover - only importable inside Google Colab
        from google.colab import userdata  # type: ignore
    except Exception:
        return None
    for name in KEY_ENV_VARS:
        try:
            value = userdata.get(name)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def route_tier(task: str) -> ModelTier:
    """Cheap deterministic classifier: light work stays on the lite model."""
    text = (task or "").lower()
    light = any(keyword in text for keyword in LIGHT_KEYWORDS)
    deep = any(keyword in text for keyword in DEEP_KEYWORDS)
    if deep and not (light and len(text) < 60):
        return ModelTier.DEEP
    if light:
        return ModelTier.LIGHT
    if len(text) > 1200:
        return ModelTier.DEEP
    return ModelTier.GENERAL


def choose_model(task: str) -> str:
    """Return the first model to try for *task*."""
    return TIER_MODELS[route_tier(task)]


def cascade_for(model: str) -> List[str]:
    """*model* first, then every lower-priority free model in order."""
    if model in MODEL_CHAIN:
        return list(MODEL_CHAIN[MODEL_CHAIN.index(model):])
    return [model, *MODEL_CHAIN]

def estimate_token_count(text: str) -> int:
    """Return a clearly-labelled rough estimate when provider counts are absent."""
    value = str(text or "")
    return (len(value.encode("utf-8")) + 3) // 4


def token_usage_report(prompt: str, response_text: str,
                       usage: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Separate provider-measured counts from rough character-based estimates."""
    metadata = usage if isinstance(usage, Mapping) else {}
    measured_input = metadata.get("promptTokenCount")
    measured_output = metadata.get("candidatesTokenCount")
    measured_total = metadata.get("totalTokenCount")

    def valid_count(value: Any) -> Optional[int]:
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return count if count >= 0 else None

    input_count = valid_count(measured_input)
    output_count = valid_count(measured_output)
    total_count = valid_count(measured_total)
    input_source = "measured" if input_count is not None else "estimated"
    output_source = "measured" if output_count is not None else "estimated"
    if input_count is None:
        input_count = estimate_token_count(prompt)
    if output_count is None:
        output_count = estimate_token_count(response_text)
    if total_count is None:
        total_count = input_count + output_count
        total_source = "estimated"
    else:
        total_source = "measured"
    sources = {input_source, output_source, total_source}
    return {
        "input_tokens": input_count,
        "output_tokens": output_count,
        "total_tokens": total_count,
        "input_source": input_source,
        "output_source": output_source,
        "total_source": total_source,
        "source": next(iter(sources)) if len(sources) == 1 else "mixed",
    }


def describe_routing() -> Dict[str, Any]:
    """Human-readable routing table for the CLI."""
    return {
        "chain": list(MODEL_CHAIN),
        "light": LIGHT_MODEL,
        "deep": DEEP_MODEL,
        "general": GENERAL_MODEL,
        "open": OPEN_MODEL,
    }


@dataclass(frozen=True)
class ModelResponse:
    """One successful completion."""

    text: str
    model: str
    tier: str
    attempts: int = 1
    latency_ms: float = 0.0
    usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    trace: Tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "tier": self.tier,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 1),
            "usage": dict(self.usage),
            "finish_reason": self.finish_reason,
            "trace": [dict(entry) for entry in self.trace],
        }

    def token_usage(self, prompt: str) -> Dict[str, Any]:
        """Provider counts when present; otherwise explicit rough estimates."""
        return token_usage_report(prompt, self.text, self.usage)


def _urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> Tuple[int, Mapping[str, str], str]:
    """POST *body* and return ``(status, headers, text)``; HTTP errors are data."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            return int(getattr(response, "status", 200)), _lower_headers(response.headers), payload
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return int(exc.code), _lower_headers(exc.headers), raw.decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise ModelRouterError(f"network error contacting the model API: {exc.reason}") from exc


def _lower_headers(headers: Any) -> Dict[str, str]:
    if not headers:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except (AttributeError, TypeError):
        return {}


def _extract_text(payload: Mapping[str, Any]) -> str:
    pieces: List[str] = []
    for candidate in payload.get("candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content") or {}
        if isinstance(content, Mapping):
            for part in content.get("parts") or ():
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
    return "".join(pieces).strip()


def _finish_reason(payload: Mapping[str, Any]) -> str:
    for candidate in payload.get("candidates") or ():
        if isinstance(candidate, Mapping) and candidate.get("finishReason"):
            return str(candidate["finishReason"])
    feedback = payload.get("promptFeedback") or {}
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        return str(feedback["blockReason"])
    return ""


def _error_detail(payload: Any, body: str) -> str:
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            status = error.get("status") or ""
            message = error.get("message") or ""
            return f"{status} {message}".strip()
    return (body or "").strip()[:300]


def build_payload(
    model: str,
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Request body; Gemma has no ``systemInstruction`` so it is prepended."""
    text = prompt
    payload: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }
    if system:
        if model.startswith("gemini"):
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        else:
            payload["contents"][0]["parts"] = [
                {"text": system + "\n\n" + prompt},
            ]
    generation: Dict[str, Any] = {}
    if temperature is not None:
        generation["temperature"] = float(temperature)
    if max_output_tokens is not None:
        generation["maxOutputTokens"] = int(max_output_tokens)
    if generation:
        payload["generationConfig"] = generation
    return payload


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating code fences."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ModelRouterError("model reply did not contain a JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelRouterError(f"model reply was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelRouterError("model reply JSON was not an object")
    return parsed


class ModelRouter:
    """Free-tier model cascade with quota-aware routing."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        transport: Optional[Transport] = None,
        sleeper: Optional[Sleeper] = None,
        clock: Optional[Callable[[], float]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = MAX_BACKOFF,
        jitter: float = 0.25,
        endpoint: str = ENDPOINT_TEMPLATE,
        env: Optional[Mapping[str, str]] = None,
        random_source: Optional[random.Random] = None,
    ) -> None:
        self._explicit_key = api_key.strip() if api_key else None
        self._env = env
        self._transport = transport or _urllib_transport
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._random = random_source or random.Random(0)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.jitter = max(0.0, float(jitter))
        self.endpoint = endpoint

    # ------------------------------------------------------------------ config
    @property
    def available(self) -> bool:
        return bool(self.api_key())

    def api_key(self) -> Optional[str]:
        if self._explicit_key:
            return self._explicit_key
        return resolve_api_key(self._env)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "configured" if self.available else "missing-key"
        return f"ModelRouter({state}, chain={len(MODEL_CHAIN)} models)"

    def _require_key(self) -> str:
        key = self.api_key()
        if not key:
            raise ModelRouterError(NO_KEY_MESSAGE)
        return key

    # ---------------------------------------------------------------- requests
    def _headers(self, key: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
            "User-Agent": USER_AGENT,
        }

    def _delay(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** attempt))
        if self.jitter:
            delay += self._random.uniform(0.0, self.jitter * delay)
        return delay

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None

    def generate(
        self,
        prompt: str,
        *,
        tier: Optional[str | ModelTier] = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> ModelResponse:
        """Ask for a completion, cascading across the free-tier chain on 429."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        key = self._require_key()

        if model:
            first = model
            chosen_tier = ModelTier.OPEN.value if model not in MODEL_CHAIN else _tier_of(model).value
        elif tier is not None:
            chosen_tier = _coerce_tier(tier).value
            first = TIER_MODELS[_coerce_tier(tier)]
        else:
            routed = route_tier(text)
            chosen_tier = routed.value
            first = TIER_MODELS[routed]

        chain = cascade_for(first)
        trace: List[Dict[str, Any]] = []
        attempts_total = 0
        wait_timeout = float(timeout if timeout is not None else self.timeout)

        for candidate in chain:
            payload = json.dumps(
                build_payload(
                    candidate,
                    text,
                    system=system,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            ).encode("utf-8")
            url = self.endpoint.format(model=candidate)
            for attempt in range(self.retries + 1):
                attempts_total += 1
                started = self._clock()
                try:
                    status, headers, body = self._transport(
                        url, self._headers(key), payload, wait_timeout
                    )
                except ModelRouterError as exc:
                    trace.append(
                        {
                            "model": candidate,
                            "attempt": attempt + 1,
                            "status": 0,
                            "detail": str(exc),
                        }
                    )
                    break
                latency_ms = (self._clock() - started) * 1000.0
                parsed: Any = None
                try:
                    parsed = json.loads(body) if body.strip() else {}
                except json.JSONDecodeError:
                    parsed = None

                if status == 200 and isinstance(parsed, Mapping):
                    reply = _extract_text(parsed)
                    if reply:
                        trace.append(
                            {
                                "model": candidate,
                                "attempt": attempt + 1,
                                "status": status,
                                "latency_ms": round(latency_ms, 1),
                            }
                        )
                        usage = parsed.get("usageMetadata") or {}
                        return ModelResponse(
                            text=reply,
                            model=candidate,
                            tier=chosen_tier,
                            attempts=attempts_total,
                            latency_ms=latency_ms,
                            usage=usage if isinstance(usage, Mapping) else {},
                            finish_reason=_finish_reason(parsed),
                            trace=tuple(trace),
                        )

                detail = _error_detail(parsed, body)
                exhausted = status == 429 or "RESOURCE_EXHAUSTED" in detail.upper()
                trace.append(
                    {
                        "model": candidate,
                        "attempt": attempt + 1,
                        "status": status,
                        "detail": detail,
                        "quota": exhausted,
                    }
                )

                if exhausted:
                    if attempt < self.retries:
                        hint = self._retry_after(headers)
                        self._sleeper(
                            min(self.max_delay, hint) if hint is not None else self._delay(attempt)
                        )
                        continue
                    break  # quota gone on this model: cascade
                if status >= 500:
                    if attempt < self.retries:
                        self._sleeper(self._delay(attempt))
                        continue
                    break
                if status in (403, 404):
                    break  # model unavailable for this key/region: cascade
                if status == 401:
                    raise ModelRouterError(
                        "Gemini rejected the API key (HTTP 401); check GEMINI_API_KEY",
                        trace,
                    )
                if status == 400:
                    raise ModelRouterError(
                        "Gemini rejected the request (HTTP 400): " + (detail or "bad request"),
                        trace,
                    )
                if not isinstance(parsed, Mapping):
                    break
                raise ModelRouterError(
                    f"unexpected model API response (HTTP {status}): " + (detail or "no detail"),
                    trace,
                )

        summary = "; ".join(
            f"{entry['model']}#{entry['attempt']}={entry.get('status')}"
            for entry in trace[-8:]
        )
        raise ModelRouterError(
            "every free-tier model is exhausted or unavailable (" + (summary or "no attempts") + ")",
            trace,
        )

    def generate_json(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[Dict[str, Any], ModelResponse]:
        """Completion constrained to a JSON object; returns ``(object, response)``."""
        response = self.generate(prompt, system=system or JSON_SYSTEM, **kwargs)
        return parse_json_object(response.text), response


def _coerce_tier(tier: str | ModelTier) -> ModelTier:
    if isinstance(tier, ModelTier):
        return tier
    token = str(tier).strip().lower()
    for candidate in ModelTier:
        if candidate.value == token:
            return candidate
    raise ValueError(f"unknown model tier: {tier!r} (expected deep|general|light|open)")


def _tier_of(model: str) -> ModelTier:
    for tier, name in TIER_MODELS.items():
        if name == model:
            return tier
    return ModelTier.OPEN
