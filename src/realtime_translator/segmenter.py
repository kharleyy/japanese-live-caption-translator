from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class CaptionSegment:
    source_ja: str
    observed_at_utc: str


@dataclass(frozen=True, slots=True)
class SegmenterUpdate:
    temporary: CaptionSegment | None
    completed: tuple[CaptionSegment, ...]
    temporary_changed: bool


class CaptionSegmenter:
    """Turn the changing Live Captions buffer into temporary and stable lines.

    Windows exposes roughly the newest 12 recognized lines. All lines before the
    last one have moved out of the editable row and are treated as stable. The
    last line remains temporary until a later line follows it or it stays
    unchanged for ``stable_after_seconds``.
    """

    def __init__(
        self,
        stable_after_seconds: float = 1.5,
        *,
        emit_initial_stable: bool = False,
    ) -> None:
        if stable_after_seconds <= 0:
            raise ValueError("stable_after_seconds must be positive")
        self.stable_after_seconds = stable_after_seconds
        self.emit_initial_stable = emit_initial_stable
        self._initialized = False
        self._lines: tuple[str, ...] = ()
        self._recent_stable: tuple[str, ...] = ()
        self._last_change_at: datetime | None = None
        self._temporary_persisted = False
        self._timeout_waiting_to_move = ""

    @property
    def lines(self) -> tuple[str, ...]:
        return self._lines

    @staticmethod
    def normalize_lines(lines: Iterable[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for line in lines:
            text = " ".join(part.strip() for part in line.splitlines() if part.strip())
            if text:
                normalized.append(text)
        return tuple(normalized)

    @staticmethod
    def _find_overlap(
        previous_stable: tuple[str, ...],
        current_stable: tuple[str, ...],
    ) -> int:
        maximum = min(len(previous_stable), len(current_stable))
        for length in range(maximum, 0, -1):
            if previous_stable[-length:] == current_stable[:length]:
                return length
        return 0

    def process_text(
        self,
        text: str,
        observed_at: datetime | str | None = None,
    ) -> SegmenterUpdate:
        return self.process_lines(text.splitlines(), observed_at)

    def process_lines(
        self,
        lines: Iterable[str],
        observed_at: datetime | str | None = None,
    ) -> SegmenterUpdate:
        observed = _as_utc(observed_at)
        normalized = self.normalize_lines(lines)

        # Live Captions can briefly expose an empty Name while its UIA tree is
        # being updated. Ignoring that sample avoids finalizing a live sentence.
        if not normalized:
            return self._current_update(observed, temporary_changed=False)

        changed = normalized != self._lines
        previous_temporary = self._lines[-1] if self._lines else ""
        temporary = normalized[-1]
        temporary_changed = changed and temporary != previous_temporary
        if changed:
            self._last_change_at = observed
        if temporary_changed:
            self._temporary_persisted = False

        stable_lines = normalized[:-1]
        if not self._initialized:
            self._initialized = True
            self._recent_stable = stable_lines
            self._lines = normalized
            initial_completed = (
                tuple(
                    CaptionSegment(
                        source_ja=source,
                        observed_at_utc=_utc_iso(observed),
                    )
                    for source in stable_lines
                )
                if self.emit_initial_stable
                else ()
            )
            return SegmenterUpdate(
                temporary=CaptionSegment(
                    source_ja=temporary,
                    observed_at_utc=_utc_iso(observed),
                ),
                completed=initial_completed,
                temporary_changed=temporary_changed,
            )

        overlap = self._find_overlap(self._recent_stable, stable_lines)
        new_stable = stable_lines[overlap:]
        completed: list[CaptionSegment] = []
        for source in new_stable:
            if (
                self._timeout_waiting_to_move
                and source == self._timeout_waiting_to_move
            ):
                self._timeout_waiting_to_move = ""
                continue
            completed.append(
                CaptionSegment(source_ja=source, observed_at_utc=_utc_iso(observed))
            )

        if (
            self._timeout_waiting_to_move
            and self._timeout_waiting_to_move not in normalized
        ):
            self._timeout_waiting_to_move = ""

        self._recent_stable = stable_lines
        self._lines = normalized
        return SegmenterUpdate(
            temporary=CaptionSegment(
                source_ja=temporary,
                observed_at_utc=_utc_iso(observed),
            ),
            completed=tuple(completed),
            temporary_changed=temporary_changed,
        )

    def tick(
        self,
        observed_at: datetime | str | None = None,
        *,
        force: bool = False,
    ) -> tuple[CaptionSegment, ...]:
        if not self._lines or self._temporary_persisted:
            return ()
        if self._last_change_at is None:
            return ()

        observed = _as_utc(observed_at)
        quiet_seconds = (observed - self._last_change_at).total_seconds()
        if not force and quiet_seconds < self.stable_after_seconds:
            return ()

        segment = CaptionSegment(
            source_ja=self._lines[-1],
            observed_at_utc=_utc_iso(observed),
        )
        self._temporary_persisted = True
        self._timeout_waiting_to_move = self._lines[-1]
        return (segment,)

    def flush(
        self,
        observed_at: datetime | str | None = None,
    ) -> tuple[CaptionSegment, ...]:
        return self.tick(observed_at, force=True)

    def _current_update(
        self,
        observed: datetime,
        *,
        temporary_changed: bool,
    ) -> SegmenterUpdate:
        temporary = (
            CaptionSegment(
                source_ja=self._lines[-1],
                observed_at_utc=_utc_iso(observed),
            )
            if self._lines
            else None
        )
        return SegmenterUpdate(
            temporary=temporary,
            completed=(),
            temporary_changed=temporary_changed,
        )
