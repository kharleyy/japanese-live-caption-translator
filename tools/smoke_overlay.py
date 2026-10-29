from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from realtime_translator.database import Database
from realtime_translator.live_state import LiveCaptionSnapshot, LiveCaptionStore
from realtime_translator.overlay import OverlayApp
from realtime_translator.runtime_state import RuntimeStateStore


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_directory:
        root = Path(temp_directory)
        database_path = root / "smoke.sqlite3"
        database = Database(database_path)
        now = datetime.now(timezone.utc)
        first_session = database.create_session(
            "冒烟测试会话一",
            started_at=now - timedelta(minutes=2),
        )
        second_session = database.create_session(
            "冒烟测试会话二",
            started_at=now - timedelta(minutes=1),
        )
        for index in range(9):
            segment = database.add_finalized_segment(
                first_session.id if index < 2 else second_session.id,
                f"テスト字幕{index + 1}",
                finalized_at=now - timedelta(seconds=40 - index * 3),
            )
            if index != 8:
                database.mark_translation_done(
                    segment.id,
                    f"测试译文{index + 1}",
                    input_tokens=10,
                    output_tokens=4,
                    estimated_cost=0.0001,
                )
        previous_day_session = database.create_session(
            "前一天会话",
            started_at=now - timedelta(days=1),
        )
        database.add_finalized_segment(
            previous_day_session.id,
            "前日の記録",
            finalized_at=now - timedelta(days=1),
        )

        live_store = LiveCaptionStore(root / "live_caption.json")
        live_store.save(
            LiveCaptionSnapshot(
                session_id=second_session.id,
                session_name=second_session.name,
                business_date=second_session.business_date,
                source_ja="現在入力中の字幕",
                observed_at_utc=now.isoformat(timespec="milliseconds"),
            )
        )
        app = OverlayApp(
            database=database,
            state_store=RuntimeStateStore(root / "runtime_state.json"),
            live_store=live_store,
            visible=False,
        )
        app.root.update()
        rendered = app.text.get("1.0", "end-1c")
        assert "テスト字幕1" in rendered
        assert "测试译文1" in rendered
        assert "翻译中…" in rendered
        assert "現在入力中の字幕" in rendered
        assert "前日の記録" not in rendered
        assert all(
            button.winfo_manager() == "pack"
            for button in (
                app.pause_button,
                app.clear_button,
                app.stats_button,
                app.history_button,
                app.settings_button,
                app.close_button,
            )
        )

        app.text.yview_moveto(0)
        app._stick_to_bottom = False
        app.root.update()
        database.add_finalized_segment(
            second_session.id,
            "新しく追加された字幕",
            finalized_at=now + timedelta(seconds=1),
        )
        app._last_signature = None
        app._refresh_immediately()
        app.root.update()
        assert "新しく追加された字幕" in app.text.get("1.0", "end-1c")
        assert app.text.yview()[1] < 0.999

        app.toggle_pause()
        app.root.update()
        assert app.state.paused
        assert "翻译已暂停" in app.text.get("1.0", "end-1c")

        app.clear_view()
        app.root.update()
        assert "テスト字幕1" not in app.text.get("1.0", "end-1c")
        app.close()
    print("overlay smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
