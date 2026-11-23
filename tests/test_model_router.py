"""Model-router tests: free-tier routing, 429 cascade, backoff, JSON helpers.

No test ever touches the network: every call goes through a scripted transport.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.model_router import (  # noqa: E402
    DEEP_MODEL,
    GENERAL_MODEL,
    LIGHT_MODEL,
    MODEL_CHAIN,
    OPEN_MODEL,
    ModelRouter,
    ModelRouterError,
    ModelTier,
    build_payload,
    cascade_for,
    choose_model,
    parse_json_object,
    resolve_api_key,
    route_tier,
    token_usage_report,
)

KEY = "unit-test-key"


def completion(text: str) -> str:
    return json.dumps(
        {
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"totalTokenCount": 7},
        }
    )


def quota_message() -> str:
    return json.dumps(
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}
    )


class ScriptedTransport:
    """Return pre-scripted responses and record every request."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "body": json.loads(body.decode("utf-8"))}
        )
        if not self.script:
            raise AssertionError("transport called more times than scripted")
        status, payload, extra = self.script.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return status, extra or {}, text


def make_router(script, **kwargs):
    sleeper_calls = []
    transport = ScriptedTransport(script)
    router = ModelRouter(
        KEY,
        transport=transport,
        sleeper=sleeper_calls.append,
        jitter=0.0,
        **kwargs,
    )
    return router, transport, sleeper_calls


class RoutingTests(unittest.TestCase):
    def test_simple_task_pins_the_lite_model(self) -> None:
        self.assertEqual(route_tier("summarise this stack trace"), ModelTier.LIGHT)
        self.assertEqual(choose_model("extract the field names"), LIGHT_MODEL)

    def test_deep_task_starts_on_the_reasoning_model(self) -> None:
        self.assertEqual(
            route_tier("design the architecture for a distributed scheduler"), ModelTier.DEEP
        )
        self.assertEqual(route_tier("prove this invariant holds"), ModelTier.DEEP)
        self.assertEqual(choose_model("debug the race condition"), DEEP_MODEL)

    def test_general_task_uses_general_model(self) -> None:
        self.assertEqual(route_tier("write a function that adds two numbers"), ModelTier.GENERAL)
        self.assertEqual(choose_model("write a function that adds two numbers"), GENERAL_MODEL)

    def test_cascade_order(self) -> None:
        self.assertEqual(cascade_for(DEEP_MODEL), list(MODEL_CHAIN))
        self.assertEqual(cascade_for(LIGHT_MODEL), [LIGHT_MODEL, OPEN_MODEL])
        self.assertEqual(cascade_for("custom-model"), ["custom-model", *MODEL_CHAIN])

    def test_key_resolution_prefers_environment(self) -> None:
        self.assertEqual(resolve_api_key({"GEMINI_API_KEY": "  abc  "}), "abc")
        self.assertEqual(resolve_api_key({"GOOGLE_API_KEY": "g"}), "g")


class RequestTests(unittest.TestCase):
    def test_generate_uses_header_key_and_expected_endpoint(self) -> None:
        router, transport, _ = make_router([(200, completion("hello"), None)])
        response = router.generate("write a function that adds two numbers")
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.model, GENERAL_MODEL)
        self.assertEqual(response.tier, ModelTier.GENERAL.value)
        call = transport.calls[0]
        self.assertIn(GENERAL_MODEL, call["url"])
        self.assertIn("/v1beta/models/", call["url"])
        self.assertNotIn(KEY, call["url"])
        self.assertNotIn("key=", call["url"])
        self.assertEqual(call["headers"]["x-goog-api-key"], KEY)
        self.assertEqual(call["body"]["contents"][0]["parts"][0]["text"],
                         "write a function that adds two numbers")

    def test_tier_override_wins_over_classifier(self) -> None:
        router, transport, _ = make_router([(200, completion("ok"), None)])
        router.generate("design a compiler architecture", tier="light")
        self.assertIn(LIGHT_MODEL, transport.calls[0]["url"])

    def test_system_instruction_for_gemini(self) -> None:
        router, transport, _ = make_router([(200, completion("ok"), None)])
        router.generate("hello", system="be terse", temperature=0.2, max_output_tokens=64)
        body = transport.calls[0]["body"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "be terse")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 64)
        self.assertAlmostEqual(body["generationConfig"]["temperature"], 0.2)

    def test_open_weight_model_gets_prepended_system_text(self) -> None:
        payload = build_payload(OPEN_MODEL, "hello", system="be terse")
        self.assertNotIn("systemInstruction", payload)
        self.assertTrue(payload["contents"][0]["parts"][0]["text"].startswith("be terse"))

    def test_empty_prompt_is_a_value_error(self) -> None:
        router, _, _ = make_router([])
        with self.assertRaises(ValueError):
            router.generate("   ")

    def test_unknown_tier_is_rejected(self) -> None:
        router, _, _ = make_router([])
        with self.assertRaises(ValueError):
            router.generate("hello", tier="galactic")


