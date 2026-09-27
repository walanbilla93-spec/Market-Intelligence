from __future__ import annotations

import hashlib
import http.client
import io
import asyncio
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.parse
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from market_intelligence.app import run_forever
from market_intelligence.export_ui import (
    ExportError, ExportServer, ExportSettings, FailureLimiter, _safe_child,
    create_export, start_export_ui,
)
from market_intelligence.models import Observation
from market_intelligence.storage import Storage
from market_intelligence.util import iso_utc


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-with-at-least-thirty-two-bytes-123456"


class ExportUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.store = Storage(self.data, "export-test", ROOT / "migrations")
        self.store.add_observation(Observation(
            source="test", metric="price", instrument="BTC", value_num=1.0, unit="USD",
            observed_at_utc=iso_utc(), available_to_system_at_utc=iso_utc(),
            source_url="https://example.test",
        ))
        self.settings = ExportSettings(
            enabled=True, host="127.0.0.1", port=0, token=TOKEN, require_https=True,
            session_seconds=3600, request_timeout_seconds=30,
            max_input_bytes=64 * 1024 * 1024, max_zip_bytes=64 * 1024 * 1024,
            tmp_quota_bytes=64 * 1024 * 1024, tmp_dir=self.root, max_http_threads=4,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _start(self) -> tuple[ExportServer, threading.Thread]:
        server = ExportServer(self.settings, self.store.db_path, self.store.export_dir, ROOT / "migrations")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, thread

    @staticmethod
    def _request(server: ExportServer, method: str, path: str, body: str = "", headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        merged = {"X-Forwarded-Proto": "https", **(headers or {})}
        connection.request(method, path, body=body.encode(), headers=merged)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def test_disabled_default_and_short_token_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(start_export_ui(self.store.db_path, self.store.export_dir, ROOT / "migrations"))
        with patch.dict(os.environ, {"MI_EXPORT_UI_ENABLED": "true", "MI_EXPORT_TOKEN": "short"}, clear=True):
            with self.assertRaises(ValueError):
                start_export_ui(self.store.db_path, self.store.export_dir, ROOT / "migrations")

    def test_ui_startup_failure_does_not_prevent_collection_attempt(self) -> None:
        config = SimpleNamespace(gemini_enabled=False, gemini_interval_seconds=1800, gemini_event_trigger=True, poll_seconds=900)
        with patch("market_intelligence.app.start_export_ui", side_effect=ValueError("bad optional UI")), \
             patch("market_intelligence.app.collect_once", side_effect=RuntimeError("collector reached")) as collect:
            with self.assertRaisesRegex(RuntimeError, "collector reached"):
                asyncio.run(run_forever(config, self.store))
        collect.assert_called_once_with(config, self.store)

    def test_auth_headers_cookie_csrf_and_no_token_leak(self) -> None:
        server, _ = self._start()
        status, headers, body = self._request(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertNotIn(TOKEN.encode(), body)

        encoded = urllib.parse.urlencode({"token": "wrong"})
        status, headers, body = self._request(server, "POST", "/login", encoded, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(TOKEN.encode(), body)

        encoded = urllib.parse.urlencode({"token": TOKEN})
        status, headers, _ = self._request(server, "POST", "/login", encoded, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-mi_export_session="))
        self.assertIn("HttpOnly", cookie);self.assertIn("Secure", cookie);self.assertIn("SameSite=Strict", cookie)
        cookie_pair = cookie.split(";", 1)[0]

        status, _, dashboard = self._request(server, "GET", "/", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200);self.assertIn(b"Download ZIP", dashboard);self.assertNotIn(TOKEN.encode(), dashboard)
        status, _, _ = self._request(server, "POST", "/download", urllib.parse.urlencode({"kind": "observations", "range": "all"}),
                                     {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie_pair})
        self.assertEqual(status, 403)

    def test_correct_session_and_bearer_download_mime_disposition(self) -> None:
        server, _ = self._start()
        encoded = urllib.parse.urlencode({"token": TOKEN})
        _, headers, _ = self._request(server, "POST", "/login", encoded, {"Content-Type": "application/x-www-form-urlencoded"})
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        _, _, dashboard = self._request(server, "GET", "/", headers={"Cookie": cookie})
        csrf = re.search(rb'name="csrf" value="([^"]+)"', dashboard).group(1).decode()
        form = urllib.parse.urlencode({"csrf": csrf, "kind": "observations", "range": "all"})
        status, headers, body = self._request(server, "POST", "/download", form,
            {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
        self.assertEqual(status, 200);self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment; filename=", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "observations-all.jsonl"})

        # The first handler releases its one-at-a-time lifecycle lock after the
        # response is flushed and the temporary ZIP is removed.
        time.sleep(0.05)
        form = urllib.parse.urlencode({"kind": "events", "range": "24h"})
        status, _, body = self._request(server, "POST", "/download", form,
            {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:self.assertIn("events-24h.jsonl", archive.namelist())

    def test_insecure_request_and_unauthenticated_download_rejected(self) -> None:
        server, _ = self._start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/")
        response = connection.getresponse();self.assertEqual(response.status, 400);response.read();connection.close()
        form = urllib.parse.urlencode({"kind": "events", "range": "all"})
        status, _, _ = self._request(server, "POST", "/download", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)

    def test_failed_auth_rate_limit(self) -> None:
        limiter = FailureLimiter()
        for _ in range(5):
            self.assertTrue(limiter.allowed("phone"));limiter.fail("phone")
        self.assertFalse(limiter.allowed("phone"))
        limiter.success("phone");self.assertTrue(limiter.allowed("phone"))

    def test_consistent_backup_while_writing_and_manifest_hashes(self) -> None:
        stop = threading.Event()
        def writer() -> None:
            index = 0
            while not stop.is_set() and index < 200:
                now = iso_utc()
                self.store.add_observation(Observation(source="writer", metric=f"m{index}", value_num=float(index),
                    observed_at_utc=now, available_to_system_at_utc=now, source_url="https://example.test"))
                index += 1
        thread = threading.Thread(target=writer);thread.start()
        artifact = create_export(self.store.db_path, self.store.export_dir, ROOT / "migrations", self.settings, "bundle", "all",
                                 datetime(2026, 9, 27, tzinfo=timezone.utc))
        stop.set();thread.join(timeout=5)
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read("manifest.json"))
                for item in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(item["path"])).hexdigest(), item["sha256"])
                copied = self.root / "copied.sqlite3";copied.write_bytes(archive.read("market_intelligence.sqlite3"))
                connection = sqlite3.connect(copied)
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                connection.close()
        finally:
            artifact.cleanup()

    def test_jsonl_fixed_snapshot_ignores_concurrent_append(self) -> None:
        source = self.store.export_dir / "events-2026-09-27-01.jsonl"
        source.write_text(json.dumps({"row": 1}) + "\n", "utf-8")
        original = source.stat().st_size
        artifact = create_export(self.store.db_path, self.store.export_dir, ROOT / "migrations", self.settings, "bundle", "24h",
                                 datetime(2026, 9, 27, 2, tzinfo=timezone.utc))
        with source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"row": 2}) + "\n")
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                rows = archive.read(source.name).splitlines()
                self.assertEqual(len(rows), 1);self.assertGreater(source.stat().st_size, original)
        finally:artifact.cleanup()

    def test_jsonl_incomplete_append_tail_is_ignored(self) -> None:
        source = self.store.export_dir / "events-2026-09-27-01.jsonl"
        source.write_bytes((json.dumps({"row": 1}) + "\n{\"row\":").encode())
        artifact = create_export(self.store.db_path, self.store.export_dir, ROOT / "migrations", self.settings, "bundle", "24h",
                                 datetime(2026, 9, 27, 2, tzinfo=timezone.utc))
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                self.assertEqual(len(archive.read(source.name).splitlines()), 1)
        finally:artifact.cleanup()

    def test_path_safety_and_credential_audit(self) -> None:
        outside = self.root / "outside.jsonl";outside.write_text("{}\n", "utf-8")
        with self.assertRaises(ExportError):_safe_child(self.store.export_dir, outside)
        self.store.db.execute("UPDATE observations SET payload_json=?", (json.dumps({"api_key": "must-not-export"}),))
        self.store.db.commit()
        with self.assertRaises(ExportError):
            create_export(self.store.db_path, self.store.export_dir, ROOT / "migrations", self.settings, "bundle", "all")
        # Export failure does not poison the collector connection.
        now = iso_utc();self.store.add_observation(Observation(source="after-failure", metric="ok", value_num=1,
            observed_at_utc=now, available_to_system_at_utc=now, source_url="https://example.test"))

    def test_large_jsonl_memory_is_bounded(self) -> None:
        source = self.store.export_dir / "events-2026-09-27-01.jsonl"
        row = json.dumps({"payload": "x" * 900}) + "\n"
        with source.open("w", encoding="utf-8") as handle:
            for _ in range(12000):handle.write(row)
        tracemalloc.start()
        artifact = create_export(self.store.db_path, self.store.export_dir, ROOT / "migrations",
            replace(self.settings, max_input_bytes=32 * 1024 * 1024, max_zip_bytes=32 * 1024 * 1024),
            "bundle", "24h", datetime(2026, 9, 27, 2, tzinfo=timezone.utc))
        _, peak = tracemalloc.get_traced_memory();tracemalloc.stop()
        try:
            self.assertLess(peak, 16 * 1024 * 1024)
            with zipfile.ZipFile(artifact.path) as archive:self.assertIsNone(archive.testzip())
        finally:artifact.cleanup()


if __name__ == "__main__":
    unittest.main()
