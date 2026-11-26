from __future__ import annotations

import unittest

from mt5_status import parse_logs


def log(*lines: tuple[str, str, str]) -> str:
    """MT5 log text: code, level, time, source, message separated by tabs."""
    return "\n".join(f"XX\t0\t{time}\t{source}\t{message}" for time, source, message in lines)


START = ("10:00:00.000", "Terminal", "MetaTrader 5 x64 build 6275 started for MetaQuotes Ltd.")


class Mt5StatusTests(unittest.TestCase):
    def test_successful_login_and_running_bridge(self) -> None:
        status = parse_logs(
            log(START,
                ("10:00:05.000", "Experts", "expert JustMarketsBridge (XAUUSD,M1) loaded successfully"),
                ("10:00:09.000", "Network", "'1300066989': authorized on JustMarkets-Demo through Access Point #1 (ping: 120.4 ms)")),
            log(("10:00:10.000", "JustMarketsBridge (XAUUSD,M1)", "JustMarketsBridge linked XAUUSD on DEMO account 1300066989 (JustMarkets-Demo).")),
        )
        self.assertEqual(status["login"], {"account": 1300066989, "server": "JustMarkets-Demo"})
        self.assertEqual(status["bridge"], "running")
        self.assertIsNone(status["webrequest_error"])

    def test_wrong_password_unknown_server_and_blocked_webrequest(self) -> None:
        status = parse_logs(
            log(START, ("10:00:09.000", "Network", "'1300066989': authorization on JustMarkets-Demo failed (Invalid account)")),
            log(("10:00:12.000", "JustMarketsBridge (XAUUSD,M1)", "Cannot reach http://127.0.0.1:8765 (error 4014). Start the dashboard")),
        )
        self.assertIsNone(status["login"])
        self.assertIn("Invalid account", status["login_error"])
        self.assertIn("4014", status["webrequest_error"])
        no_server = parse_logs(log(START, ("10:00:09.000", "Network", "'1300066989': no connection to JustMarkets-Demo")), "")
        self.assertIn("no connection", no_server["login_error"])

    def test_only_the_latest_mt5_session_counts(self) -> None:
        status = parse_logs(
            log(("09:00:00.000", "Network", "'1100244978': authorization on MetaQuotes-Demo failed (Invalid account)"),
                START, ("10:00:05.000", "Experts", "expert JustMarketsBridge (XAUUSD,M1) loaded successfully")),
            log(("09:30:00.000", "JustMarketsBridge (XAUUSD,M1)", "Alert: JustMarketsBridge: this is a REAL money account.")),
        )
        self.assertIsNone(status["login_error"])  # the failed login belongs to the earlier session
        self.assertIsNone(status["bridge_error"])  # so does the old alert
        self.assertEqual(status["bridge"], "running")
