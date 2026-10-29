from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from realtime_translator.config import (
    ConfigurationError,
    load_translation_config,
    read_env_file,
)


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_directory.name) / ".env"

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_reads_quotes_comments_and_export(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "# comment",
                    "API_KEY='secret-value'",
                    "export API_MODEL=\"generic-model\"",
                    "TRANSLATION_RETRY_SECONDS=5,15,60,300",
                ]
            ),
            encoding="utf-8",
        )
        values = read_env_file(self.env_path)
        self.assertEqual(values["API_KEY"], "secret-value")
        self.assertEqual(values["API_MODEL"], "generic-model")

    def test_process_environment_overrides_env_file(self) -> None:
        self.env_path.write_text(
            "API_KEY=file-key\nAPI_MODEL=file-model\n",
            encoding="utf-8",
        )
        config = load_translation_config(
            self.env_path,
            environ={
                "API_KEY": "process-key",
                "API_MODEL": "process-model",
            },
        )
        self.assertEqual(config.api_key, "process-key")
        self.assertEqual(config.model, "process-model")

    def test_legacy_deepseek_names_still_work(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "DEEPSEEK_API_KEY=legacy-key",
                    "DEEPSEEK_BASE_URL=https://legacy.example/v1",
                    "DEEPSEEK_MODEL=legacy-model",
                    "DEEPSEEK_INPUT_PRICE_PER_MILLION=2",
                ]
            ),
            encoding="utf-8",
        )
        config = load_translation_config(self.env_path, environ={})
        self.assertEqual(config.api_key, "legacy-key")
        self.assertEqual(config.base_url, "https://legacy.example/v1")
        self.assertEqual(config.model, "legacy-model")
        self.assertEqual(config.input_price_per_million, 2.0)

    def test_generic_names_take_precedence_over_legacy_names(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "API_KEY=generic-key",
                    "API_BASE_URL=https://generic.example/v1",
                    "API_MODEL=generic-model",
                    "DEEPSEEK_API_KEY=legacy-key",
                    "DEEPSEEK_BASE_URL=https://legacy.example/v1",
                    "DEEPSEEK_MODEL=legacy-model",
                ]
            ),
            encoding="utf-8",
        )
        config = load_translation_config(self.env_path, environ={})
        self.assertEqual(config.api_key, "generic-key")
        self.assertEqual(config.base_url, "https://generic.example/v1")
        self.assertEqual(config.model, "generic-model")

    def test_defaults_remain_deepseek(self) -> None:
        config = load_translation_config(self.env_path, environ={})
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-chat")

    def test_prices_and_retry_schedule_are_configurable(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "API_KEY=key",
                    "API_INPUT_PRICE_PER_MILLION=2",
                    "API_OUTPUT_PRICE_PER_MILLION=8",
                    "TRANSLATION_RETRY_SECONDS=1,2,4",
                ]
            ),
            encoding="utf-8",
        )
        config = load_translation_config(self.env_path, environ={})
        self.assertEqual(config.retry_delays_seconds, (1.0, 2.0, 4.0))
        self.assertAlmostEqual(config.estimated_cost(1_000_000, 500_000), 6.0)

    def test_rejects_invalid_retry_schedule(self) -> None:
        self.env_path.write_text(
            "API_KEY=key\nTRANSLATION_RETRY_SECONDS=5,nope\n",
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError):
            load_translation_config(self.env_path, environ={})


if __name__ == "__main__":
    unittest.main()
