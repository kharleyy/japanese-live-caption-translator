from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from realtime_translator.config import (
    ConfigurationError,
    TranslationConfig,
    load_translation_config,
    read_env_file,
)
from realtime_translator.database import Database
from realtime_translator.history_server import HistoryServer
from realtime_translator.live_captions import (
    LiveCaptionsError,
    ensure_interactive_desktop,
    find_process_ids,
    find_window,
    launch_live_captions,
    prepare_live_captions,
)
from realtime_translator.overlay import OverlayApp
from realtime_translator.runtime_state import RuntimeStateStore
from realtime_translator.live_state import LiveCaptionStore
from realtime_translator.translation_worker import (
    TranslationWorker,
    print_summary,
)
from realtime_translator.translator import OpenAICompatibleTranslator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LiveCaptionsProcessManager:
    """Keep Live Captions configured and the UIA collector running."""

    def __init__(
        self,
        *,
        database_path: Path,
        live_state_path: Path,
        stable_after_ms: int,
        session_name: str | None = None,
        python_executable: str = sys.executable,
        cwd: Path | None = None,
        startup_delay_seconds: float = 2.0,
        find_process_ids_func: Callable[[], list[int]] = find_process_ids,
        launch_func: Callable[[], None] = launch_live_captions,
        find_window_func: Callable[..., object] = find_window,
        prepare_func: Callable[..., str] = prepare_live_captions,
        process_factory: Callable[..., object] = subprocess.Popen,
        ensure_desktop_func: Callable[[], str] = ensure_interactive_desktop,
        log: Callable[[str], None] = print,
    ) -> None:
        self.database_path = database_path.resolve()
        self.live_state_path = live_state_path.resolve()
        self.stable_after_ms = stable_after_ms
        self.session_name = session_name
        self.python_executable = python_executable
        self.cwd = (cwd or Path.cwd()).resolve()
        self.startup_delay_seconds = startup_delay_seconds
        self.find_process_ids = find_process_ids_func
        self.launch_live_captions = launch_func
        self.find_window = find_window_func
        self.prepare_live_captions = prepare_func
        self.process_factory = process_factory
        self.ensure_desktop_func = ensure_desktop_func
        self.log = log
        self._lock = threading.Lock()
        self._process: object | None = None

    @property
    def process(self) -> object | None:
        return self._process

    def ensure_running(self) -> None:
        with self._lock:
            self.ensure_desktop_func()
            process = self._process
            if process is not None and process.poll() is None:
                return

            if not self.find_process_ids():
                self.log("starting Windows Live Captions")
                self.launch_live_captions()
                if self.startup_delay_seconds:
                    time.sleep(self.startup_delay_seconds)

            window = self.find_window(timeout_seconds=10.0)
            state = self.prepare_live_captions(window)
            self.log(f"Live Captions state: {state}")

            command = [
                self.python_executable,
                "-m",
                "realtime_translator.uia_probe",
                "--duration",
                "86400",
                "--quiet-events",
                "--persist",
                "--db",
                str(self.database_path),
                "--live-state",
                str(self.live_state_path),
                "--stable-after-ms",
                str(self.stable_after_ms),
            ]
            if self.session_name:
                command.extend(["--session-name", self.session_name])
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            )
            self._process = self.process_factory(
                command,
                cwd=self.cwd,
                creationflags=creation_flags,
            )
            self.log(f"UIA collector started: pid={getattr(self._process, 'pid', '?')}")

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError, ValueError):
                process.terminate()
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the complete Japanese live-caption translator."
    )
    try:
        history_port = int(
            read_env_file(PROJECT_ROOT / ".env").get("HISTORY_PORT", "8765")
        )
    except (ConfigurationError, ValueError):
        parser.error("HISTORY_PORT must be an integer")
    if not 0 <= history_port <= 65535:
        parser.error("HISTORY_PORT must be between 0 and 65535")

    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "realtime_translator.sqlite3",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=PROJECT_ROOT / "data" / "runtime_state.json",
    )
    parser.add_argument(
        "--live-state",
        type=Path,
        default=PROJECT_ROOT / "data" / "live_caption.json",
    )
    parser.add_argument(
        "--session-name",
        help="Optional name for the current day's open session.",
    )
    parser.add_argument(
        "--history-port",
        type=int,
        default=history_port,
    )
    parser.add_argument(
        "--history-url",
        help="Use an already-running history service instead of starting one.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=1000,
    )
    return parser.parse_args()


