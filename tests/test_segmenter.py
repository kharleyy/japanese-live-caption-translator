from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from realtime_translator.segmenter import CaptionSegmenter


START = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)


class CaptionSegmenterTests(unittest.TestCase):
    def test_edits_last_line_without_duplicates(self) -> None:
        segmenter = CaptionSegmenter(stable_after_seconds=1.5)

        first = segmenter.process_lines(["前の文", "犬さ"], START)
        self.assertEqual(first.completed, ())
        self.assertEqual(first.temporary.source_ja, "犬さ")

        second = segmenter.process_lines(["前の文", "犬さんエ"], START + timedelta(
            milliseconds=300
        ))
        self.assertEqual(second.completed, ())
        self.assertTrue(second.temporary_changed)
        self.assertEqual(second.temporary.source_ja, "犬さんエ")

        third = segmenter.process_lines(
            ["前の文", "犬さんエスパート", "次の文"],
            START + timedelta(seconds=1),
        )
        self.assertEqual(
            [item.source_ja for item in third.completed],
            ["犬さんエスパート"],
        )

        fourth = segmenter.process_lines(
            ["前の文", "犬さんエスパート", "次の文"],
            START + timedelta(seconds=2),
        )
        self.assertEqual(fourth.completed, ())

    def test_scrolling_removes_old_prefix_without_repeating(self) -> None:
        segmenter = CaptionSegmenter()
        first = segmenter.process_lines(["A", "B", "C"], START)
        self.assertEqual(first.completed, ())

        second = segmenter.process_lines(
            ["B", "C", "D"],
            START + timedelta(seconds=1),
        )
        self.assertEqual([item.source_ja for item in second.completed], ["C"])

        third = segmenter.process_lines(
            ["C", "D", "E"],
            START + timedelta(seconds=2),
        )
        self.assertEqual([item.source_ja for item in third.completed], ["D"])

    def test_idle_timeout_finalizes_last_line_once(self) -> None:
        segmenter = CaptionSegmenter(stable_after_seconds=1.5)
        segmenter.process_lines(["A"], START)

        self.assertEqual(segmenter.tick(START + timedelta(seconds=1)), ())
        completed = segmenter.tick(START + timedelta(milliseconds=1500))
        self.assertEqual([item.source_ja for item in completed], ["A"])
        self.assertEqual(
            segmenter.tick(START + timedelta(seconds=3)),
            (),
        )

        next_line = segmenter.process_lines(
            ["A", "B"],
            START + timedelta(seconds=4),
        )
        self.assertEqual(next_line.completed, ())

    def test_empty_transient_sample_does_not_finalize(self) -> None:
        segmenter = CaptionSegmenter()
        segmenter.process_lines(["A", "B"], START)
        cleared = segmenter.process_lines([], START + timedelta(seconds=1))
        self.assertEqual(cleared.completed, ())
        self.assertEqual(cleared.temporary.source_ja, "B")

    def test_initial_large_buffer_is_baseline_without_backfill(self) -> None:
        segmenter = CaptionSegmenter()
        update = segmenter.process_lines(["1", "2", "3", "4"], START)
        self.assertEqual(update.completed, ())
        self.assertEqual(update.temporary.source_ja, "4")

    def test_replay_can_explicitly_emit_initial_buffer(self) -> None:
        segmenter = CaptionSegmenter(emit_initial_stable=True)
        update = segmenter.process_lines(["1", "2", "3", "4"], START)
        self.assertEqual(
            [item.source_ja for item in update.completed],
            ["1", "2", "3"],
        )


if __name__ == "__main__":
    unittest.main()
