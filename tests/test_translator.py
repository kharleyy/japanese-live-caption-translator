from __future__ import annotations

import io
import json
import socket
import unittest
from urllib.error import HTTPError, URLError

from realtime_translator.config import TranslationConfig
from realtime_translator.translator import (
    OpenAICompatibleTranslator,
    TranslationConfigurationError,
    TranslationContext,
    TranslationRequestError,
    TranslationTransientError,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TranslatorTests(unittest.TestCase):
    def config(self, *, api_key: str = "test-key") -> TranslationConfig:
        return TranslationConfig(
            api_key=api_key,
            input_price_per_million=2.0,
            output_price_per_million=8.0,
        )

    def test_successful_translation_parses_text_usage_and_cost(self) -> None:
        captured: dict[str, object] = {}

        def opener(request, *, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {
                    "choices": [
                        {"message": {"content": "大家好，晚上好！"}}
                    ],
                    "usage": {
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 500_000,
                    },
                }
            )

        translator = OpenAICompatibleTranslator(self.config(), opener=opener)
        result = translator.translate(
            "みなさん、こんばんは！",
            [
                TranslationContext(
                    source_ja="前に",
                    translation_zh="之前",
                )
            ],
        )

        self.assertEqual(result.translation_zh, "大家好，晚上好！")
        self.assertEqual(result.input_tokens, 1_000_000)
        self.assertEqual(result.output_tokens, 500_000)
        self.assertAlmostEqual(result.estimated_cost, 6.0)
        self.assertEqual(
            captured["url"],
            "https://api.deepseek.com/chat/completions",
        )
        payload = captured["payload"]
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertIn("前に", payload["messages"][1]["content"])

    def test_missing_api_key_fails_before_request(self) -> None:
        with self.assertRaises(TranslationConfigurationError):
            OpenAICompatibleTranslator(self.config(api_key=""))

    def test_authentication_error_is_not_retryable(self) -> None:
        def opener(request, *, timeout):
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"error":{"message":"bad key"}}'),
            )

        translator = OpenAICompatibleTranslator(self.config(), opener=opener)
        with self.assertRaises(TranslationRequestError) as raised:
            translator.translate("こんにちは")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "authentication_failed")
        self.assertNotIn("test-key", str(raised.exception))

    def test_server_and_timeout_errors_are_retryable(self) -> None:
        def server_error(request, *, timeout):
            raise HTTPError(
                request.full_url,
                503,
                "Unavailable",
                {},
                io.BytesIO(b"{}"),
            )

        with self.assertRaises(TranslationTransientError) as raised:
            OpenAICompatibleTranslator(
                self.config(),
                opener=server_error,
            ).translate("本文")
        self.assertTrue(raised.exception.retryable)

        def timeout_error(request, *, timeout):
            raise socket.timeout("timed out")

        with self.assertRaises(TranslationTransientError) as timed_out:
            OpenAICompatibleTranslator(
                self.config(),
                opener=timeout_error,
            ).translate("本文")
        self.assertTrue(timed_out.exception.retryable)

    def test_network_error_is_retryable(self) -> None:
        def opener(request, *, timeout):
            raise URLError("offline")

        with self.assertRaises(TranslationTransientError) as raised:
            OpenAICompatibleTranslator(
                self.config(),
                opener=opener,
            ).translate("本文")
        self.assertEqual(raised.exception.code, "network_error")


if __name__ == "__main__":
    unittest.main()
