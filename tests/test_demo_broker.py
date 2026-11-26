from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from demo_broker import BrokerError, DemoBroker, order_line
from main import LocalDashboardServer, find_metatrader


def account(mode: str = "DEMO", **extra: object) -> dict[str, object]:
    return {
        "login": 15748669, "server": "JustMarkets-Demo", "company": "JustMarkets", "mode": mode, "currency": "USD",
        "balance": 206.49, "equity": 206.49, "symbol": "XAUUSD", "bid": 4045.19, "ask": 4045.45, "positions": [],
        "today_pl": -3.5, "today_trades": 2, "wins": 4, "closed": 9,
        **extra,
    }


OPEN = {"action": "open", "side": "BUY", "volume": 0.01, "sl_distance": 3.0, "tp_distance": 6.0, "client": "tab00001"}


class DemoBrokerTests(unittest.TestCase):
    def test_no_orders_until_linked_and_real_money_needs_the_page_switch(self) -> None:
        broker = DemoBroker()
        broker.status("tab00001")
        with self.assertRaises(BrokerError):
            broker.queue_order(OPEN)  # MT5 not linked yet
        broker.report_account(account(mode="REAL"))
        with self.assertRaisesRegex(BrokerError, "REAL account"):
            broker.queue_order(OPEN)  # page left on demo only
        with self.assertRaisesRegex(BrokerError, "REAL account"):
            broker.queue_order({**OPEN, "allow_real": "yes"})  # only a literal true counts
        self.assertEqual(broker.queue_order({**OPEN, "allow_real": True})["status"], "pending")
        broker.report_account(account(mode="CONTEST"))
        with self.assertRaisesRegex(BrokerError, "CONTEST account"):
            broker.queue_order({**OPEN, "allow_real": True})
        broker.report_account(account())
        self.assertEqual(broker.queue_order(OPEN)["status"], "pending")  # demo needs no switch

    def test_only_the_controlling_tab_may_send_orders(self) -> None:
        broker = DemoBroker()
        broker.report_account(account())
        self.assertTrue(broker.status("tab00001")["controller"])
        self.assertFalse(broker.status("tab00002")["controller"])  # a second tab only watches
        with self.assertRaisesRegex(BrokerError, "controls the account"):
            broker.queue_order({**OPEN, "client": "tab00002"})
        with self.assertRaisesRegex(BrokerError, "controls the account"):
            broker.queue_order({k: v for k, v in OPEN.items() if k != "client"})  # older pages send no client
        self.assertEqual(broker.queue_order(OPEN)["status"], "pending")

    def test_order_is_handed_to_mt5_once_and_result_is_recorded(self) -> None:
        broker = DemoBroker()
        broker.status("tab00001")
        broker.report_account(account())
        order = broker.queue_order(OPEN)
        sent = broker.take_orders()
        self.assertEqual(order_line(sent[0]), f"{order['id']}|open|BUY|0.01|0.0|0.0|0|3.0|6.0")
        self.assertEqual(broker.take_orders(), [])
        broker.order_result({"id": order["id"], "ok": True, "ticket": 987, "message": "done"})
        self.assertEqual(broker.status()["orders"][-1]["status"], "done")
        self.assertEqual(broker.status()["orders"][-1]["ticket"], 987)

    def test_rejects_bad_orders_and_bars(self) -> None:
        broker = DemoBroker()
        broker.status("tab00001")
        broker.report_account(account())
        for bad in ({**OPEN, "volume": 0.015}, {**OPEN, "volume": 5}, {**OPEN, "side": "HOLD"},
                    {"action": "modify", "ticket": 1, "sl": -1, "tp": 4050}, {"action": "explode"}):
            with self.assertRaises(BrokerError):
                broker.queue_order(bad)
        with self.assertRaises(BrokerError):
            broker.set_bars({"tf": "1m", "bars": [[1, 10, 9, 8, 9.5]]})  # high below open
        broker.set_bars({"tf": "1m", "bars": [[1793230000, 4045.0, 4046.0, 4044.0, 4045.5]]})
        self.assertEqual(broker.bars("1m"), [[1793230000, 4045.0, 4046.0, 4044.0, 4045.5]])

    def test_http_blocks_other_websites_and_serves_the_page(self) -> None:
        server = LocalDashboardServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, body: dict[str, object], origin: str | None = None) -> int:
            headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
            try:
                with urlopen(Request(f"{base}{path}", json.dumps(body).encode(), headers, method="POST"), timeout=2) as r:
                    return r.status
            except HTTPError as error:
                return error.code

        try:
            self.assertEqual(post("/api/broker/account", account()), 204)  # the MT5 add-on sends no Origin
            urlopen(f"{base}/api/broker?client=tab00001", timeout=2).close()  # this tab takes control
            self.assertEqual(post("/api/broker/orders", OPEN, origin="https://evil.example"), 403)
            self.assertEqual(post("/api/broker/orders", OPEN, origin=base.replace(str(server.server_port), "8765")), 201)
            with urlopen(f"{base}/api/broker/orders", timeout=2) as response:
                self.assertTrue(response.read().decode().endswith("|open|BUY|0.01|0.0|0.0|0|3.0|6.0"))
            with urlopen(f"{base}/api/broker", timeout=2) as response:
                status = json.load(response)
                self.assertTrue(status["linked"])
                self.assertEqual(status["account"]["mode"], "DEMO")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class MetaTraderCheckTests(unittest.TestCase):
    def test_reports_missing_and_installed_app_with_version(self) -> None:
        import plistlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "MetaTrader 5.app"
            missing = find_metatrader((app,))
            self.assertFalse(missing["installed"])
            self.assertIn("metatrader5.com", missing["download"])
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "5.0.6275"}))
            found = find_metatrader((app,))
            self.assertTrue(found["installed"])
            self.assertEqual(found["version"], "5.0.6275")
