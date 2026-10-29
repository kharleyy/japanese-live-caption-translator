from __future__ import annotations

import argparse
import html
import secrets
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, quote, urlparse

from realtime_translator.config import ConfigurationError, read_env_file
from realtime_translator.database import (
    Database,
    SegmentRecord,
    SessionRecord,
    SHANGHAI_TIMEZONE,
    parse_utc,
    shanghai_business_date,
    validate_business_date,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_DIR = PROJECT_ROOT / "templates"
DEFAULT_STATIC_DIR = PROJECT_ROOT / "static"
MAX_FORM_BYTES = 64 * 1024


class HistoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        database_path: Path,
        template_dir: Path,
        static_dir: Path,
    ) -> None:
        super().__init__(server_address, HistoryRequestHandler)
        self.database_path = database_path
        self.template_dir = template_dir
        self.static_dir = static_dir
        self.csrf_token = secrets.token_urlsafe(32)


class HistoryServer:
    def __init__(
        self,
        database_path: str | Path,
        *,
        port: int = 8765,
        host: str = "127.0.0.1",
        template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
        static_dir: str | Path = DEFAULT_STATIC_DIR,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("history server may only bind to 127.0.0.1")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.database_path = Path(database_path).resolve()
        self.host = host
        self.requested_port = port
        self.template_dir = Path(template_dir)
        self.static_dir = Path(static_dir)
        self._server: HistoryHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self.requested_port
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        if self._server is not None:
            return
        if not (self.template_dir / "history.html").is_file():
            raise FileNotFoundError(
                f"history template not found: {self.template_dir / 'history.html'}"
            )
        server = HistoryHTTPServer(
            (self.host, self.requested_port),
            database_path=self.database_path,
            template_dir=self.template_dir,
            static_dir=self.static_dir,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="history-server",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

    def serve_forever(self) -> None:
        self.start()
        try:
            self.wait()
        finally:
            self.stop()

    def wait(self) -> None:
        if self._thread is None:
            return
        try:
            self._thread.join()
        except KeyboardInterrupt:
            pass


class HistoryRequestHandler(BaseHTTPRequestHandler):
    server: HistoryHTTPServer
    server_version = "RealtimeTranslatorHistory/1.0"
    sys_version = ""
    static_files: ClassVar[dict[str, tuple[str, str]]] = {
        "/static/history.css": ("history.css", "text/css; charset=utf-8"),
        "/static/history.js": (
            "history.js",
            "application/javascript; charset=utf-8",
        ),
    }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_history(parsed.query)
            return
        if parsed.path in self.static_files:
            self._serve_static(parsed.path)
            return
        self._send_text(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path in self.static_files:
            self._send_headers(HTTPStatus.OK, self._content_type(parsed.path))
            return
        self._send_headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/delete-day":
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            form = self._read_form()
        except ValueError as error:
            self._send_text(HTTPStatus.BAD_REQUEST, str(error))
            return

        csrf_token = form.get("csrf_token", "")
        if not secrets.compare_digest(csrf_token, self.server.csrf_token):
            self._send_text(HTTPStatus.FORBIDDEN, "Invalid confirmation token")
            return

        try:
            business_date = validate_business_date(form.get("date", ""))
        except ValueError:
            self._send_text(HTTPStatus.BAD_REQUEST, "Invalid date")
            return
        if form.get("confirm_date", "") != business_date:
            self._send_text(
                HTTPStatus.BAD_REQUEST,
                "Type the exact business date to confirm deletion",
            )
            return

        with Database(self.server.database_path) as database:
            result = database.delete_day(business_date)
        query = (
            f"?date={quote(result.business_date)}"
            f"&deleted_sessions={result.deleted_sessions}"
            f"&deleted_segments={result.deleted_segments}"
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/{query}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, path: str) -> None:
        filename, content_type = self.static_files[path]
        file_path = self.server.static_dir / filename
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_response(
            HTTPStatus.OK,
            body,
            content_type=content_type,
            cache_control="public, max-age=3600",
        )

    def _serve_history(self, query_string: str) -> None:
        query = parse_qs(query_string)
        requested_date = query.get("date", [""])[0]
        selected_date = (
            requested_date if requested_date else shanghai_business_date()
        )
        notice = ""
        is_error = False
        try:
            selected_date = validate_business_date(selected_date)
        except ValueError:
            selected_date = shanghai_business_date()
            notice = "日期格式无效，已显示今天。"
            is_error = True

        deleted_sessions = _query_int(query, "deleted_sessions")
        deleted_segments = _query_int(query, "deleted_segments")
        if deleted_sessions is not None and deleted_segments is not None:
            notice = (
                f"已删除 {selected_date} 的 "
                f"{deleted_sessions} 个会话和 {deleted_segments} 条字幕。"
            )

        with Database(self.server.database_path) as database:
            sessions = database.list_sessions(selected_date)
            segments = database.list_segments_for_date(selected_date)
        content = render_history_content(
            selected_date,
            sessions,
            segments,
        )
        template_path = self.server.template_dir / "history.html"
        template = template_path.read_text(encoding="utf-8")
        page = (
            template.replace("__DATE__", html.escape(selected_date))
            .replace("__CONTENT__", content)
            .replace("__CSRF_TOKEN__", html.escape(self.server.csrf_token))
            .replace("__NOTICE_CLASS__", "error" if is_error else "notice")
            .replace("__NOTICE__", html.escape(notice) if notice else "")
            .replace(
                "__DELETE_DISABLED__",
                "" if sessions else "disabled",
            )
            .replace(
                "__GENERATED_AT__",
                html.escape(
                    datetime.now(timezone.utc)
                    .astimezone(SHANGHAI_TIMEZONE)
                    .strftime("%Y-%m-%d %H:%M:%S")
                ),
            )
        )
        self._send_response(
            HTTPStatus.OK,
            page.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            cache_control="no-store",
        )

    def _read_form(self) -> dict[str, str]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise ValueError("Unsupported content type")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length") from error
        if content_length <= 0 or content_length > MAX_FORM_BYTES:
            raise ValueError("Invalid form size")
        body = self.rfile.read(content_length).decode("utf-8", errors="strict")
        return {
            key: values[0]
            for key, values in parse_qs(
                body,
                keep_blank_values=True,
            ).items()
        }

    def _content_type(self, path: str) -> str:
        if path == "/":
            return "text/html; charset=utf-8"
        return self.static_files[path][1]

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_response(
            status,
            text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            cache_control="no-store",
        )

    def _send_response(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_headers(self, status: HTTPStatus, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def render_history_content(
    business_date: str,
    sessions: list[SessionRecord],
    segments: list[SegmentRecord],
) -> str:
    segments_by_session: dict[int, list[SegmentRecord]] = defaultdict(list)
    for segment in segments:
        segments_by_session[segment.session_id].append(segment)

    if not sessions:
        return (
            '<section class="empty">'
            f"<p>{html.escape(business_date)} 没有字幕记录。</p>"
            "</section>"
        )

    parts = [
        '<p class="summary">'
        f"{len(sessions)} 个会话，{len(segments)} 条字幕"
        "</p>"
    ]
    for session in sessions:
        started = parse_utc(session.started_at_utc).astimezone(
            SHANGHAI_TIMEZONE
        )
        ended_text = ""
        if session.ended_at_utc:
            ended = parse_utc(session.ended_at_utc).astimezone(
                SHANGHAI_TIMEZONE
            )
            ended_text = f" - {ended:%H:%M:%S}"
        parts.append('<section class="session">')
        parts.append(
            '<div class="session-heading">'
            f"<h2>{html.escape(session.name)}</h2>"
            f"<p>{started:%Y-%m-%d %H:%M:%S}{ended_text}</p>"
            "</div>"
        )
        session_segments = segments_by_session.get(session.id, [])
        if not session_segments:
            parts.append('<p class="empty-row">此会话没有字幕记录。</p>')
        for segment in session_segments:
            finalized = parse_utc(segment.finalized_at_utc).astimezone(
                SHANGHAI_TIMEZONE
            )
            parts.append('<article class="segment">')
            parts.append(
                '<div class="segment-meta">'
                f"<time>{finalized:%H:%M:%S}</time>"
                f"{_status_badge(segment)}"
                "</div>"
            )
            parts.append(
                '<p class="source">'
                f"{html.escape(segment.source_ja)}"
                "</p>"
            )
            if segment.translation_status == "translation_done":
                translation = segment.translation_zh or ""
                parts.append(
                    '<p class="translation">'
                    f"{html.escape(translation)}"
                    "</p>"
                )
            elif segment.translation_status == "translation_failed":
                parts.append(
                    '<p class="translation unavailable">'
                    "翻译失败，请检查配置。"
                    "</p>"
                )
            else:
                parts.append(
                    '<p class="translation pending">'
                    "翻译中…"
                    "</p>"
                )
            parts.append("</article>")
        parts.append("</section>")
    return "\n".join(parts)


def _status_badge(segment: SegmentRecord) -> str:
    labels = {
        "translation_done": ("done", "已翻译"),
        "translation_pending": ("pending", "待翻译"),
        "translation_failed": ("failed", "失败"),
    }
    css_class, label = labels[segment.translation_status]
    return f'<span class="badge {css_class}">{label}</span>'


def _query_int(query: dict[str, list[str]], key: str) -> int | None:
    raw = query.get(key, [""])[0]
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the local caption history page."
    )
    try:
        default_port = int(read_env_file(".env").get("HISTORY_PORT", "8765"))
    except (ConfigurationError, ValueError):
        parser.error("HISTORY_PORT must be an integer")
    if not 0 <= default_port <= 65535:
        parser.error("HISTORY_PORT must be between 0 and 65535")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "realtime_translator.sqlite3",
        help="SQLite database containing caption history.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Local port. The server always binds to 127.0.0.1.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the history page in the default browser after starting.",
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        server = HistoryServer(args.db, port=args.port)
        server.start()
    except (OSError, ValueError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"history: {server.url}", flush=True)
    if args.open:
        webbrowser.open(server.url)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
