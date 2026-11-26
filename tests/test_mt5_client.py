from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from mt5bot.mt5_client import MT5Client, MT5Error


def fake_mt5(**overrides):
    mt5 = mock.Mock(ACCOUNT_TRADE_MODE_DEMO=0)
    mt5.last_error.return_value = (-6, "Authorization failed")
    mt5.account_info.return_value = SimpleNamespace(
        login=1, server="JustMarkets-Demo", trade_mode=0, currency="USD", balance=215.59, equity=215.59, leverage=500, trade_expert=True)
    mt5.configure_mock(**overrides)
    return mt5


class MT5ClientTests(unittest.TestCase):
    def test_connect_retries_with_growing_waits_then_succeeds(self) -> None:
        mt5, waits = fake_mt5(), []
        mt5.initialize.side_effect = [False, False, True]
        MT5Client(mt5, waits.append).connect(1, "secret", "S", 5, 2)
        self.assertEqual(waits, [2, 4])

    def test_failed_connect_reports_the_error_without_the_password(self) -> None:
        mt5 = fake_mt5()
        mt5.initialize.return_value = False
        with self.assertRaises(MT5Error) as error:
            MT5Client(mt5, lambda _: None).connect(1, "secret", "S", 2, 1)
        self.assertIn("Authorization failed", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_account_reports_demo_from_trade_mode(self) -> None:
        self.assertTrue(MT5Client(fake_mt5()).account()["demo"])
        mt5 = fake_mt5()
        mt5.account_info.return_value.trade_mode = 2
        self.assertFalse(MT5Client(mt5).account()["demo"])

    def test_unknown_symbol_names_the_fix(self) -> None:
        mt5 = fake_mt5()
        mt5.symbol_info.return_value = None
        with self.assertRaisesRegex(MT5Error, "Market Watch"):
            MT5Client(mt5).symbol("BTCUSD")


if __name__ == "__main__":
    unittest.main()
