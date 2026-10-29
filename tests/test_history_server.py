from __future__ import annotations

import http.client
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from realtime_translator.database import Database
from realtime_translator.history_server import HistoryServer


class HistoryServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name) / "history.sqlite3"
        with Database(self.db_path) as database:
            first = database.create_session(
                "会话 <A>",
                started_at="2026-10-24T01:00:00+00:00",
            )
            second = database.create_session(
                "同日第二会话",
                started_at="2026-10-24T02:00:00+00:00",
            )
            completed = database.add_finalized_segment(
                first.id,
                "こんにちは & テスト",
                finalized_at="2026-10-24T01:00:01+00:00",
            )
            database.mark_translation_done(completed.id, "你好 & 测试")
            database.add_finalized_segment(
                second.id,
                "待っています",
                finalized_at="2026-10-24T02:00:01+00:00",
            )
            other_day = database.create_session(
                "其他日期",
                started_at="2026-10-25T01:00:00+00:00",
            )
            database.add_finalized_segment(
                other_day.id,
                "残す記録",
                finalized_at="2026-10-25T01:00:01+00:00",
            )

        self.server = HistoryServer(self.db_path, port=0)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.port,
            timeout=3,
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
        connection.close()
        return result

    def test_server_binds_loopback_and_renders_selected_date(self) -> None:
        self.assertEqual(
            self.server._server.server_address[0],
            "127.0.0.1",
        )
        status, headers, body = self.request(
            "GET",
            "/?date=2026-10-24",
        )
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn("会话 &lt;A&gt;", page)
        self.assertIn("こんにちは &amp; テスト", page)
        self.assertIn("待っています", page)
        self.assertIn("翻译中…", page)
        self.assertNotIn("残す記録", page)
        self.assertIn("csrf_token", page)

    def test_static_css_is_served(self) -> None:
        status, headers, body = self.request("GET", "/static/history.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["content-type"])
        self.assertIn(b"--accent", body)

    def test_delete_requires_csrf_and_exact_second_confirmation(self) -> None:
        _, _, page_body = self.request("GET", "/?date=2026-10-24")
        page = page_body.decode("utf-8")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match)
        csrf_token = match.group(1)

        common_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        wrong_token = urlencode(
            {
                "csrf_token": "wrong",
                "date": "2026-10-24",
                "confirm_date": "2026-10-24",
            }
        ).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/delete-day",
            body=wrong_token,
            headers=common_headers,
        )
        self.assertEqual(status, 403)

        wrong_date = urlencode(
            {
                "csrf_token": csrf_token,
                "date": "2026-10-24",
                "confirm_date": "wrong",
            }
        ).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/delete-day",
            body=wrong_date,
            headers=common_headers,
        )
        self.assertEqual(status, 400)

        valid = urlencode(
            {
                "csrf_token": csrf_token,
                "date": "2026-10-24",
                "confirm_date": "2026-10-24",
            }
        ).encode("utf-8")
        status, headers, _ = self.request(
            "POST",
            "/delete-day",
            body=valid,
            headers=common_headers,
        )
        self.assertEqual(status, 303)
        self.assertIn("deleted_sessions=2", headers["location"])
        self.assertIn("deleted_segments=2", headers["location"])

        with Database(self.db_path) as database:
            self.assertEqual(
                database.list_segments_for_date("2026-10-24"),
                [],
            )
            self.assertEqual(
                len(database.list_segments_for_date("2026-10-25")),
                1,
            )

    def test_invalid_date_falls_back_to_today(self) -> None:
        status, _, body = self.request("GET", "/?date=not-a-date")
        self.assertEqual(status, 200)
        self.assertIn("日期格式无效", body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
