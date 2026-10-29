from __future__ import annotations

import ctypes
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psutil
import uiautomation as auto

PROCESS_NAME = "LiveCaptions.exe"
EXECUTABLE_PATH = Path(r"C:\Windows\System32\LiveCaptions.exe")
CAPTION_AUTOMATION_ID = "CaptionsTextBlock"
LANGUAGE_AUTOMATION_ID = "SpeechModelDropDown"
SELECTION_READY_AUTOMATION_ID = "SelectionReadyToCaptionTextBlock"
READY_AUTOMATION_ID = "ReadyToCaptionTextBlock"


class LiveCaptionsError(RuntimeError):
    """Base error for Windows Live Captions capture."""


class LiveCaptionsNotRunning(LiveCaptionsError):
    """Raised when the Live Captions process is not running."""


class LiveCaptionsWindowNotFound(LiveCaptionsError):
    """Raised when no UIA window belongs to Live Captions."""


class InteractiveDesktopRequired(LiveCaptionsError):
    """Raised when the probe is running on an isolated agent desktop."""


class LiveCaptionsConfigurationError(LiveCaptionsError):
    """Raised when the Japanese language cannot be selected automatically."""


@dataclass(frozen=True)
class CaptionNode:
    runtime_id: str
    process_id: int
    control_type: str
    automation_id: str
    name: str
    class_name: str
    help_text: str
    text_pattern: str
    is_offscreen: bool | None
    bounds: tuple[float, float, float, float] | None

    @property
    def display_text(self) -> str:
        return self.name.strip() or self.text_pattern.strip()

    @property
    def content_signature(self) -> tuple[str, bool | None]:
        # CaptionsTextBlock exposes the same value through Name and TextPattern.
        # One property can advance while the other is being read, so changes are
        # compared using Name as the canonical text source.
        return (
            self.name.strip() or self.text_pattern.strip(),
            self.is_offscreen,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def current_desktop_name() -> str:
    if os.name != "nt":
        return ""

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    if not desktop:
        return ""

    needed = ctypes.c_ulong()
    user32.GetUserObjectInformationW(
        desktop,
        2,
        None,
        0,
        ctypes.byref(needed),
    )
    buffer = ctypes.create_unicode_buffer(max(needed.value // 2, 1))
    if not user32.GetUserObjectInformationW(
        desktop,
        2,
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(needed),
    ):
        return ""
    return buffer.value


def ensure_interactive_desktop() -> str:
    desktop = current_desktop_name()
    if desktop.casefold().startswith("codexsandboxdesktop"):
        raise InteractiveDesktopRequired(
            "The UIA probe is running on an isolated agent desktop "
            f"({desktop}). Run it from the normal logged-in Windows desktop."
        )
    return desktop


def find_process_ids() -> list[int]:
    process_ids: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if (process.info["name"] or "").casefold() == PROCESS_NAME.casefold():
                process_ids.append(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return process_ids


def launch_live_captions() -> None:
    if not EXECUTABLE_PATH.is_file():
        raise LiveCaptionsError(f"Live Captions executable not found: {EXECUTABLE_PATH}")
    os.startfile(EXECUTABLE_PATH)


def is_japanese_language_name(value: str) -> bool:
    compact = "".join(value.split()).casefold()
    return (
        "日语" in compact
        or "日本語" in compact
        or ("japanese" in compact and ("japan" in compact or "jp" in compact))
    )


def _automation_id(control: auto.Control) -> str:
    return str(_safe_value(control, "AutomationId", "") or "")


def _control_name(control: auto.Control) -> str:
    return str(_safe_value(control, "Name", "") or "")


def _control_type(control: auto.Control) -> str:
    return str(_safe_value(control, "ControlTypeName", "") or "")


def _find_controls(
    window: auto.Control,
    *,
    automation_id: str = "",
    control_type: str = "",
    name_predicate: Any = None,
    max_depth: int = 18,
) -> list[auto.Control]:
    matches: list[auto.Control] = []
    for control, _depth, _path in iter_descendants(window, max_depth):
        if automation_id and _automation_id(control) != automation_id:
            continue
        if control_type and _control_type(control) != control_type:
            continue
        if name_predicate is not None and not name_predicate(_control_name(control)):
            continue
        matches.append(control)
    return matches


def live_captions_ui_state(window: auto.Control) -> str:
    controls = list(iter_descendants(window, 18))
    automation_ids = {
        _automation_id(control)
        for control, _depth, _path in controls
    }
    if CAPTION_AUTOMATION_ID in automation_ids:
        return "captions"
    if READY_AUTOMATION_ID in automation_ids:
        return "ready"
    if LANGUAGE_AUTOMATION_ID in automation_ids:
        return "selection"
    return "unknown"


def _invoke_control(control: auto.Control) -> bool:
    getter = getattr(control, "GetInvokePattern", None)
    if callable(getter):
        try:
            pattern = getter()
            if pattern is not None and pattern.Invoke():
                return True
        except Exception:
            pass
    click = getattr(control, "Click", None)
    if callable(click):
        try:
            click(simulateMove=False)
            return True
        except Exception:
            pass
    return False


def prepare_live_captions(
    window: auto.Control,
    *,
    timeout_seconds: float = 12.0,
) -> str:
    """Select Japanese and enter caption mode through the UIA controls."""

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        state = live_captions_ui_state(window)
        if state in {"captions", "ready"}:
            return state

        combos = _find_controls(
            window,
            automation_id=LANGUAGE_AUTOMATION_ID,
        )
        if not combos:
            time.sleep(0.25)
            continue
        combo = combos[0]

        try:
            select = getattr(combo, "Select", None)
            selected = False
            if callable(select):
                selected = bool(
                    select(
                        condition=is_japanese_language_name,
                        simulateMove=False,
                    )
                )
            if not selected:
                selected = _select_japanese_list_item(window)
            if not selected:
                raise LiveCaptionsConfigurationError(
                    "Japanese option was not found in the Live Captions language menu"
                )

            continue_buttons = _find_controls(
                window,
                control_type="ButtonControl",
                name_predicate=lambda name: "继续" in name
                or "continue" in name.casefold(),
            )
            if continue_buttons and not _invoke_control(continue_buttons[0]):
                raise LiveCaptionsConfigurationError(
                    "The Live Captions Continue button could not be activated"
                )
        except Exception as error:
            last_error = error

        time.sleep(0.4)

    if last_error is not None:
        raise LiveCaptionsConfigurationError(
            f"Failed to configure Japanese Live Captions: {last_error}"
        ) from last_error
    raise LiveCaptionsConfigurationError(
        "Timed out waiting for the Japanese Live Captions controls"
    )


def _select_japanese_list_item(window: auto.Control) -> bool:
    combos = _find_controls(
        window,
        automation_id=LANGUAGE_AUTOMATION_ID,
    )
    if not combos:
        return False
    getter = getattr(combos[0], "GetExpandCollapsePattern", None)
    if callable(getter):
        try:
            pattern = getter()
            if pattern is not None:
                pattern.Expand()
                time.sleep(0.35)
        except Exception:
            pass

    items = _find_controls(
        window,
        control_type="ListItemControl",
        name_predicate=is_japanese_language_name,
    )
    if not items:
        return False
    selection_getter = getattr(items[0], "GetSelectionItemPattern", None)
    if callable(selection_getter):
        try:
            pattern = selection_getter()
            if pattern is not None and pattern.Select():
                return True
        except Exception:
            pass
    return _invoke_control(items[0])


def _safe_value(control: auto.Control, attribute: str, default: Any = None) -> Any:
    try:
        return getattr(control, attribute)
    except Exception:
        return default


def _runtime_id(control: auto.Control) -> str:
    try:
        runtime_id = control.GetRuntimeId()
    except Exception:
        return ""
    if runtime_id is None:
        return ""
    if isinstance(runtime_id, (tuple, list)):
        return ".".join(str(part) for part in runtime_id)
    return str(runtime_id)


def _bounds(control: auto.Control) -> tuple[float, float, float, float] | None:
    value = _safe_value(control, "BoundingRectangle")
    if value is None:
        return None
    try:
        return (
            float(value.left),
            float(value.top),
            float(value.right),
            float(value.bottom),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _text_pattern(control: auto.Control) -> str:
    getter = getattr(control, "GetTextPattern", None)
    if not callable(getter):
        return ""
    try:
        pattern = getter()
        if pattern is None:
            return ""
        return str(pattern.DocumentRange.GetText(-1) or "")
    except Exception:
        return ""


def _visible_native_window(control: auto.Control) -> bool:
    handle = _safe_value(control, "NativeWindowHandle", 0)
    if not handle:
        return False
    return bool(ctypes.windll.user32.IsWindowVisible(int(handle)))


def find_window(
    timeout_seconds: float = 5.0,
) -> auto.Control:
    ensure_interactive_desktop()
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        process_ids = set(find_process_ids())
        if process_ids:
            try:
                candidates = [
                    window
                    for window in auto.GetRootControl().GetChildren()
                    if _safe_value(window, "ProcessId", 0) in process_ids
                    and _visible_native_window(window)
                ]
            except Exception as error:
                raise LiveCaptionsWindowNotFound(
                    "Live Captions is running, but its UIA window could not be read."
                ) from error

            if candidates:
                return max(
                    candidates,
                    key=lambda window: _bounds(window) or (0.0, 0.0, 0.0, 0.0),
                )
        time.sleep(0.25)

    if not find_process_ids():
        raise LiveCaptionsNotRunning(
            "LiveCaptions.exe is not running. Start it, select Japanese, "
            "and click Continue before running the probe."
        )
    raise LiveCaptionsWindowNotFound(
        "LiveCaptions.exe is running, but no visible UIA window was found."
    )


def iter_descendants(
    root: auto.Control,
    max_depth: int,
) -> Iterator[tuple[auto.Control, int, list[int]]]:
    stack: list[tuple[auto.Control, int, list[int]]] = [(root, 0, [])]
    while stack:
        control, depth, path = stack.pop()
        yield control, depth, path
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        for index in range(len(children) - 1, -1, -1):
            stack.append((children[index], depth + 1, path + [index]))


def capture_tree(
    window: auto.Control,
    max_depth: int = 18,
) -> dict[str, CaptionNode]:
    nodes: dict[str, CaptionNode] = {}
    for control, _depth, path in iter_descendants(window, max_depth):
        try:
            control_type = str(_safe_value(control, "ControlTypeName", ""))
            automation_id = str(_safe_value(control, "AutomationId", "") or "")
            name = str(_safe_value(control, "Name", "") or "")
            class_name = str(_safe_value(control, "ClassName", "") or "")
            help_text = str(_safe_value(control, "HelpText", "") or "")
            process_id = int(_safe_value(control, "ProcessId", 0) or 0)
            is_offscreen = _safe_value(control, "IsOffscreen")
            runtime_id = _runtime_id(control)
            text_pattern = _text_pattern(control)

            if not any((automation_id, name, help_text, text_pattern)):
                continue

            key = (
                f"runtime:{runtime_id}"
                if runtime_id
                else "path:"
                + "/".join(str(index) for index in path)
                + f":{control_type}:{automation_id}"
            )
            nodes[key] = CaptionNode(
                runtime_id=runtime_id,
                process_id=process_id,
                control_type=control_type,
                automation_id=automation_id,
                name=name,
                class_name=class_name,
                help_text=help_text,
                text_pattern=text_pattern,
                is_offscreen=(
                    bool(is_offscreen) if is_offscreen is not None else None
                ),
                bounds=_bounds(control),
            )
        except Exception:
            continue
    return nodes