def run_translation_worker_loop(
    *,
    database_path: Path,
    state_path: Path,
    config: TranslationConfig,
    stop_event: threading.Event,
    poll_interval_seconds: float,
    max_iterations: int | None = None,
    client_factory: Callable[
        [TranslationConfig],
        object,
    ] = OpenAICompatibleTranslator,
    output: Callable[[str], None] = print,
    error_output: Callable[[str], None] | None = None,
) -> None:
    write_error = error_output or (
        lambda message: print(message, file=sys.stderr)
    )
    try:
        with Database(database_path) as worker_database:
            runtime_state = RuntimeStateStore(state_path)
            worker = TranslationWorker(
                worker_database,
                client_factory(config),
                retry_delays_seconds=config.retry_delays_seconds,
                pause_check=lambda: runtime_state.load().paused,
                context_after_id=(
                    lambda: runtime_state.load().clear_after_segment_id
                ),
            )

            iterations = 0
            while not stop_event.is_set():
                if max_iterations is not None and iterations >= max_iterations:
                    return
                try:
                    summary = worker.run_once(limit=50)
                    if summary.attempted or summary.transient_failures:
                        print_summary(summary, output=output)
                except Exception as error:
                    write_error(f"translation worker error: {error}")
                iterations += 1
                stop_event.wait(poll_interval_seconds)
    except Exception as error:
        write_error(f"translation worker database error: {error}")


def run_application(
    args: argparse.Namespace,
    config: TranslationConfig,
) -> int:
    if args.poll_interval_ms <= 0:
        raise ValueError("--poll-interval-ms must be positive")
    config.require_api_key()

    database_path = args.db.resolve()
    state_path = args.state.resolve()
    live_state_path = args.live_state.resolve()
    stop_event = threading.Event()
    manager = LiveCaptionsProcessManager(
        database_path=database_path,
        live_state_path=live_state_path,
        stable_after_ms=config.final_debounce_ms,
        session_name=args.session_name,
        cwd=PROJECT_ROOT,
    )
    history_server: HistoryServer | None = None
    overlay_database: Database | None = None
    worker_thread: threading.Thread | None = None
    monitor_thread: threading.Thread | None = None
    previous_sigint: object = signal.getsignal(signal.SIGINT)

    try:
        manager.ensure_running()

        history_url = args.history_url
        if history_url is None:
            try:
                history_server = HistoryServer(
                    database_path,
                    port=args.history_port,
                )
                history_server.start()
                history_url = history_server.url
                print(f"history: {history_url}", flush=True)
            except (OSError, ValueError, FileNotFoundError) as error:
                print(
                    f"history server unavailable: {error}",
                    file=sys.stderr,
                )
                history_server = None

        overlay_database = Database(database_path)
        overlay = OverlayApp(
            database=overlay_database,
            state_store=RuntimeStateStore(state_path),
            live_store=LiveCaptionStore(live_state_path),
            history_url=history_url,
            poll_interval_ms=750,
        )

        def worker_loop() -> None:
            run_translation_worker_loop(
                database_path=database_path,
                state_path=state_path,
                config=config,
                stop_event=stop_event,
                poll_interval_seconds=args.poll_interval_ms / 1000.0,
            )

        def monitor_loop() -> None:
            while not stop_event.wait(2.0):
                try:
                    manager.ensure_running()
                except Exception as error:
                    print(f"live captions monitor: {error}", file=sys.stderr)

        worker_thread = threading.Thread(
            target=worker_loop,
            name="translation-worker",
            daemon=True,
        )
        monitor_thread = threading.Thread(
            target=monitor_loop,
            name="live-captions-monitor",
            daemon=True,
        )
        worker_thread.start()
        monitor_thread.start()

        def handle_sigint(_signum: int, _frame: object) -> None:
            overlay.root.after(0, overlay.close)

        signal.signal(signal.SIGINT, handle_sigint)
        overlay.run()
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        stop_event.set()
        if worker_thread is not None:
            worker_thread.join(timeout=3)
        if monitor_thread is not None:
            monitor_thread.join(timeout=3)
        manager.stop()
        if history_server is not None:
            history_server.stop()
        if overlay_database is not None:
            overlay_database.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        config = load_translation_config(PROJECT_ROOT / ".env")
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    try:
        return run_application(args, config)
    except (ConfigurationError, LiveCaptionsError, ValueError, OSError) as error:
        print(f"startup error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
