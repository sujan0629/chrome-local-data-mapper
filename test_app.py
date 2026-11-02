#!/usr/bin/env python3
"""
Comprehensive unit and HTTP integration test suite for Chrome History SQLite Studio.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class TestChromeHistoryStudio(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="chrome_history_unittest_")
        self.sandbox_db = os.path.join(self.temp_dir, "sandbox_History.db")
        self.custom_db = os.path.join(self.temp_dir, "custom_History")
        self.manager = app.HistoryDBManager(sandbox_path=self.sandbox_db)

        # Start ephemeral HTTP server on port 0 (OS-assigned free port)
        app.HistoryRequestHandler.db_manager = self.manager
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.HistoryRequestHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _api(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_01_webkit_timestamp_conversion_and_past_date(self):
        """
        Verify adding a link with '2022-08-15 14:30:00' writes the exact 17-digit
        WebKit timestamp to both visits.visit_time and urls.last_visit_time.
        """
        dt_str = "2022-08-15 14:30:00"
        expected_webkit = app.datetime_local_to_webkit(dt_str)
        self.assertEqual(len(str(expected_webkit)), 17)

        status, res = self._api(
            "POST",
            "/api/visits",
            {
                "url": "https://chromium.org/backdate-test",
                "title": "Chromium Backdate Test",
                "datetime_local": dt_str,
                "transition": 805306368,
                "duration_seconds": 60,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(res["webkit_timestamp"], expected_webkit)

        conn = sqlite3.connect(self.sandbox_db)
        conn.row_factory = sqlite3.Row
        v_row = conn.execute("SELECT * FROM visits WHERE id = ?", (res["visit_id"],)).fetchone()
        u_row = conn.execute("SELECT * FROM urls WHERE id = ?", (res["url_id"],)).fetchone()
        conn.close()

        self.assertEqual(v_row["visit_time"], expected_webkit)
        self.assertEqual(u_row["last_visit_time"], expected_webkit)
        self.assertEqual(u_row["visit_count"], 1)

    def test_02_second_visit_increments_visit_count_and_updates_last_visit_time(self):
        """
        Verify adding a second visit to the same URL increments urls.visit_count to 2
        and sets urls.last_visit_time to the newer timestamp.
        """
        url = "https://developer.mozilla.org/en-US/docs/Web"
        _, res1 = self._api(
            "POST",
            "/api/visits",
            {
                "url": url,
                "title": "MDN Web Docs",
                "datetime_local": "2022-08-15 14:30:00",
                "transition": 805306368,
            },
        )
        newer_dt = "2024-03-10 18:45:12"
        expected_newer_webkit = app.datetime_local_to_webkit(newer_dt)
        _, res2 = self._api(
            "POST",
            "/api/visits",
            {
                "url": url,
                "title": "MDN Web Docs Updated",
                "datetime_local": newer_dt,
                "transition": 805306369,  # Typed URL
            },
        )
        self.assertEqual(res1["url_id"], res2["url_id"])
        self.assertEqual(res2["url_record"]["visit_count"], 2)
        self.assertEqual(res2["url_record"]["typed_count"], 1)
        self.assertEqual(res2["url_record"]["last_visit_time"], expected_newer_webkit)

    def test_03_edit_and_delete_synchronization(self):
        """
        Verify editing or deleting visits keeps urls and visits synchronized,
        and deleting the last visit removes the parent urls row.
        """
        url = "https://sqlite.org/index.html"
        _, v1 = self._api(
            "POST",
            "/api/visits",
            {"url": url, "title": "SQLite Home", "datetime_local": "2022-05-01 10:00:00"},
        )
        _, v2 = self._api(
            "POST",
            "/api/visits",
            {"url": url, "title": "SQLite Home", "datetime_local": "2022-06-01 10:00:00"},
        )

        # Backdate v2 to 2020 so v1 becomes the latest visit
        v1_webkit = app.datetime_local_to_webkit("2022-05-01 10:00:00")
        status, edit_res = self._api(
            "PUT",
            f"/api/visits/{v2['visit_id']}",
            {"datetime_local": "2020-01-01 00:00:00", "title": "SQLite Backdated"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(edit_res["url_record"]["visit_count"], 2)
        self.assertEqual(edit_res["url_record"]["last_visit_time"], v1_webkit)

        # Delete v1 -> only v2 remains
        status, del1 = self._api("DELETE", f"/api/visits/{v1['visit_id']}")
        self.assertEqual(status, 200)
        self.assertFalse(del1["url_deleted"])
        self.assertEqual(del1["url_record"]["visit_count"], 1)

        # Delete v2 -> parent URL is automatically removed
        status, del2 = self._api("DELETE", f"/api/visits/{v2['visit_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(del2["url_deleted"])

    def test_04_custom_mode_backup_and_extra_notnull_columns(self):
        """
        Verify custom mode creates a timestamped .bak backup before writing and
        inspects PRAGMA table_info to populate extra NOT NULL columns with safe defaults.
        """
        conn = sqlite3.connect(self.custom_db)
        conn.execute(
            """
            CREATE TABLE urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url LONGVARCHAR,
                title LONGVARCHAR,
                visit_count INTEGER DEFAULT 0 NOT NULL,
                typed_count INTEGER DEFAULT 0 NOT NULL,
                last_visit_time INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0 NOT NULL,
                custom_notnull_text TEXT NOT NULL,
                custom_notnull_int INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url INTEGER NOT NULL,
                visit_time INTEGER NOT NULL,
                from_visit INTEGER DEFAULT 0,
                transition INTEGER DEFAULT 805306368 NOT NULL,
                segment_id INTEGER DEFAULT 0,
                visit_duration INTEGER DEFAULT 0 NOT NULL,
                originator_cache_guid TEXT NOT NULL,
                visited_link_id INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        status, mode_res = self._api("POST", "/api/db/mode", {"mode": "custom", "path": self.custom_db})
        self.assertEqual(status, 200)
        self.assertEqual(mode_res["mode"], "custom")

        status, add_res = self._api(
            "POST",
            "/api/visits",
            {
                "url": "https://google.com",
                "title": "Google",
                "datetime_local": "2022-08-15 14:30:00",
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(add_res["backup_created"])
        self.assertTrue(os.path.isfile(add_res["backup_created"]))
        self.assertTrue(add_res["backup_created"].endswith(".bak"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
