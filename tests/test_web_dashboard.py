from __future__ import annotations

import json
import tempfile
import threading
import io
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from main import LocalDashboardServer, NewsCalendar, dashboard_status
from paper_ledger import PaperLedger


class PaperLedgerTests(unittest.TestCase):
    def test_saved_trades_survive_reopening_and_a_stale_tab_cannot_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/paper.sqlite3"
            ledger = PaperLedger(path)
            self.assertEqual(ledger.read(), {"version": 0, "state": None, "events": []})
            self.assertEqual(ledger.save(0, {"trades": [1]}), 1)
            self.assertIsNone(ledger.save(0, {"trades": []}))
            self.assertEqual(ledger.save(1, {"trades": [1, 2]}), 2)
            self.assertIsNone(ledger.save(1, {"trades": []}))
            ledger.add_event({"kind": "open", "time": 5})
            reopened = PaperLedger(path).read()
            self.assertEqual(reopened["version"], 2)
            self.assertEqual(reopened["state"], {"trades": [1, 2]})
            self.assertEqual(reopened["events"], [{"kind": "open", "time": 5}])


class LocalDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch("main.PaperLedger", lambda _path: PaperLedger(f"{folder.name}/paper.sqlite3"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_news_calendar_keeps_only_high_impact_events_and_caches(self) -> None:
        calendar = json.dumps([
            {"title": "Advance GDP q/q", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "High"},
            {"title": "Pending Home Sales", "country": "USD", "date": "2026-10-29T10:00:00-04:00", "impact": "Medium"},
        ]).encode("utf-8")
        with mock.patch("main.urlopen", return_value=io.BytesIO(calendar)) as fetch:
            news = NewsCalendar()
            self.assertEqual(
                news.high_impact(),
                [{"time": "2026-10-29T08:30:00-04:00", "title": "Advance GDP q/q", "currency": "USD"}],
            )
            news.high_impact()
            self.assertEqual(fetch.call_count, 1)

    def test_news_calendar_failure_before_first_load_raises(self) -> None:
        with mock.patch("main.urlopen", side_effect=OSError("HTTP Error 429")):
            with self.assertRaises(OSError):
                NewsCalendar().high_impact()

    def test_status_is_explicitly_disconnected_and_execution_disabled(self) -> None:
        status = dashboard_status()
        self.assertFalse(status["broker_connected"])
        self.assertEqual(status["account_mode"], "UNKNOWN")
        self.assertFalse(status["account_verified"])
        self.assertEqual(status["market_data"], "unavailable")
        self.assertFalse(status["execution_enabled"])
        self.assertEqual(status["instruments"], [])

    def test_local_quote_bridge_updates_status_without_enabling_trading(self) -> None:
        server = LocalDashboardServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(
                f"{base_url}/api/quotes",
                data=json.dumps(
                    {
                        "symbol": "EURUSD.a",
                        "bid": 1.1,
                        "ask": 1.1002,
                        "timestamp_ms": int(time.time() * 1000),
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 204)

            with urlopen(f"{base_url}/api/status", timeout=2) as response:
                status = json.load(response)
            self.assertTrue(status["broker_connected"])
            self.assertEqual(status["market_data"], "available")
            self.assertEqual(status["instruments"][0]["symbol"], "EURUSD.a")
            self.assertEqual(status["instruments"][0]["bid"], 1.1)
            self.assertEqual(status["instruments"][0]["ask"], 1.1002)
            self.assertEqual(status["account_mode"], "UNKNOWN")
            self.assertFalse(status["execution_enabled"])
            self.assertFalse(status["orders_submitted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_quote_bridge_rejects_invalid_prices(self) -> None:
        server = LocalDashboardServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(
                f"{base_url}/api/quotes",
                data=json.dumps(
                    {
                        "symbol": "EURUSD",
                        "bid": 1.2,
                        "ask": 1.1,
                        "timestamp_ms": int(time.time() * 1000),
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_quote_bridge_does_not_publish_stale_terminal_ticks(self) -> None:
        server = LocalDashboardServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(
                f"{base_url}/api/quotes",
                data=json.dumps(
                    {
                        "symbol": "EURUSD",
                        "bid": 1.1,
                        "ask": 1.1002,
                        "timestamp_ms": int(time.time() * 1000) - 60_000,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 202)

            with urlopen(f"{base_url}/api/status", timeout=2) as response:
                status = json.load(response)
            self.assertFalse(status["broker_connected"])
            self.assertEqual(status["instruments"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_local_server_serves_dashboard_and_status_api(self) -> None:
        server = LocalDashboardServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base_url, timeout=2) as response:
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("Market overview", html)
                self.assertIn("Broker not connected", html)
                self.assertIn("Live price chart", html)
                self.assertIn("refreshed every 5 seconds", html)
                self.assertIn("chart-path", html)
            with urlopen(f"{base_url}/chart", timeout=2) as response:
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("Paper bot", html)
            with urlopen(f"{base_url}/chart", timeout=2) as response:
                html = response.read().decode("utf-8")
                self.assertIn("JustMarkets account", html)
                self.assertIn('value="demo" checked', html)  # trading starts on demo only; real must be chosen
                self.assertIn("Account · not linked", html)
            with urlopen(f"{base_url}/trade", timeout=2) as response:
                self.assertIn("Trade terminal", response.read().decode("utf-8"))
            with urlopen(f"{base_url}/practice", timeout=2) as response:
                self.assertIn("fake money", response.read().decode("utf-8"))
            with urlopen(f"{base_url}/api/paper", timeout=2) as response:
                self.assertEqual(json.load(response)["version"], 0)
            foreign = Request(f"{base_url}/api/paper/state", data=b'{"version":0,"state":{}}', headers={"Content-Type": "application/json", "Origin": "https://evil.example"}, method="POST")
            with self.assertRaises(HTTPError) as error:
                urlopen(foreign, timeout=2)
            self.assertEqual(error.exception.code, 403)
            with urlopen(f"{base_url}/rules.json", timeout=2) as response:
                rules = json.load(response)["rules"]
                self.assertEqual(response.status, 200)
                self.assertIn("stop_atr", rules)
                self.assertEqual(len(rules), 19)
            with urlopen(f"{base_url}/api/status", timeout=2) as response:
                status = json.load(response)
                self.assertEqual(response.status, 200)
                self.assertFalse(status["broker_connected"])
                self.assertFalse(status["orders_submitted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
