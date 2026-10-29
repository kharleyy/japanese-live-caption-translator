from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigurationError(ValueError):
    """Raised when a required local configuration value is invalid."""


@dataclass(frozen=True, slots=True)
class TranslationConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 512
    final_debounce_ms: int = 1500
    retry_delays_seconds: tuple[float, ...] = (5.0, 15.0, 60.0, 300.0)
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0

    def require_api_key(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError(
                "API_KEY is missing. Set API_KEY in .env before translating "
                "(the legacy name DEEPSEEK_API_KEY is also accepted)."
            )

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        return (
            input_tokens * self.input_price_per_million
            + output_tokens * self.output_price_per_million
        ) / 1_000_000


def read_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"invalid .env line: {raw_line!r}")

        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.fullmatch(key):
            raise ConfigurationError(f"invalid .env key: {key!r}")

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
            if raw_line.strip().endswith('"'):
                value = value.replace(r"\n", "\n").replace(r"\t", "\t")
        values[key] = value
    return values


def _configured_value(
    values: Mapping[str, str],
    generic_key: str,
    legacy_key: str,
    default: str = "",
) -> tuple[str, str]:
    if generic_key in values:
        return values[generic_key], generic_key
    if legacy_key in values:
        return values[legacy_key], legacy_key
    return default, generic_key


def _float_value(
    values: Mapping[str, str],
    generic_key: str,
    legacy_key: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw_value, configured_key = _configured_value(
        values,
        generic_key,
        legacy_key,
    )
    raw_value = raw_value.strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{configured_key} must be a number") from error
    if value < minimum:
        raise ConfigurationError(f"{configured_key} must be at least {minimum}")
    return value


def _retry_delays(values: Mapping[str, str]) -> tuple[float, ...]:
    raw_value = values.get("TRANSLATION_RETRY_SECONDS", "").strip()
    if not raw_value:
        return (5.0, 15.0, 60.0, 300.0)
    try:
        delays = tuple(float(part.strip()) for part in raw_value.split(","))
    except ValueError as error:
        raise ConfigurationError(
            "TRANSLATION_RETRY_SECONDS must be comma-separated numbers"
        ) from error
    if not delays or any(delay <= 0 for delay in delays):
        raise ConfigurationError("TRANSLATION_RETRY_SECONDS values must be positive")
    return delays


def _positive_int(
    values: Mapping[str, str],
    generic_key: str,
    legacy_key: str,
    default: int,
) -> int:
    raw_value, configured_key = _configured_value(
        values,
        generic_key,
        legacy_key,
    )
    raw_value = raw_value.strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{configured_key} must be an integer") from error
    if value <= 0:
        raise ConfigurationError(f"{configured_key} must be positive")
    return value


def load_translation_config(
    env_path: str | Path = ".env",
    *,
    environ: Mapping[str, str] | None = None,
) -> TranslationConfig:
    values = read_env_file(env_path)
    process_environment = os.environ if environ is None else environ
    for key, value in process_environment.items():
        if key.startswith(("API_", "DEEPSEEK_", "TRANSLATION_")):
            values[key] = value

    api_key, _ = _configured_value(
        values,
        "API_KEY",
        "DEEPSEEK_API_KEY",
    )
    base_url, base_url_key = _configured_value(
        values,
        "API_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com",
    )
    base_url = base_url.strip()
    if not base_url:
        raise ConfigurationError(f"{base_url_key} cannot be empty")
    model, model_key = _configured_value(
        values,
        "API_MODEL",
        "DEEPSEEK_MODEL",
        "deepseek-chat",
    )
    model = model.strip()
    if not model:
        raise ConfigurationError(f"{model_key} cannot be empty")

    return TranslationConfig(
        api_key=api_key.strip(),
        base_url=base_url.rstrip("/"),
        model=model,
        timeout_seconds=_float_value(
            values,
            "API_TIMEOUT_SECONDS",
            "DEEPSEEK_TIMEOUT_SECONDS",
            30.0,
            minimum=0.1,
        ),
        max_output_tokens=_positive_int(
            values,
            "API_MAX_OUTPUT_TOKENS",
            "DEEPSEEK_MAX_OUTPUT_TOKENS",
            512,
        ),
        final_debounce_ms=_positive_int(
            values,
            "FINAL_DEBOUNCE_MS",
            "FINAL_DEBOUNCE_MS",
            1500,
        ),
        retry_delays_seconds=_retry_delays(values),
        input_price_per_million=_float_value(
            values,
            "API_INPUT_PRICE_PER_MILLION",
            "DEEPSEEK_INPUT_PRICE_PER_MILLION",
            0.0,
            minimum=0.0,
        ),
        output_price_per_million=_float_value(
            values,
            "API_OUTPUT_PRICE_PER_MILLION",
            "DEEPSEEK_OUTPUT_PRICE_PER_MILLION",
            0.0,
            minimum=0.0,
        ),
    )