class QuotaTests(unittest.TestCase):
    def test_429_cascades_after_retries(self) -> None:
        script = [(429, quota_message(), None)] * 3 + [(200, completion("served"), None)]
        router, transport, delays = make_router(script, retries=2, base_delay=1.0)
        response = router.generate("write a function that adds two numbers")
        self.assertEqual(response.text, "served")
        self.assertEqual(response.model, LIGHT_MODEL)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(delays, [1.0, 2.0])
        self.assertEqual(response.attempts, 4)

    def test_retry_after_header_is_honoured(self) -> None:
        script = [
            (429, quota_message(), {"retry-after": "7"}),
            (200, completion("served"), None),
        ]
        router, _, delays = make_router(script, retries=2)
        router.generate("write a function that adds two numbers")
        self.assertEqual(delays, [7.0])

    def test_all_models_exhausted_raises_with_trace(self) -> None:
        script = [(429, quota_message(), None)] * (3 * len(MODEL_CHAIN))
        router, _, _ = make_router(script, retries=2)
        with self.assertRaises(ModelRouterError) as caught:
            router.generate("summarise this")
        self.assertIn("exhausted", str(caught.exception))
        self.assertTrue(caught.exception.trace)
        self.assertEqual(caught.exception.trace[-1]["status"], 429)

    def test_missing_model_falls_through(self) -> None:
        script = [
            (404, {"error": {"status": "NOT_FOUND", "message": "no such model"}}, None),
            (200, completion("fallback"), None),
        ]
        router, _, _ = make_router(script)
        response = router.generate("summarise this")
        self.assertEqual(response.model, OPEN_MODEL)
        self.assertEqual(response.text, "fallback")

    def test_server_error_retries_then_cascades(self) -> None:
        script = [(503, "overloaded", None)] * 3 + [(200, completion("ok"), None)]
        router, _, delays = make_router(script, retries=2, base_delay=0.5)
        response = router.generate("summarise this")
        self.assertEqual(response.text, "ok")
        self.assertEqual(delays, [0.5, 1.0])

    def test_bad_request_stops_immediately(self) -> None:
        router, transport, _ = make_router(
            [(400, {"error": {"status": "INVALID_ARGUMENT", "message": "bad"}}, None)]
        )
        with self.assertRaises(ModelRouterError):
            router.generate("write code")
        self.assertEqual(len(transport.calls), 1)

    def test_missing_key_is_reported_clearly(self) -> None:
        router = ModelRouter(env={}, transport=ScriptedTransport([]))
        self.assertFalse(router.available)
        with self.assertRaises(ModelRouterError) as caught:
            router.generate("hello")
        self.assertIn("GEMINI_API_KEY", str(caught.exception))


class TokenUsageTests(unittest.TestCase):
    def test_provider_counts_are_marked_measured(self):
        report = token_usage_report(
            "prompt text", "answer text",
            {"promptTokenCount": 11, "candidatesTokenCount": 7, "totalTokenCount": 18},
        )
        self.assertEqual(report["input_tokens"], 11)
        self.assertEqual(report["output_tokens"], 7)
        self.assertEqual(report["total_tokens"], 18)
        self.assertEqual(report["source"], "measured")

    def test_missing_provider_counts_are_explicit_estimates(self):
        report = token_usage_report("1234", "abcd", {})
        self.assertEqual(report["input_tokens"], 1)
        self.assertEqual(report["output_tokens"], 1)
        self.assertEqual(report["total_tokens"], 2)
        self.assertEqual(report["source"], "estimated")


class JsonTests(unittest.TestCase):
    def test_generate_json_parses_fenced_reply(self) -> None:
        fenced = '```json\n{"answer": 42}\n```'
        router, _, _ = make_router([(200, completion(fenced), None)])
        data, response = router.generate_json("extract the answer")
        self.assertEqual(data["answer"], 42)
        self.assertEqual(response.model, LIGHT_MODEL)

    def test_parse_json_object_rejects_prose(self) -> None:
        with self.assertRaises(ModelRouterError):
            parse_json_object("no json here")

    def test_parse_json_finds_embedded_object(self) -> None:
        self.assertEqual(parse_json_object('prefix {"a": 1} suffix')["a"], 1)


if __name__ == "__main__":
    unittest.main()
