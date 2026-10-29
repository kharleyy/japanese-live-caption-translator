from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from realtime_translator.database import Database
from realtime_translator.translation_worker import TranslationWorker
from realtime_translator.translator import (
    TranslationRequestError,
    TranslationResult,
    TranslationTransientError,
)


class RecordingClient:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, int]] = []

    def translate(self, source_ja, context=()):
        self.calls.append((source_ja, len(context)))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return TranslationResult(
            translation_zh=f"译:{source_ja}",
            input_tokens=10,
            output_tokens=4,
            estimated_cost=0.0001,
        )


class TranslationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name) / "test.sqlite3"
        self.database = Database(self.db_path)
        self.session = self.database.create_session(
            "翻译测试",
            started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp_directory.cleanup()

    def test_translates_pending_segments_in_order(self) -> None:
        first = self.database.add_finalized_segment(self.session.id, "一")
        second = self.database.add_finalized_segment(self.session.id, "二")
        client = RecordingClient()
        worker = TranslationWorker(self.database, client)

        summary = worker.run_once(now="2026-10-24T01:00:00+00:00")

        self.assertEqual(summary.attempted, 2)
        self.assertEqual(summary.completed, 2)
        self.assertEqual(self.database.get_segment(first.id).translation_zh, "译:一")
        self.assertEqual(self.database.get_segment(second.id).translation_zh, "译:二")
        self.assertEqual(summary.usage.successes, 2)

    def test_transient_failure_schedules_retry_and_stops_batch(self) -> None:
        first = self.database.add_finalized_segment(self.session.id, "一")
        self.database.add_finalized_segment(self.session.id, "二")
        client = RecordingClient(
            [
                TranslationTransientError(
                    "offline",
                    code="network_error",
                )
            ]
        )
        worker = TranslationWorker(
            self.database,
            client,
            retry_delays_seconds=(5, 15),
        )

        summary = worker.run_once(now="2026-10-24T01:00:00+00:00")

        segment = self.database.get_segment(first.id)
        self.assertEqual(summary.transient_failures, 1)
        self.assertEqual(segment.retry_count, 1)
        self.assertEqual(segment.next_retry_at_utc, "2026-10-24T01:00:05.000+00:00")
        self.assertEqual(len(client.calls), 1)

        before_due = worker.run_once(now="2026-10-24T01:00:04+00:00")
        self.assertEqual(before_due.attempted, 0)

    def test_restart_does_not_skip_earlier_segment_waiting_to_retry(self) -> None:
        first = self.database.add_finalized_segment(self.session.id, "一")
        second = self.database.add_finalized_segment(self.session.id, "二")
        self.database.mark_translation_failed(
            first.id,
            "offline",
            retryable=True,
            next_retry_at="2026-10-24T01:00:10+00:00",
            attempted_at="2026-10-24T01:00:00+00:00",
        )
        client = RecordingClient()
        restarted_worker = TranslationWorker(self.database, client)

        waiting = restarted_worker.run_once(now="2026-10-24T01:00:05+00:00")
        self.assertEqual(waiting.attempted, 0)
        self.assertEqual(waiting.next_retry_at_utc, "2026-10-24T01:00:10.000+00:00")
        self.assertEqual(client.calls, [])

        resumed = restarted_worker.run_once(now="2026-10-24T01:00:10+00:00")
        self.assertEqual(resumed.attempted, 2)
        self.assertEqual(client.calls[0][0], first.source_ja)
        self.assertEqual(client.calls[1][0], second.source_ja)

    def test_permanent_failure_stops_automatic_retry(self) -> None:
        segment = self.database.add_finalized_segment(self.session.id, "一")
        client = RecordingClient(
            [
                TranslationRequestError(
                    "authentication failed",
                    code="authentication_failed",
                    http_status=401,
                )
            ]
        )
        worker = TranslationWorker(self.database, client)

        summary = worker.run_once(now="2026-10-24T01:00:00+00:00")

        failed = self.database.get_segment(segment.id)
        self.assertEqual(summary.permanent_failures, 1)
        self.assertIn("authentication failed", summary.error_message)
        self.assertEqual(failed.translation_status, "translation_failed")
        self.assertIsNone(failed.next_retry_at_utc)

    def test_context_is_limited_to_six_completed_segments(self) -> None:
        for index in range(7):
            previous = self.database.add_finalized_segment(
                self.session.id,
                f"前{index}",
            )
            self.database.mark_translation_done(previous.id, f"前译{index}")
        current = self.database.add_finalized_segment(self.session.id, "現在")
        client = RecordingClient()
        worker = TranslationWorker(self.database, client)

        worker.run_once(now="2026-10-24T01:00:00+00:00")

        self.assertEqual(client.calls[-1][0], current.source_ja)
        self.assertEqual(client.calls[-1][1], 6)

    def test_pause_check_defers_translation_requests(self) -> None:
        self.database.add_finalized_segment(self.session.id, "停止中")
        client = RecordingClient()
        worker = TranslationWorker(
            self.database,
            client,
            pause_check=lambda: True,
        )

        summary = worker.run_once(now="2026-10-24T01:00:00+00:00")

        self.assertTrue(summary.paused)
        self.assertEqual(summary.attempted, 0)
        self.assertEqual(client.calls, [])

    def test_clear_marker_excludes_older_translation_context(self) -> None:
        old = self.database.add_finalized_segment(self.session.id, "古い")
        self.database.mark_translation_done(old.id, "旧内容")
        current = self.database.add_finalized_segment(self.session.id, "新しい")
        client = RecordingClient()
        worker = TranslationWorker(
            self.database,
            client,
            context_after_id=lambda: old.id,
        )

        worker.run_once(now="2026-10-24T01:00:00+00:00")

        self.assertEqual(client.calls[-1][0], current.source_ja)
        self.assertEqual(client.calls[-1][1], 0)


if __name__ == "__main__":
    unittest.main()
