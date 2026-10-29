from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeState:
    paused: bool = False
    clear_after_segment_id: int = 0
    window_x: int | None = None
    window_y: int | None = None
    opacity: float = 0.8
    theme: str = "dark"


class RuntimeStateStore:
    """Small cross-process control file shared by the overlay and worker."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> RuntimeState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return RuntimeState()
        if not isinstance(data, dict):
            return RuntimeState()

        try:
            clear_after_id = max(0, int(data.get("clear_after_segment_id", 0)))
            opacity = min(1.0, max(0.35, float(data.get("opacity", 0.8))))
            window_x = _optional_int(data.get("window_x"))
            window_y = _optional_int(data.get("window_y"))
        except (TypeError, ValueError):
            return RuntimeState()

        theme = str(data.get("theme", "dark"))
        if theme not in {"dark", "light"}:
            theme = "dark"
        return RuntimeState(
            paused=bool(data.get("paused", False)),
            clear_after_segment_id=clear_after_id,
            window_x=window_x,
            window_y=window_y,
            opacity=opacity,
            theme=theme,
        )

    def update(self, **changes: Any) -> RuntimeState:
        with self._lock:
            current = asdict(self.load())
            current.update(changes)
            state = self._validated(current)
            self._write(asdict(state))
            return state

    @staticmethod
    def _validated(data: dict[str, Any]) -> RuntimeState:
        clear_after_id = max(0, int(data.get("clear_after_segment_id", 0)))
        opacity = min(1.0, max(0.35, float(data.get("opacity", 0.8))))
        theme = str(data.get("theme", "dark"))
        if theme not in {"dark", "light"}:
            theme = "dark"
        return RuntimeState(
            paused=bool(data.get("paused", False)),
            clear_after_segment_id=clear_after_id,
            window_x=_optional_int(data.get("window_x")),
            window_y=_optional_int(data.get("window_y")),
            opacity=opacity,
            theme=theme,
        )

    def _write(self, data: dict[str, Any]) -> None:
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
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                temp_path = Path(stream.name)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
