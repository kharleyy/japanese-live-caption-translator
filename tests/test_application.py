from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from realtime_translator.application import (
    LiveCaptionsProcessManager,
    run_translation_worker_loop,
)
from realtime_translator.config import TranslationConfig
from realtime_translator.database import Database
from realtime_translator.live_captions import InteractiveDesktopRequired
from realtime_translator.translator import TranslationResult


class FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.pid = 1234
        self.exit_code: int | None = None
        self.signals: list[object] = []

    def poll(self) -> int | None:
        return self.exit_code

    def send_signal(self, value: object) -> None:
        self.signals.append(value)
        self.exit_code = 0

    def terminate(self) -> None:
        self.exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code or 0

    def kill(self) -> None:
        self.exit_code = -9


class InThreadTranslator:
    def translate(self, source_ja, context=()):
        return TranslationResult(
            translation_zh=f"译:{source_ja}",
            input_tokens=10,
            output_tokens=4,
            estimated_cost=0.0001,
        )


class LiveCaptionsProcessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.commands: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self.launch_count = 0
        self.prepare_count = 0
        self.logs: list[str] = []
        self.running_process_ids: list[int] = []

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def process_factory(self, command, **_kwargs):
        self.commands.append(command)
        process = FakeProcess(command)
        self.processes.append(process)
        return process

    def manager(self) -> LiveCaptionsProcessManager:
        def launch() -> None:
            self.launch_count += 1
            self.running_process_ids.append(999)

        def prepare(_window) -> str:
            self.prepare_count += 1
            return "ready"

        return LiveCaptionsProcessManager(
            database_path=self.root / "data.sqlite3",
            live_state_path=self.root / "live.json",
            stable_after_ms=1500,
            session_name="自动联调",
            cwd=self.root,
            startup_delay_seconds=0,
            find_process_ids_func=lambda: list(self.running_process_ids),
            launch_func=launch,
            find_window_func=lambda **_kwargs: object(),
            prepare_func=prepare,
            process_factory=self.process_factory,
            ensure_desktop_func=lambda: "Default",
            log=self.logs.append,
        )

    def test_launches_captions_selects_japanese_and_reuses_process(self) -> None:
        manager = self.manager()
        manager.ensure_running()

        self.assertEqual(self.launch_count, 1)
        self.assertEqual(self.prepare_count, 1)
        self.assertEqual(len(self.processes), 1)
        command = self.commands[0]
        self.assertIn("realtime_translator.uia_probe", command)
        self.assertIn("--persist", command)
        self.assertIn("--quiet-events", command)
        self.assertIn("自动联调", command)

        manager.ensure_running()
        self.assertEqual(len(self.processes), 1)

        manager.stop()
        self.assertEqual(self.processes[0].exit_code, 0)

    def test_restarts_collector_after_process_exit(self) -> None:
        manager = self.manager()
        manager.ensure_running()
        self.processes[0].exit_code = 3

        manager.ensure_running()

        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.prepare_count, 2)
        manager.stop()

    def test_refuses_to_launch_on_isolated_desktop(self) -> None:
        def reject_desktop() -> str:
            raise InteractiveDesktopRequired("isolated desktop")

        manager = LiveCaptionsProcessManager(
            database_path=self.root / "data.sqlite3",
            live_state_path=self.root / "live.json",
            stable_after_ms=1500,
            startup_delay_seconds=0,
            find_process_ids_func=lambda: [],
            launch_func=lambda: self.fail("launch should not be called"),
            find_window_func=lambda **_kwargs: object(),
            prepare_func=lambda _window: "ready",
            process_factory=self.process_factory,
            ensure_desktop_func=reject_desktop,
            log=self.logs.append,
        )

        with self.assertRaises(InteractiveDesktopRequired):
            manager.ensure_running()
        self.assertEqual(self.processes, [])

    def test_translation_worker_uses_database_inside_worker_thread(self) -> None:
        database_path = self.root / "threaded.sqlite3"
        with Database(database_path) as database:
            session = database.create_session(
                "线程测试",
                started_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            segment = database.add_finalized_segment(session.id, "本文")

        errors: list[str] = []
        worker_thread = threading.Thread(
            target=run_translation_worker_loop,
            kwargs={
                "database_path": database_path,
                "state_path": self.root / "runtime.json",
                "config": TranslationConfig(api_key="test-key"),
                "stop_event": threading.Event(),
                "poll_interval_seconds": 0.01,
                "max_iterations": 1,
                "client_factory": lambda _config: InThreadTranslator(),
                "output": lambda _message: None,
                "error_output": errors.append,
            },
        )
        worker_thread.start()
        worker_thread.join(timeout=3)

        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(errors, [])
        with Database(database_path) as database:
            self.assertEqual(
                database.get_segment(segment.id).translation_zh,
                "译:本文",
            )


if __name__ == "__main__":
    unittest.main()
