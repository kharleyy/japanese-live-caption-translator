from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from realtime_translator.database import (
    Database,
    shanghai_business_date,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name) / "test.sqlite3"

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_business_date_uses_shanghai_midnight(self) -> None:
        self.assertEqual(
            shanghai_business_date("2026-10-23T15:59:59+00:00"),
            "2026-10-23",
        )
        self.assertEqual(
            shanghai_business_date("2026-10-23T16:00:00+00:00"),
            "2026-10-24",
        )

    def test_finalized_segment_is_pending_and_queryable(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at="2026-10-24T01:00:00+00:00",
            )
            segment = database.add_finalized_segment(
                session.id,
                "こんにちは",
                finalized_at="2026-10-24T01:00:02+00:00",
            )

            self.assertEqual(segment.translation_status, "translation_pending")
            self.assertEqual(
                [item.id for item in database.list_segments_for_date("2026-10-24")],
                [segment.id],
            )
            self.assertEqual(
                [item.id for item in database.list_pending_translations()],
                [segment.id],
            )

            database.mark_translation_done(
                segment.id,
                "你好",
                input_tokens=20,
                output_tokens=5,
            )
            done = database.get_segment(segment.id)
            self.assertEqual(done.translation_zh, "你好")
            self.assertEqual(done.translation_status, "translation_done")
            self.assertEqual(done.input_tokens, 20)
            self.assertEqual(done.output_tokens, 5)
            self.assertEqual(database.list_pending_translations(), [])

    def test_retryable_failure_returns_to_pending(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            segment = database.add_finalized_segment(session.id, "本文")
            database.mark_translation_failed(
                segment.id,
                "网络超时",
                retryable=True,
            )
            pending = database.get_segment(segment.id)
            self.assertEqual(pending.translation_status, "translation_pending")
            self.assertEqual(pending.retry_count, 1)
            self.assertEqual(pending.last_error, "网络超时")

    def test_nonretryable_failure_stops_automatic_retry(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            segment = database.add_finalized_segment(session.id, "本文")
            database.mark_translation_failed(
                segment.id,
                "API 密钥无效",
                retryable=False,
            )
            failed = database.get_segment(segment.id)
            self.assertEqual(failed.translation_status, "translation_failed")
            self.assertEqual(database.list_pending_translations(), [])

    def test_pending_queue_survives_reopen(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            segment = database.add_finalized_segment(session.id, "再起動後")

        with Database(self.db_path) as reopened:
            pending = reopened.list_pending_translations()
            self.assertEqual([item.id for item in pending], [segment.id])

    def test_delete_day_cascades_and_leaves_other_days(self) -> None:
        with Database(self.db_path) as database:
            first = database.create_session(
                "第一天",
                started_at="2026-10-23T12:00:00+00:00",
            )
            second = database.create_session(
                "第二天",
                started_at="2026-10-24T12:00:00+00:00",
            )
            database.add_finalized_segment(first.id, "削除する")
            kept = database.add_finalized_segment(second.id, "残す")

            deleted = database.delete_day("2026-10-23")
            self.assertEqual(deleted.deleted_sessions, 1)
            self.assertEqual(deleted.deleted_segments, 1)
            self.assertEqual(
                [item.id for item in database.list_segments_for_date("2026-10-24")],
                [kept.id],
            )

    def test_foreign_keys_are_enabled(self) -> None:
        with Database(self.db_path) as database:
            self.assertEqual(
                database.connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                database.add_finalized_segment(9999, "孤立した文")

    def test_open_session_is_reused_within_same_day(self) -> None:
        with Database(self.db_path) as database:
            first = database.get_or_create_open_session(
                started_at="2026-10-24T01:00:00+00:00"
            )
            second = database.get_or_create_open_session(
                started_at="2026-10-24T02:00:00+00:00"
            )
            self.assertEqual(first.id, second.id)

    def test_retry_time_controls_pending_queue(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            segment = database.add_finalized_segment(session.id, "再試行")
            database.mark_translation_failed(
                segment.id,
                "网络超时",
                retryable=True,
                next_retry_at="2026-10-24T01:00:10+00:00",
                attempted_at="2026-10-24T01:00:05+00:00",
            )

            self.assertEqual(
                database.list_pending_translations(
                    due_at="2026-10-24T01:00:09+00:00"
                ),
                [],
            )
            self.assertEqual(
                [item.id for item in database.list_pending_translations(
                    due_at="2026-10-24T01:00:10+00:00"
                )],
                [segment.id],
            )

    def test_translation_context_uses_latest_six_completed_segments(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            completed_ids: list[int] = []
            for index in range(8):
                segment = database.add_finalized_segment(
                    session.id,
                    f"原文{index}",
                )
                database.mark_translation_done(segment.id, f"译文{index}")
                completed_ids.append(segment.id)
            current = database.add_finalized_segment(session.id, "現在")

            context = database.get_recent_translation_context(current.id, limit=6)
            self.assertEqual(
                [item.source_ja for item in context],
                [f"原文{index}" for index in range(2, 8)],
            )
            self.assertEqual(
                [item.translation_zh for item in context],
                [f"译文{index}" for index in range(2, 8)],
            )
            self.assertTrue(completed_ids)

            filtered = database.get_recent_translation_context(
                current.id,
                limit=6,
                after_id=completed_ids[4],
            )
            self.assertEqual(
                [item.source_ja for item in filtered],
                ["原文5", "原文6", "原文7"],
            )

    def test_translation_attempt_usage_is_aggregated(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            first = database.add_finalized_segment(session.id, "成功")
            second = database.add_finalized_segment(session.id, "失败")
            database.mark_translation_done(
                first.id,
                "成功译文",
                input_tokens=100,
                output_tokens=20,
                estimated_cost=0.0012,
            )
            database.mark_translation_failed(
                second.id,
                "网络错误",
                retryable=True,
                error_code="network_error",
                next_retry_at="2026-10-24T02:00:00+00:00",
            )

            usage = database.translation_usage_summary()
            self.assertEqual(usage.requests, 2)
            self.assertEqual(usage.successes, 1)
            self.assertEqual(usage.failures, 1)
            self.assertEqual(usage.input_tokens, 100)
            self.assertEqual(usage.output_tokens, 20)
            self.assertAlmostEqual(usage.estimated_cost, 0.0012)
            today = database.translation_usage_summary("2026-10-24")
            self.assertEqual(today.requests, 2)
            self.assertEqual(
                database.translation_usage_summary("2026-10-23").requests,
                0,
            )

    def test_day_queries_support_overlay_clear_marker(self) -> None:
        with Database(self.db_path) as database:
            session = database.create_session(
                "测试会话",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            first = database.add_finalized_segment(session.id, "一")
            second = database.add_finalized_segment(session.id, "二")
            self.assertEqual(
                [item.id for item in database.list_segments_for_date(
                    "2026-10-24",
                    after_id=first.id,
                )],
                [second.id],
            )
            self.assertEqual(
                database.get_latest_segment_id_for_date("2026-10-24"),
                second.id,
            )

    def test_old_stage_two_database_is_migrated(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_date TEXT NOT NULL,
                    name TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    ended_at_utc TEXT
                );
                CREATE TABLE segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id)
                        ON DELETE CASCADE,
                    source_ja TEXT NOT NULL,
                    translation_zh TEXT,
                    finalized_at_utc TEXT NOT NULL,
                    translation_status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO sessions(
                    business_date,
                    name,
                    started_at_utc
                ) VALUES (
                    '2026-10-24',
                    '旧会话',
                    '2026-10-24T01:00:00.000+00:00'
                );
                INSERT INTO segments(
                    session_id,
                    source_ja,
                    finalized_at_utc,
                    translation_status
                ) VALUES (
                    1,
                    '移行前',
                    '2026-10-24T01:00:01.000+00:00',
                    'translation_pending'
                );
                """
            )

        with Database(self.db_path) as database:
            pending = database.list_pending_translations(
                due_at="2026-10-24T02:00:00+00:00"
            )
            self.assertEqual(len(pending), 1)
            self.assertIsNone(pending[0].next_retry_at_utc)
            self.assertEqual(database.translation_usage_summary().requests, 0)


if __name__ == "__main__":
    unittest.main()
