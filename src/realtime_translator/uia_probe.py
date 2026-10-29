from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import uiautomation as auto

from realtime_translator.config import ConfigurationError, read_env_file
from realtime_translator.database import Database
from realtime_translator.live_state import LiveCaptionSnapshot, LiveCaptionStore
from realtime_translator.live_captions import (
    CAPTION_AUTOMATION_ID,
    CaptionNode,
    LiveCaptionsError,
    capture_tree,
    current_desktop_name,
    find_process_ids,
    find_window,
    launch_live_captions,
    prepare_live_captions,
    utc_now,
)
from realtime_translator.segmenter import CaptionSegment, CaptionSegmenter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Windows Live Captions UIA text changes."
    )
    try:
        env_values = read_env_file(".env")
    except ConfigurationError as error:
        parser.error(str(error))
    try:
        default_stable_after_ms = int(
            env_values.get("FINAL_DEBOUNCE_MS", "1500")
        )
    except ValueError:
        parser.error("FINAL_DEBOUNCE_MS must be an integer")
    if default_stable_after_ms <= 0:
        parser.error("FINAL_DEBOUNCE_MS must be positive")
    parser.add_argument("--duration", type=float, default=60.0, help="Seconds to observe.")
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=300,
        help="Polling interval in milliseconds.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=18,
        help="Maximum UIA tree depth.",
    )
    parser.add_argument(
        "--automation-id",
        default=CAPTION_AUTOMATION_ID,
        help="Only print changes for this UIA AutomationId.",
    )
    parser.add_argument(
        "--capture-all",
        action="store_true",
        help="Capture every named UIA node instead of only the caption text node.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSONL output path. Defaults to logs/uia-capture-<time>.jsonl.",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch/configure Live Captions with Japanese, then start capturing.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist finalized caption lines into SQLite.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "realtime_translator.sqlite3",
        help="SQLite path used with --persist.",
    )
    parser.add_argument(
        "--session-name",
        help="Session name used with --persist. Resumes today's open session by default.",
    )
    parser.add_argument(
        "--stable-after-ms",
        type=int,
        default=default_stable_after_ms,
        help="How long the last caption line must remain unchanged to finalize.",
    )
    parser.add_argument(
        "--live-state",
        type=Path,
        default=Path("data") / "live_caption.json",
        help="Ephemeral current-caption file shared with the overlay.",
    )
    parser.add_argument(
        "--quiet-events",
        action="store_true",
        help="Only print target/finalized status instead of every UIA change.",
    )
    return parser.parse_args()


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("logs") / f"uia-capture-{timestamp}.jsonl"


def node_change_summary(node: CaptionNode) -> dict[str, Any]:
    return {
        "control_type": node.control_type,
        "automation_id": node.automation_id,
        "name": node.name,
        "text_pattern": node.text_pattern,
        "is_offscreen": node.is_offscreen,
        "bounds": node.bounds,
    }


def write_event(output: Path, event: dict[str, Any]) -> None:
    with output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def print_node(event_type: str, node: CaptionNode) -> None:
    text = node.display_text.replace("\r", " ").replace("\n", " ")
    if len(text) > 240:
        text = text[:237] + "..."
    identifier = node.automation_id or "-"
    print(
        f"{event_type:7} type={node.control_type or '-':22} "
        f"id={identifier:28} text={text}",
        flush=True,
    )


def select_nodes(
    nodes: dict[str, CaptionNode],
    automation_id: str,
    capture_all: bool,
) -> dict[str, CaptionNode]:
    if capture_all:
        return nodes
    return {
        key: node
        for key, node in nodes.items()
        if node.automation_id == automation_id
    }


