from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from realtime_translator.config import (
    ConfigurationError,
    TranslationConfig,
    load_translation_config,
)
from realtime_translator.database import Database, TranslationUsageSummary
from realtime_translator.runtime_state import RuntimeStateStore
from realtime_translator.translator import (
    OpenAICompatibleTranslator,
    TranslationContext,
    TranslationError,
    TranslationResult,
)


class TranslationClient(Protocol):
    def translate(
        self,
        source_ja: str,
        context: Sequence[TranslationContext] = (),
    ) -> TranslationResult: ...


@dataclass(frozen=True, slots=True)
class TranslationRunSummary:
    attempted: int
    completed: int
    transient_failures: int
    permanent_failures: int
    next_retry_at_utc: str | None
    usage: TranslationUsageSummary
    error_message: str | None = None
    paused: bool = False


class TranslationWorker:
    def __init__(
        self,
        database: Database,
        client: TranslationClient,
        *,
        retry_delays_seconds: tuple[float, ...] = (5.0, 15.0, 60.0, 300.0),
        context_size: int = 6,
        pause_check: Callable[[], bool] | None = None,
        context_after_id: Callable[[], int] | None = None,
    ) -> None:
        if not retry_delays_seconds or any(delay <= 0 for delay in retry_delays_seconds):
            raise ValueError("retry delays must be positive")
        if context_size <= 0:
            raise ValueError("context_size must be positive")
        self.database = database
        self.client = client
        self.retry_delays_seconds = retry_delays_seconds
        self.context_size = context_size
        self.pause_check = pause_check or (lambda: False)
        self.context_after_id = context_after_id or (lambda: 0)
        self._global_retry_at: datetime | None = None

    def run_once(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 50,
    ) -> TranslationRunSummary:
        observed = _as_utc(now)
        if self.pause_check():
            return self._summary(0, 0, 0, 0, None, paused=True)
        if self._global_retry_at is not None and observed < self._global_retry_at:
            return self._summary(0, 0, 0, 0, self._global_retry_at)

        attempted = 0
        completed = 0
        transient_failures = 0
        permanent_failures = 0
        next_retry_at: datetime | None = None
        error_message: str | None = None
        context_after_id = max(0, self.context_after_id())

        while attempted < limit:
            segment = self.database.get_next_pending_translation()
            if segment is None:
                break
            if segment.next_retry_at_utc is not None:
                ready_at = _as_utc(segment.next_retry_at_utc)
                if observed < ready_at:
                    next_retry_at = ready_at
                    break

            context = [
                TranslationContext(
                    source_ja=item.source_ja,
                    translation_zh=item.translation_zh,
                )
                for item in self.database.get_recent_translation_context(
                    segment.id,
                    limit=self.context_size,
                    after_id=context_after_id,
                )
            ]
            attempted += 1
            try:
                result = self.client.translate(segment.source_ja, context)
            except TranslationError as error:
                if error.retryable:
                    delay = self.retry_delays_seconds[
                        min(segment.retry_count, len(self.retry_delays_seconds) - 1)
                    ]
                    retry_at = observed + timedelta(seconds=delay)
                    self.database.mark_translation_failed(
                        segment.id,
                        str(error),
                        retryable=True,
                        error_code=error.code,
                        next_retry_at=retry_at,
                        attempted_at=observed,
                    )
                    transient_failures += 1
                    next_retry_at = retry_at
                    error_message = f"segment {segment.id}: {error}"
                    self._global_retry_at = retry_at
                    break

                self.database.mark_translation_failed(
                    segment.id,
                    str(error),
                    retryable=False,
                    error_code=error.code,
                    attempted_at=observed,
                )
                permanent_failures += 1
                error_message = f"segment {segment.id}: {error}"
                break

            self.database.mark_translation_done(
                segment.id,
                result.translation_zh,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                estimated_cost=result.estimated_cost,
                attempted_at=observed,
            )
            completed += 1

        return self._summary(
            attempted,
            completed,
            transient_failures,
            permanent_failures,
            next_retry_at,
            error_message,
        )

    def _summary(
        self,
        attempted: int,
        completed: int,
        transient_failures: int,
        permanent_failures: int,
        next_retry_at: datetime | None,
        error_message: str | None = None,
        paused: bool = False,
    ) -> TranslationRunSummary:
        return TranslationRunSummary(
            attempted=attempted,
            completed=completed,
            transient_failures=transient_failures,
            permanent_failures=permanent_failures,
            next_retry_at_utc=(
                next_retry_at.isoformat(timespec="milliseconds")
                if next_retry_at is not None
                else None
            ),
            usage=self.database.translation_usage_summary(),
            error_message=error_message,
            paused=paused,
        )


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate stable Japanese captions with an OpenAI-compatible API."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "realtime_translator.sqlite3",
        help="SQLite path containing finalized caption segments.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one pass and exit instead of polling continuously.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=1000,
        help="Database polling interval in continuous mode.",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=50,
        help="Maximum segments attempted in one pass.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("data") / "runtime_state.json",
        help="Runtime control file shared with the overlay.",
    )
    return parser.parse_args()


def print_summary(
    summary: TranslationRunSummary,
    *,
    output: Callable[[str], None] = print,
) -> None:
    usage = summary.usage
    if summary.paused:
        output("translation: paused by overlay")
    output(
        "translation: "
        f"attempted={summary.attempted} completed={summary.completed} "
        f"transient={summary.transient_failures} "
        f"permanent={summary.permanent_failures}"
    )
    output(
        "usage: "
        f"requests={usage.requests} successes={usage.successes} "
        f"failures={usage.failures} input_tokens={usage.input_tokens} "
        f"output_tokens={usage.output_tokens} "
        f"estimated_cost={usage.estimated_cost:.6f}"
    )
    if summary.next_retry_at_utc:
        output(f"next_retry_at_utc: {summary.next_retry_at_utc}")
    if summary.error_message:
        output(f"last_error: {summary.error_message}")
    if summary.permanent_failures:
        output(
            "action: check .env, API_MODEL, API permissions, and the last error",
        )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.poll_interval_ms <= 0:
        print("error: --poll-interval-ms must be positive", file=sys.stderr)
        return 2
    if args.max_segments <= 0:
        print("error: --max-segments must be positive", file=sys.stderr)
        return 2

    try:
        config = load_translation_config()
        config.require_api_key()
        client: TranslationClient = OpenAICompatibleTranslator(config)
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    with Database(args.db) as database:
        runtime_state = RuntimeStateStore(args.state)
        worker = TranslationWorker(
            database,
            client,
            retry_delays_seconds=config.retry_delays_seconds,
            pause_check=lambda: runtime_state.load().paused,
            context_after_id=lambda: runtime_state.load().clear_after_segment_id,
        )
        try:
            while True:
                summary = worker.run_once(limit=args.max_segments)
                if args.once:
                    print_summary(summary)
                    return 0
                if summary.attempted or summary.transient_failures:
                    print_summary(summary)
                time.sleep(args.poll_interval_ms / 1000.0)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
