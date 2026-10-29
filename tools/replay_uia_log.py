from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from realtime_translator.database import Database
from realtime_translator.segmenter import CaptionSegmenter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a UIA JSONL capture through the caption segmenter."
    )
    parser.add_argument("log", type=Path, help="JSONL file produced by rt-uia-probe.")
    parser.add_argument(
        "--automation-id",
        default="CaptionsTextBlock",
        help="Only replay this UIA AutomationId.",
    )
    parser.add_argument(
        "--stable-after-ms",
        type=int,
        default=1500,
        help="How long the last line must remain unchanged to finalize.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="Optional SQLite path. Without it, replay only prints a summary.",
    )
    parser.add_argument(
        "--session-name",
        default="日志回放",
        help="Session name when --db is used.",
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.stable_after_ms <= 0:
        print("error: --stable-after-ms must be positive.")
        return 2
    if not args.log.is_file():
        print(f"error: log file not found: {args.log}")
        return 2

    segmenter = CaptionSegmenter(
        args.stable_after_ms / 1000.0,
        emit_initial_stable=True,
    )
    completed: list[tuple[str, str]] = []
    processed_events = 0
    first_timestamp = ""
    last_timestamp = ""

    database = Database(args.db) if args.db is not None else None
    session = None
    try:
        with args.log.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                node = event.get("node")
                if not isinstance(node, dict):
                    continue
                if node.get("automation_id") != args.automation_id:
                    continue

                captured_at = str(event.get("captured_at", ""))
                if not captured_at:
                    continue
                last_timestamp = captured_at
                if not first_timestamp:
                    first_timestamp = captured_at
                    if database is not None:
                        session = database.get_or_create_open_session(
                            name=args.session_name,
                            started_at=first_timestamp,
                        )

                text = str(node.get("name") or node.get("text_pattern") or "")
                update = segmenter.process_text(text, observed_at=captured_at)
                completed.extend(
                    (segment.source_ja, segment.observed_at_utc)
                    for segment in update.completed
                )
                completed.extend(
                    (segment.source_ja, segment.observed_at_utc)
                    for segment in segmenter.tick(observed_at=captured_at)
                )
                processed_events += 1

        final_timestamp = last_timestamp or first_timestamp or None
        completed.extend(
            (segment.source_ja, segment.observed_at_utc)
            for segment in segmenter.flush(observed_at=final_timestamp)
        )

        if database is not None and session is not None:
            database.add_finalized_segments(session.id, completed)

        duplicate_count = sum(
            1
            for index in range(1, len(completed))
            if completed[index][0] == completed[index - 1][0]
        )
        print(f"events: {processed_events}")
        print(f"completed: {len(completed)}")
        print(f"adjacent_duplicates: {duplicate_count}")
        if completed:
            print(f"first: {completed[0][0]}")
            print(f"last: {completed[-1][0]}")
        if database is not None and session is not None:
            print(f"database: {args.db.resolve()}")
            print(f"session_id: {session.id}")
    finally:
        if database is not None:
            database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
