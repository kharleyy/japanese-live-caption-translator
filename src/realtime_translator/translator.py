from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from realtime_translator.config import ConfigurationError, TranslationConfig

SYSTEM_PROMPT = """你是日语直播实时字幕翻译器。请把给出的日语原句翻译成自然、简洁的简体中文口语。
尽量保留主播语气、称呼和网络梗，不要生硬直译。不要补充原文没有的信息。
括号中的笑声、音乐、掌声等非语言提示应保留，不要改写成普通对话。只输出译文。"""


@dataclass(frozen=True, slots=True)
class TranslationContext:
    source_ja: str
    translation_zh: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translation_zh: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float


class TranslationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class TranslationConfigurationError(TranslationError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="configuration_error",
            retryable=False,
        )


class TranslationRequestError(TranslationError):
    def __init__(self, message: str, *, code: str, http_status: int) -> None:
        super().__init__(
            message,
            code=code,
            retryable=False,
            http_status=http_status,
        )


class TranslationTransientError(TranslationError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            retryable=True,
            http_status=http_status,
        )


class OpenAICompatibleTranslator:
    """Minimal synchronous client for OpenAI-compatible chat endpoints."""

    def __init__(
        self,
        config: TranslationConfig,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not config.api_key.strip():
            raise TranslationConfigurationError(
                "API_KEY is missing; configure .env before translating"
            )
        self.config = config
        self._opener = opener

    @property
    def endpoint(self) -> str:
        return f"{self.config.base_url}/chat/completions"

    def translate(
        self,
        source_ja: str,
        context: Sequence[TranslationContext] = (),
    ) -> TranslationResult:
        source = " ".join(source_ja.split()).strip()
        if not source:
            raise ValueError("source_ja cannot be empty")

        context_payload = [
            {"ja": item.source_ja, "zh": item.translation_zh} for item in context
        ]
        user_content = (
            "以下是最近的稳定双语上下文，仅用于保持指代和语气一致：\n"
            + json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"))
            + "\n\n待翻译日语：\n"
            + source
        )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with self._opener(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw_body = response.read()
        except HTTPError as error:
            raise self._http_error(error) from error
        except (TimeoutError, socket.timeout) as error:
            raise TranslationTransientError(
                "API request timed out",
                code="timeout",
            ) from error
        except URLError as error:
            raise TranslationTransientError(
                f"API network error: {error.reason}",
                code="network_error",
            ) from error

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TranslationTransientError(
                "API returned an invalid JSON response",
                code="invalid_response",
            ) from error

        try:
            translation = str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as error:
            raise TranslationTransientError(
                "API response did not contain translated text",
                code="invalid_response",
            ) from error
        if not translation:
            raise TranslationTransientError(
                "API returned an empty translation",
                code="empty_translation",
            )

        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = self._token_count(
            usage,
            "prompt_tokens",
            "input_tokens",
        )
        output_tokens = self._token_count(
            usage,
            "completion_tokens",
            "output_tokens",
        )
        return TranslationResult(
            translation_zh=translation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=self.config.estimated_cost(
                input_tokens,
                output_tokens,
            ),
        )

    @staticmethod
    def _token_count(usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _http_error(error: HTTPError) -> TranslationError:
        status = int(error.code)
        detail = ""
        try:
            body = error.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                error_body = parsed.get("error")
                if isinstance(error_body, dict):
                    detail = str(error_body.get("message") or "")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

        suffix = f": {detail[:300]}" if detail else ""
        if status in {401, 403}:
            return TranslationRequestError(
                f"API authentication failed (HTTP {status}){suffix}",
                code="authentication_failed",
                http_status=status,
            )
        if status == 404:
            return TranslationRequestError(
                f"API model or endpoint was not found (HTTP 404){suffix}",
                code="model_or_endpoint_not_found",
                http_status=status,
            )
        if status == 429:
            return TranslationTransientError(
                f"API rate limit reached (HTTP 429){suffix}",
                code="rate_limited",
                http_status=status,
            )
        if status >= 500:
            return TranslationTransientError(
                f"API service error (HTTP {status}){suffix}",
                code="server_error",
                http_status=status,
            )
        if status in {408, 409, 425}:
            return TranslationTransientError(
                f"API temporary HTTP error {status}{suffix}",
                code="temporary_http_error",
                http_status=status,
            )
        return TranslationRequestError(
            f"API rejected the request (HTTP {status}){suffix}",
            code="request_rejected",
            http_status=status,
        )


# Kept for compatibility with code written before the provider-neutral rename.
DeepSeekTranslator = OpenAICompatibleTranslator