def run_probe(args: argparse.Namespace) -> int:
    if args.duration <= 0:
        raise ValueError("--duration must be positive.")
    if args.interval_ms <= 0:
        raise ValueError("--interval-ms must be positive.")
    if args.max_depth < 0:
        raise ValueError("--max-depth cannot be negative.")
    if args.stable_after_ms <= 0:
        raise ValueError("--stable-after-ms must be positive.")

    auto.SetGlobalSearchTimeout(1.0)
    print(f"desktop: {current_desktop_name() or '-'}", flush=True)

    if args.launch:
        if not find_process_ids():
            launch_live_captions()
            time.sleep(2.0)
        launch_window = find_window(timeout_seconds=10.0)
        state = prepare_live_captions(launch_window)
        print(f"live captions state: {state}", flush=True)

    window = find_window(timeout_seconds=8.0)
    print(
        f"window: pid={window.ProcessId} title={window.Name!r}",
        flush=True,
    )

    output = args.output or default_output_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("", encoding="utf-8")
    database = Database(args.db) if args.persist else None
    session = (
        database.get_or_create_open_session(name=args.session_name)
        if database is not None
        else None
    )
    live_state = LiveCaptionStore(args.live_state) if session is not None else None
    if live_state is not None:
        live_state.clear()
    segmenter = CaptionSegmenter(args.stable_after_ms / 1000.0)

    write_event(
        output,
        {
            "captured_at": utc_now(),
            "event": "start",
            "desktop": current_desktop_name(),
            "process_id": int(window.ProcessId),
            "window_title": str(window.Name),
            "duration_seconds": args.duration,
            "interval_ms": args.interval_ms,
            "max_depth": args.max_depth,
            "persist": database is not None,
            "database": str(args.db.resolve()) if database is not None else None,
            "session_id": session.id if session is not None else None,
        },
    )

    def persist_completed(segments: tuple[CaptionSegment, ...]) -> None:
        if database is None or session is None or not segments:
            return
        records = database.add_finalized_segments(
            session.id,
            [(segment.source_ja, segment.observed_at_utc) for segment in segments],
        )
        for record in records:
            text = record.source_ja.replace("\r", " ").replace("\n", " ")
            if len(text) > 240:
                text = text[:237] + "..."
            print(
                f"finalized id={record.id} status={record.translation_status} "
                f"session={session.id} text={text}",
                flush=True,
            )
            write_event(
                output,
                {
                    "captured_at": utc_now(),
                    "event": "finalized",
                    "segment": {
                        "id": record.id,
                        "session_id": record.session_id,
                        "source_ja": record.source_ja,
                        "finalized_at_utc": record.finalized_at_utc,
                        "translation_status": record.translation_status,
                    },
                },
            )

    previous: dict[str, CaptionNode] = {}
    counts: Counter[str] = Counter()
    deadline = time.monotonic() + args.duration
    sequence = 0
    saw_target = False

    try:
        try:
            while time.monotonic() < deadline:
                captured_at = utc_now()
                current = select_nodes(
                    capture_tree(window, max_depth=args.max_depth),
                    automation_id=args.automation_id,
                    capture_all=args.capture_all,
                )
                if current and not saw_target:
                    saw_target = True
                    print(
                        f"target: automation_id={args.automation_id}",
                        flush=True,
                    )

                if not args.capture_all:
                    target_node = next(iter(current.values()), None)
                    if target_node is not None:
                        update = segmenter.process_text(
                            target_node.display_text,
                            observed_at=captured_at,
                        )
                        persist_completed(update.completed)
                        if (
                            live_state is not None
                            and session is not None
                            and update.temporary_changed
                            and update.temporary is not None
                        ):
                            live_state.save(
                                LiveCaptionSnapshot(
                                    session_id=session.id,
                                    session_name=session.name,
                                    business_date=session.business_date,
                                    source_ja=update.temporary.source_ja,
                                    observed_at_utc=update.temporary.observed_at_utc,
                                )
                            )
                    finalized_on_timeout = segmenter.tick(
                        observed_at=captured_at
                    )
                    persist_completed(finalized_on_timeout)
                    if live_state is not None and finalized_on_timeout:
                        live_state.clear()

                for key, node in current.items():
                    if key not in previous:
                        event_type = "initial" if sequence == 0 else "added"
                        counts[event_type] += 1
                        write_event(
                            output,
                            {
                                "captured_at": captured_at,
                                "sequence": sequence,
                                "event": event_type,
                                "node": node.to_dict(),
                            },
                        )
                        if node.display_text and not args.quiet_events:
                            print_node(event_type, node)
                    elif previous[key].content_signature != node.content_signature:
                        counts["changed"] += 1
                        write_event(
                            output,
                            {
                                "captured_at": captured_at,
                                "sequence": sequence,
                                "event": "changed",
                                "previous": node_change_summary(previous[key]),
                                "node": node.to_dict(),
                            },
                        )
                        if (
                            not args.quiet_events
                            and (node.display_text or previous[key].display_text)
                        ):
                            print_node("changed", node)

                for key, node in previous.items():
                    if key not in current:
                        counts["removed"] += 1
                        write_event(
                            output,
                            {
                                "captured_at": captured_at,
                                "sequence": sequence,
                                "event": "removed",
                                "node": node.to_dict(),
                            },
                        )
                        if node.display_text and not args.quiet_events:
                            print_node("removed", node)

                previous = current
                sequence += 1
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(args.interval_ms / 1000.0, remaining))
        except KeyboardInterrupt:
            pass

        if not args.capture_all:
            finalized_at_stop = segmenter.flush(observed_at=utc_now())
            persist_completed(finalized_at_stop)
            if live_state is not None and finalized_at_stop:
                live_state.clear()
    finally:
        if database is not None:
            database.close()

    write_event(
        output,
        {
            "captured_at": utc_now(),
            "event": "stop",
            "sequences": sequence,
            "counts": dict(counts),
            "final_node_count": len(previous),
        },
    )
    if not saw_target and not args.capture_all:
        print(
            f"error: UIA target {args.automation_id!r} was not observed.",
            file=sys.stderr,
        )
        return 3
    print(f"output: {output.resolve()}", flush=True)
    print(f"summary: {dict(counts)} final_nodes={len(previous)}", flush=True)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        return run_probe(args)
    except (LiveCaptionsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
