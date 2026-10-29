from __future__ import annotations

import argparse
import sys
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from realtime_translator.config import ConfigurationError, read_env_file
from realtime_translator.database import (
    Database,
    SegmentRecord,
    SessionRecord,
    SHANGHAI_TIMEZONE,
    parse_utc,
    shanghai_business_date,
)
from realtime_translator.history_server import HistoryServer
from realtime_translator.live_state import LiveCaptionSnapshot, LiveCaptionStore
from realtime_translator.runtime_state import RuntimeState, RuntimeStateStore

THEMES = {
    "dark": {
        "background": "#12151b",
        "surface": "#1b2029",
        "foreground": "#f4f5f7",
        "muted": "#aeb5c2",
        "accent": "#7db7ff",
        "warning": "#f0c36b",
        "button": "#272e39",
        "button_active": "#354052",
        "border": "#343c49",
    },
    "light": {
        "background": "#f3f5f8",
        "surface": "#ffffff",
        "foreground": "#17191d",
        "muted": "#606773",
        "accent": "#165fa8",
        "warning": "#9b6500",
        "button": "#e5e9ef",
        "button_active": "#d3dae4",
        "border": "#cbd2dc",
    },
}


class OverlayApp:
    def __init__(
        self,
        *,
        database: Database,
        state_store: RuntimeStateStore,
        live_store: LiveCaptionStore,
        history_url: str | None = None,
        poll_interval_ms: int = 750,
        visible: bool = True,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        self.database = database
        self.state_store = state_store
        self.live_store = live_store
        self.history_url = history_url
        self.poll_interval_ms = poll_interval_ms
        self.state = state_store.load()
        self.business_date = shanghai_business_date()
        self._last_signature: tuple[object, ...] | None = None
        self._refresh_job: str | None = None
        self._status_job: str | None = None
        self._drag_offset: tuple[int, int] | None = None
        self._stick_to_bottom = True
        self._suppressed_live_observed_at: str | None = None
        self._settings_window: tk.Toplevel | None = None
        self._stats_window: tk.Toplevel | None = None

        self.root = tk.Tk()
        self.root.title("实时日语字幕翻译")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if not visible:
            self.root.withdraw()

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.window_width = min(620, max(460, screen_width // 3))
        self.max_window_height = max(260, int(screen_height * 0.4))
        self.window_height = min(380, self.max_window_height)

        default_x = max(0, screen_width - self.window_width - 24)
        default_y = max(0, screen_height - self.window_height - 72)
        window_x = (
            self.state.window_x
            if self.state.window_x is not None
            else default_x
        )
        window_y = (
            self.state.window_y
            if self.state.window_y is not None
            else default_y
        )
        window_x = min(max(window_x, 0), max(0, screen_width - 180))
        window_y = min(max(window_y, 0), max(0, screen_height - 100))
        self.root.geometry(
            f"{self.window_width}x{self.window_height}+{window_x}+{window_y}"
        )

        self._build_widgets()
        self._apply_visuals()
        self._bind_events()
        self.refresh(force=True)
        self._schedule_refresh()

    def _build_widgets(self) -> None:
        self.header = tk.Frame(self.root, bd=0, highlightthickness=0)
        self.header.pack(fill=tk.X, padx=10, pady=(8, 4))

        self.session_label = tk.Label(
            self.header,
            text="今日暂无字幕",
            anchor=tk.W,
        )
        self.session_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.pause_button = self._make_button(self.header, "暂停", self.toggle_pause)
        self.clear_button = self._make_button(self.header, "清空", self.clear_view)
        self.stats_button = self._make_button(self.header, "统计", self.show_stats)
        self.history_button = self._make_button(
            self.header,
            "历史",
            self.open_history,
        )
        self.settings_button = self._make_button(
            self.header,
            "设置",
            self.show_settings,
        )
        self.close_button = self._make_button(self.header, "关闭", self.close)
        for button in (
            self.close_button,
            self.settings_button,
            self.history_button,
            self.stats_button,
            self.clear_button,
            self.pause_button,
        ):
            button.pack(side=tk.RIGHT, padx=(4, 0))

        self.content = tk.Frame(self.root, bd=0, highlightthickness=0)
        self.content.pack(fill=tk.BOTH, expand=True, padx=10, pady=2)

        self.text = tk.Text(
            self.content,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            cursor="arrow",
            insertwidth=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self.content,
            orient=tk.VERTICAL,
            command=self.text.yview,
        )
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.footer = tk.Frame(self.root, bd=0, highlightthickness=0)
        self.footer.pack(fill=tk.X, padx=10, pady=(2, 8))
        self.status_label = tk.Label(self.footer, text="", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.bottom_button = self._make_button(
            self.footer,
            "回到底部",
            self.scroll_to_bottom,
        )
        self.bottom_button.pack_forget()

    def _make_button(
        self,
        parent: tk.Misc,
        text: str,
        command: object,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0,
            padx=7,
            pady=3,
            cursor="hand2",
        )

    def _bind_events(self) -> None:
        for widget in (self.header, self.session_label):
            widget.bind("<ButtonPress-1>", self._begin_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<ButtonRelease-1>", self._end_drag)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.text.bind("<Key>", lambda _event: "break")
        self.text.bind("<ButtonRelease-1>", self._schedule_scroll_state)
        self.text.bind("<B1-Motion>", self._schedule_scroll_state)

    def _apply_visuals(self) -> None:
        colors = THEMES[self.state.theme]
        self.root.configure(bg=colors["background"])
        self.root.attributes("-alpha", self.state.opacity)
        for frame in (self.header, self.content, self.footer):
            frame.configure(bg=colors["background"])

        self.session_label.configure(
            bg=colors["background"],
            fg=colors["foreground"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.status_label.configure(
            bg=colors["background"],
            fg=colors["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        for button in (
            self.pause_button,
            self.clear_button,
            self.stats_button,
            self.history_button,
            self.settings_button,
            self.close_button,
            self.bottom_button,
        ):
            button.configure(
                bg=colors["button"],
                fg=colors["foreground"],
                activebackground=colors["button_active"],
                activeforeground=colors["foreground"],
                font=("Microsoft YaHei UI", 8),
            )

        self.text.configure(
            bg=colors["surface"],
            fg=colors["foreground"],
            selectbackground=colors["button_active"],
            selectforeground=colors["foreground"],
            font=("Microsoft YaHei UI", 11),
            spacing1=1,
            spacing2=2,
            spacing3=4,
        )
        self.text.tag_configure(
            "session",
            foreground=colors["accent"],
            font=("Microsoft YaHei UI", 9, "bold"),
            spacing1=7,
            spacing3=4,
        )
        self.text.tag_configure(
            "timestamp",
            foreground=colors["muted"],
            font=("Segoe UI", 8),
            spacing1=3,
        )
        self.text.tag_configure(
            "source",
            foreground=colors["foreground"],
            font=("Microsoft YaHei UI", 11),
            lmargin1=0,
            lmargin2=0,
            spacing1=1,
            spacing2=2,
            spacing3=2,
        )
        self.text.tag_configure(
            "translation",
            foreground=colors["foreground"],
            font=("Microsoft YaHei UI", 11),
            spacing1=1,
            spacing3=6,
        )
        self.text.tag_configure(
            "pending",
            foreground=colors["accent"],
            font=("Microsoft YaHei UI", 10),
            spacing1=1,
            spacing3=6,
        )
        self.text.tag_configure(
            "warning",
            foreground=colors["warning"],
            font=("Microsoft YaHei UI", 10),
            spacing1=1,
            spacing3=6,
        )
        self.pause_button.configure(
            text="继续" if self.state.paused else "暂停"
        )
        self._update_status_label()

    def refresh(self, *, force: bool = False) -> None:
        self._refresh_job = None
        try:
            state = self.state_store.load()
            if (
                state.theme != self.state.theme
                or abs(state.opacity - self.state.opacity) > 0.001
            ):
                self.state = state
                self._apply_visuals()
            self.state = state
            self.business_date = shanghai_business_date()
            records = self.database.list_segments_for_date(
                self.business_date,
                after_id=self.state.clear_after_segment_id,
            )
            sessions = self.database.list_sessions(self.business_date)
            live = self.live_store.load()
            if live is not None and live.business_date != self.business_date:
                live = None
            if (
                live is not None
                and self._suppressed_live_observed_at is not None
                and live.observed_at_utc == self._suppressed_live_observed_at
            ):
                live = None
            signature = self._signature(records, live)
            if force or signature != self._last_signature:
                self._render(records, sessions, live)
                self._last_signature = signature
            self._update_current_session(records, sessions, live)
            self._update_status_label()
        except Exception as error:
            self._show_transient_status(f"刷新失败：{error}")
        finally:
            self._schedule_refresh()

    @staticmethod
    def _signature(
        records: list[SegmentRecord],
        live: LiveCaptionSnapshot | None,
    ) -> tuple[object, ...]:
        return (
            tuple(
                (
                    record.id,
                    record.translation_status,
                    record.translation_zh,
                    record.last_error,
                )
                for record in records
            ),
            (
                live.session_id,
                live.source_ja,
                live.observed_at_utc,
            )
            if live is not None
            else None,
        )

    def _render(
        self,
        records: list[SegmentRecord],
        sessions: list[SessionRecord],
        live: LiveCaptionSnapshot | None,
    ) -> None:
        was_at_bottom = self._stick_to_bottom or self._at_bottom()
        current_view = self.text.yview()[0]
        session_names = {session.id: session.name for session in sessions}

        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        last_session_id: int | None = None
        card_starts: list[str] = []
        for record in records:
            card_starts.append(self.text.index(tk.END))
            if record.session_id != last_session_id:
                name = session_names.get(
                    record.session_id,
                    f"会话 {record.session_id}",
                )
                self.text.insert(tk.END, f"{name}\n", "session")
                last_session_id = record.session_id
            local_time = parse_utc(record.finalized_at_utc).astimezone(
                SHANGHAI_TIMEZONE
            )
            self.text.insert(
                tk.END,
                f"{local_time:%H:%M:%S}  ",
                "timestamp",
            )
            self.text.insert(tk.END, f"{record.source_ja}\n", "source")
            if (
                record.translation_status == "translation_done"
                and record.translation_zh
            ):
                self.text.insert(
                    tk.END,
                    f"{record.translation_zh}\n",
                    "translation",
                )
            elif record.translation_status == "translation_failed":
                self.text.insert(
                    tk.END,
                    "翻译暂不可用，请检查配置\n",
                    "warning",
                )
            elif record.last_error:
                self.text.insert(
                    tk.END,
                    "翻译暂不可用，等待自动重试\n",
                    "warning",
                )
            else:
                self.text.insert(
                    tk.END,
                    self._pending_text() + "\n",
                    "pending",
                )

        if live is not None:
            card_starts.append(self.text.index(tk.END))
            if live.session_id != last_session_id:
                self.text.insert(tk.END, f"{live.session_name}\n", "session")
            observed = parse_utc(live.observed_at_utc).astimezone(
                SHANGHAI_TIMEZONE
            )
            self.text.insert(
                tk.END,
                f"{observed:%H:%M:%S}  ",
                "timestamp",
            )
            self.text.insert(tk.END, f"{live.source_ja}\n", "source")
            self.text.insert(
                tk.END,
                self._pending_text() + "\n",
                "pending",
            )

        if not records and live is None:
            self.text.insert(
                tk.END,
                "等待今天的日语字幕…\n",
                "pending",
            )
        tail_start = card_starts[-4] if len(card_starts) >= 4 else "1.0"
        self.text.configure(state=tk.DISABLED)

        self.root.update_idletasks()
        self._resize_for_content(tail_start)
        if was_at_bottom:
            self.text.yview_moveto(1.0)
            self._stick_to_bottom = True
        else:
            self.text.yview_moveto(current_view)
            self._stick_to_bottom = False
        self._schedule_scroll_state()

    def _resize_for_content(self, tail_start: str) -> None:
        line_count = self.text.count(
            tail_start,
            "end-1c",
            "displaylines",
        )
        visible_lines = int(line_count[0]) if line_count else 1
        desired_height = max(170, 82 + visible_lines * 21)
        new_height = min(desired_height, self.max_window_height)
        if new_height == self.window_height:
            return
        bottom_edge = self.root.winfo_y() + self.window_height
        new_y = max(0, bottom_edge - new_height)
        self.window_height = new_height
        self.root.geometry(
            f"{self.window_width}x{self.window_height}"
            f"+{self.root.winfo_x()}+{new_y}"
        )

    def _update_current_session(
        self,
        records: list[SegmentRecord],
        sessions: list[SessionRecord],
        live: LiveCaptionSnapshot | None,
    ) -> None:
        if live is not None:
            name = live.session_name
        elif sessions:
            name = sessions[-1].name
        else:
            name = "今日暂无字幕"
        if len(name) > 48:
            name = name[:45] + "..."
        self.session_label.configure(text=name)

    def _update_status_label(self) -> None:
        if self.state.paused:
            text = "翻译已暂停；日文捕获与入库仍继续"
        else:
            text = "仅显示今天的内容"
        if self._status_job is not None:
            return
        self.status_label.configure(text=text)

    def _show_transient_status(self, text: str) -> None:
        if self._status_job is not None:
            self.root.after_cancel(self._status_job)
        self.status_label.configure(text=text)
        self._status_job = self.root.after(2500, self._clear_transient_status)

    def _clear_transient_status(self) -> None:
        self._status_job = None
        self._update_status_label()

    def _schedule_refresh(self) -> None:
        if self._refresh_job is None:
            self._refresh_job = self.root.after(
                self.poll_interval_ms,
                self.refresh,
            )

    def _begin_drag(self, event: tk.Event) -> None:
        if self._drag_offset is None:
            self._drag_offset = (
                event.x_root - self.root.winfo_x(),
                event.y_root - self.root.winfo_y(),
            )

    def _drag_window(self, event: tk.Event) -> None:
        if self._drag_offset is None:
            return
        x = max(0, event.x_root - self._drag_offset[0])
        y = max(0, event.y_root - self._drag_offset[1])
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event: tk.Event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self.state = self.state_store.update(
                window_x=self.root.winfo_x(),
                window_y=self.root.winfo_y(),
            )

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = int(event.delta)
        if delta == 0:
            return "break"
        self.text.yview_scroll(-3 if delta > 0 else 3, "units")
        self._schedule_scroll_state()
        return "break"

    def _schedule_scroll_state(self, _event: object | None = None) -> None:
        self.root.after_idle(self._update_scroll_state)

    def _update_scroll_state(self) -> None:
        self._stick_to_bottom = self._at_bottom()
        if self._stick_to_bottom:
            self.bottom_button.pack_forget()
        else:
            self.bottom_button.pack(side=tk.RIGHT)

    def _at_bottom(self) -> bool:
        return self.text.yview()[1] >= 0.999

    def _pending_text(self) -> str:
        return "翻译已暂停" if self.state.paused else "翻译中…"

    def scroll_to_bottom(self) -> None:
        self._stick_to_bottom = True
        self.text.yview_moveto(1.0)
        self.bottom_button.pack_forget()

    def toggle_pause(self) -> None:
        self.state = self.state_store.update(paused=not self.state.paused)
        self._apply_visuals()
        self._last_signature = None
        self._refresh_immediately()

    def clear_view(self) -> None:
        live = self.live_store.load()
        if live is not None:
            self._suppressed_live_observed_at = live.observed_at_utc
        latest_id = self.database.get_latest_segment_id_for_date(
            self.business_date
        )
        self.state = self.state_store.update(
            clear_after_segment_id=latest_id
        )
        self._last_signature = None
        self._stick_to_bottom = True
        self._refresh_immediately()
        self._show_transient_status("当前悬浮窗已清空，历史记录未删除")

    def _refresh_immediately(self) -> None:
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        self.refresh(force=True)

    def open_history(self) -> None:
        if self.history_url:
            webbrowser.open(self.history_url)
        else:
            self._show_transient_status("历史页面将在下一阶段接入")

    def show_stats(self) -> None:
        if self._stats_window is not None and self._stats_window.winfo_exists():
            self._stats_window.lift()
            return
        usage = self.database.translation_usage_summary(self.business_date)
        colors = THEMES[self.state.theme]
        window = tk.Toplevel(self.root)
        window.title("今日 API 统计")
        window.transient(self.root)
        window.attributes("-topmost", True)
        window.configure(bg=colors["background"])
        window.resizable(False, False)
        frame = tk.Frame(window, bg=colors["background"], padx=16, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)
        lines = [
            ("请求次数", usage.requests),
            ("成功次数", usage.successes),
            ("失败次数", usage.failures),
            ("输入 Token", usage.input_tokens),
            ("输出 Token", usage.output_tokens),
            ("估算费用", f"{usage.estimated_cost:.6f}"),
        ]
        for row, (label, value) in enumerate(lines):
            tk.Label(
                frame,
                text=label,
                bg=colors["background"],
                fg=colors["muted"],
                font=("Microsoft YaHei UI", 9),
                anchor=tk.W,
            ).grid(row=row, column=0, sticky=tk.W, pady=3)
            tk.Label(
                frame,
                text=str(value),
                bg=colors["background"],
                fg=colors["foreground"],
                font=("Microsoft YaHei UI", 9, "bold"),
                anchor=tk.E,
            ).grid(row=row, column=1, sticky=tk.E, padx=(20, 0), pady=3)
        tk.Button(
            frame,
            text="关闭",
            command=window.destroy,
            bd=0,
            bg=colors["button"],
            fg=colors["foreground"],
            activebackground=colors["button_active"],
            activeforeground=colors["foreground"],
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=3,
        ).grid(row=len(lines), column=0, columnspan=2, pady=(12, 0))
        self._stats_window = window

    def show_settings(self) -> None:
        if (
            self._settings_window is not None
            and self._settings_window.winfo_exists()
        ):
            self._settings_window.lift()
            return
        colors = THEMES[self.state.theme]
        window = tk.Toplevel(self.root)
        window.title("悬浮窗设置")
        window.transient(self.root)
        window.attributes("-topmost", True)
        window.configure(bg=colors["background"])
        window.resizable(False, False)
        frame = tk.Frame(window, bg=colors["background"], padx=16, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text="不透明度",
            bg=colors["background"],
            fg=colors["foreground"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W)
        opacity_scale = tk.Scale(
            frame,
            from_=0.35,
            to=1.0,
            resolution=0.01,
            orient=tk.HORIZONTAL,
            length=220,
            showvalue=True,
            bd=0,
            highlightthickness=0,
            bg=colors["background"],
            fg=colors["foreground"],
            troughcolor=colors["button"],
        )
        opacity_scale.set(self.state.opacity)
        opacity_scale.pack(fill=tk.X, pady=(2, 10))
        opacity_scale.configure(
            command=lambda value: self._change_opacity(float(value))
        )

        tk.Label(
            frame,
            text="主题",
            bg=colors["background"],
            fg=colors["foreground"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W)
        theme_value = tk.StringVar(value=self.state.theme)
        choices = tk.Frame(frame, bg=colors["background"])
        choices.pack(anchor=tk.W, pady=(3, 10))
        for value, label in (("dark", "深色"), ("light", "浅色")):
            tk.Radiobutton(
                choices,
                text=label,
                value=value,
                variable=theme_value,
                command=lambda: self._change_theme(theme_value.get()),
                bg=colors["background"],
                fg=colors["foreground"],
                activebackground=colors["background"],
                activeforeground=colors["foreground"],
                selectcolor=colors["surface"],
                font=("Microsoft YaHei UI", 9),
            ).pack(side=tk.LEFT, padx=(0, 10))

        tk.Button(
            frame,
            text="关闭",
            command=window.destroy,
            bd=0,
            bg=colors["button"],
            fg=colors["foreground"],
            activebackground=colors["button_active"],
            activeforeground=colors["foreground"],
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=3,
        ).pack(anchor=tk.E)
        self._settings_window = window

    def _change_opacity(self, opacity: float) -> None:
        self.root.attributes("-alpha", opacity)
        self.state = self.state_store.update(opacity=opacity)

    def _change_theme(self, theme: str) -> None:
        self.state = self.state_store.update(theme=theme)
        self._apply_visuals()
        self._last_signature = None
        self._refresh_immediately()
        if self._settings_window is not None:
            self._settings_window.destroy()
            self._settings_window = None
            self.show_settings()

    def close(self) -> None:
        try:
            self.state_store.update(
                window_x=self.root.winfo_x(),
                window_y=self.root.winfo_y(),
            )
        finally:
            if self._refresh_job is not None:
                self.root.after_cancel(self._refresh_job)
                self._refresh_job = None
            self.database.close()
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show today's bilingual captions in a topmost overlay."
    )
    try:
        default_history_port = int(
            read_env_file(".env").get("HISTORY_PORT", "8765")
        )
    except (ConfigurationError, ValueError):
        parser.error("HISTORY_PORT must be an integer")
    if not 0 <= default_history_port <= 65535:
        parser.error("HISTORY_PORT must be between 0 and 65535")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "realtime_translator.sqlite3",
        help="SQLite database containing captured segments.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("data") / "runtime_state.json",
        help="Runtime control file shared with the translation worker.",
    )
    parser.add_argument(
        "--live-state",
        type=Path,
        default=Path("data") / "live_caption.json",
        help="Ephemeral file written by the UIA probe.",
    )
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=750,
        help="Database polling interval in milliseconds.",
    )
    parser.add_argument(
        "--history-url",
        help="Local history-page URL opened by the History button.",
    )
    parser.add_argument(
        "--history-port",
        type=int,
        default=default_history_port,
        help="Local port for the embedded history server.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.poll_ms <= 0:
        print("error: --poll-ms must be positive", file=sys.stderr)
        return 2
    if args.history_port < 0 or args.history_port > 65535:
        print("error: --history-port must be between 0 and 65535", file=sys.stderr)
        return 2

    history_server: HistoryServer | None = None
    history_url = args.history_url
    if history_url is None:
        try:
            history_server = HistoryServer(
                args.db,
                port=args.history_port,
            )
            history_server.start()
            history_url = history_server.url
        except (OSError, ValueError, FileNotFoundError) as error:
            print(
                f"warning: history server unavailable: {error}",
                file=sys.stderr,
            )

    database = Database(args.db)
    try:
        app = OverlayApp(
            database=database,
            state_store=RuntimeStateStore(args.state),
            live_store=LiveCaptionStore(args.live_state),
            history_url=history_url,
            poll_interval_ms=args.poll_ms,
        )
        app.run()
        return 0
    finally:
        if history_server is not None:
            history_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
