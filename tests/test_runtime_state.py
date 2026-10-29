from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from realtime_translator.live_state import LiveCaptionSnapshot, LiveCaptionStore
from realtime_translator.runtime_state import RuntimeState, RuntimeStateStore


class RuntimeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_runtime_state_round_trip_and_validation(self) -> None:
        store = RuntimeStateStore(self.root / "runtime.json")
        self.assertEqual(store.load(), RuntimeState())

        updated = store.update(
            paused=True,
            clear_after_segment_id=42,
            window_x=100,
            window_y=-20,
            opacity=0.7,
            theme="light",
        )
        self.assertTrue(updated.paused)
        self.assertEqual(store.load(), updated)

        (self.root / "runtime.json").write_text(
            '{"opacity": 0.1, "theme": "unknown"}',
            encoding="utf-8",
        )
        fallback = store.load()
        self.assertEqual(fallback.opacity, 0.35)
        self.assertEqual(fallback.theme, "dark")

    def test_live_caption_rejects_stale_snapshot_and_can_clear(self) -> None:
        store = LiveCaptionStore(self.root / "live.json")
        now = datetime(2026, 10, 24, 1, 0, 0, tzinfo=timezone.utc)
        snapshot = LiveCaptionSnapshot(
            session_id=1,
            session_name="直播",
            business_date="2026-10-24",
            source_ja="こんにちは",
            observed_at_utc=now.isoformat(timespec="milliseconds"),
        )
        store.save(snapshot)

        self.assertEqual(store.load(now=now + timedelta(seconds=2)), snapshot)
        self.assertIsNone(store.load(now=now + timedelta(seconds=11)))
        store.clear()
        self.assertIsNone(store.load(now=now))


if __name__ == "__main__":
    unittest.main()
