from __future__ import annotations

import http.client
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from realtime_translator.database import Database
from realtime_translator.history_server import HistoryServer
from realtime_translator.segmenter import CaptionSegmenter
from realtime_translator.translation_worker import TranslationWorker
from realtime_translator.translator import TranslationResult


class PrefixTranslator:
    def translate(self, source_ja, context=()):
        return TranslationResult(
            translation_zh=f"译:{source_ja}",
            input_tokens=20,
            output_tokens=8,
            estimated_cost=0.0002,
        )


class EndToEndOfflineTests(unittest.TestCase):
    def test_caption_to_translation_to_history_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            database_path = root / "integration.sqlite3"
            start = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)
            segmenter = CaptionSegmenter(stable_after_seconds=1.5)
            completed = []

            completed.extend(
                segmenter.process_lines(
                    ["最初の文", "途中"],
                    start,
                ).completed
            )
            completed.extend(
                segmenter.process_lines(
                    ["最初の文", "途中の文", "最後の文"],
                    start + timedelta(seconds=1),
                ).completed
            )
            completed.extend(
                segmenter.flush(start + timedelta(seconds=3))
            )

            with Database(database_path) as database:
                session = database.create_session(
                    "联调会话",
                    started_at=start,
                )
                for index, segment in enumerate(completed):
                    database.add_finalized_segment(
                        session.id,
                        segment.source_ja,
                        finalized_at=start + timedelta(seconds=index),
                    )

                worker = TranslationWorker(database, PrefixTranslator())
                summary = worker.run_once(
                    now=start + timedelta(seconds=4),
                )
                self.assertEqual(summary.completed, 2)
                self.assertEqual(
                    [item.source_ja for item in database.list_segments_for_date(
                        "2026-10-24"
                    )],
                    ["途中の文", "最後の文"],
                )

            server = HistoryServer(database_path, port=0)
            server.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.port,
                    timeout=3,
                )
                connection.request("GET", "/?date=2026-10-24")
                response = connection.getresponse()
                page = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertIn("联调会话", page)
                self.assertIn("途中の文", page)
                self.assertIn("译:途中の文", page)
                self.assertNotIn("最初の文", page)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
