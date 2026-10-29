from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LiveCaptionSnapshot:
    session_id: int
    session_name: str
    business_date: str
    source_ja: str
    observed_at_utc: str


class LiveCaptionStore:
    """Ephemeral current caption row; this file is never a history store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, snapshot: LiveCaptionSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                json.dump(
                    asdict(snapshot),
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                temp_path = Path(stream.name)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def load(
        self,
        *,
        max_age_seconds: float = 10.0,
        now: datetime | None = None,
    ) -> LiveCaptionSnapshot | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            snapshot = LiveCaptionSnapshot(
                session_id=int(data["session_id"]),
                session_name=str(data["session_name"]),
                business_date=str(data["business_date"]),
                source_ja=str(data["source_ja"]).strip(),
                observed_at_utc=str(data["observed_at_utc"]),
            )
            observed = datetime.fromisoformat(
                snapshot.observed_at_utc.replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > max_age_seconds or not snapshot.source_ja:
            return None
        return snapshot

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
