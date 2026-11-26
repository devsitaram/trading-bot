from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dashboard import dashboard_payload
from trading_agent.autonomous_trader import AutonomousTrader
from trading_agent.broker import UnconfiguredTerminal
from trading_agent.instrument_scanner import InstrumentScanner
from trading_agent.models import (
    AccountMode,
    AccountState,
    Candle,
    InstrumentSpec,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    Quote,
    RiskConfig,
    ScanResult,
    Side,
    Signal,
    StrategyStats,
    Timeframe,
)
from trading_agent.opportunity_ranker import rank_opportunities
from trading_agent.risk_manager import RiskManager, RiskRejected
from trading_agent.strategy import signal_for, walk_forward_backtest


NOW = datetime(2026, 10, 26, 5, 45, tzinfo=timezone.utc)


def candles_for(direction: int, count: int = 150) -> list[Candle]:
    result = []
    price = 100.0
    for index in range(count):
        change = direction * (0.12 if index % 7 == 0 else 0.04)
        open_price = price
        close = price + change
        result.append(
            Candle(
                NOW - timedelta(minutes=count - index),
                open_price,
                max(open_price, close) + 0.015,
                min(open_price, close) - 0.015,
                close,
            )
        )
        price = close
    return result


class MockTerminal:
    def __init__(self) -> None:
        self.history_calls: list[datetime | None] = []
        self.specs = {
            "GOOD": InstrumentSpec(
                "GOOD", "forex", True, True, 0.01, 10, 0.01,
                0.01, 1.0, 100_000, 100, 3.0, account_currency="USD",
                swap_long_per_lot=0.0, swap_short_per_lot=0.0,
                short_allowed=True,
            ),
            "BETTER": InstrumentSpec(
                "BETTER", "forex", True, True, 0.01, 10, 0.01,
                0.01, 1.0, 100_000, 100, 3.0, account_currency="USD",
                swap_long_per_lot=0.0, swap_short_per_lot=0.0,
                short_allowed=True,
            ),
            "CLOSED": InstrumentSpec(
                "CLOSED", "index", True, False, 0.1, 10, 0.1,
                0.1, 1.0, 1, 100, account_currency="USD",
                commission_per_lot=0.0,
                swap_long_per_lot=0.0,
                swap_short_per_lot=0.0,
            ),
        }
        self.histories = {
            ("GOOD", tf): candles_for(1, 150) for tf in Timeframe
        }
        self.histories.update(
            {("BETTER", tf): candles_for(1, 150) for tf in Timeframe}
        )
        self.histories.update(
            {("CLOSED", tf): candles_for(1, 150) for tf in Timeframe}
        )

    def account_state(self) -> AccountState:
        return AccountState(
            AccountMode.DEMO, "demo-123", "USD", 10_000, 10_000, 8_000,
            0, 10_000, NOW,
        )

    def discover_symbols(self) -> list[str]:
        return list(self.specs)

    def instrument_spec(self, symbol: str) -> InstrumentSpec:
        return self.specs[symbol]

    def quote(self, symbol: str) -> Quote:
        ask = 105.002 if symbol == "BETTER" else 105.01
        return Quote(105.0, ask, NOW, estimated_slippage_bps=1.0)

    def candles(self, symbol, timeframe, *, since, limit):
        self.history_calls.append(since)
        values = self.histories[(symbol, timeframe)]
        if since is not None:
            values = [item for item in values if item.timestamp >= since]
        return values[-limit:]

    def portfolio(self) -> PortfolioSnapshot:
        return PortfolioSnapshot()


class ScannerTests(unittest.TestCase):
    def test_discovers_and_excludes_closed_instrument(self) -> None:
        scanner = InstrumentScanner(
            MockTerminal(), minimum_candles=60, max_spread_to_atr=2.0
        )
        results = {item.symbol: item for item in scanner.scan_once(NOW)}
        self.assertTrue(results["GOOD"].eligible)
        self.assertTrue(results["BETTER"].eligible)
        self.assertLess(results["BETTER"].costs_bps, results["GOOD"].costs_bps)
        self.assertFalse(results["CLOSED"].eligible)
        self.assertIn("market session closed", results["CLOSED"].reasons)

    def test_excludes_missing_contract_costs_stale_quotes_and_history_errors(self) -> None:
        terminal = MockTerminal()
        terminal.specs["UNCOSTED"] = InstrumentSpec(
            "UNCOSTED", "crypto", True, True, 0.01, 10, 0.01,
            0.01, 1, 1, 10, account_currency="USD",
        )
        terminal.specs["BROKEN"] = InstrumentSpec(
            "BROKEN", "stock", True, True, 0.01, 10, 0.01,
            0.01, 1, 1, 10, commission_per_lot=0,
            swap_long_per_lot=0, swap_short_per_lot=0,
            account_currency="USD",
        )

        class DataQualityTerminal(MockTerminal):
            def quote(self, symbol):
                if symbol == "STALE":
                    return Quote(
                        100, 101, NOW - timedelta(hours=1),
                        estimated_slippage_bps=1.0,
                    )
                return super().quote(symbol)

            def candles(self, symbol, timeframe, *, since, limit):
                if symbol == "BROKEN":
                    raise ValueError("historical data unavailable")
                return super().candles(
                    symbol, timeframe, since=since, limit=limit
                )

        quality_terminal = DataQualityTerminal()
        quality_terminal.specs.update(terminal.specs)
        quality_terminal.specs["STALE"] = InstrumentSpec(
            "STALE", "forex", True, True, 0.01, 10, 0.01, 0.01, 1,
            100_000, 100, commission_per_lot=0, swap_long_per_lot=0,
            swap_short_per_lot=0, account_currency="USD",
        )
        results = {
            item.symbol: item
            for item in InstrumentScanner(
                quality_terminal, max_spread_to_atr=2.0
            ).scan_once(NOW)
        }
        self.assertFalse(results["UNCOSTED"].eligible)
        self.assertIn(
            "invalid or incomplete contract specifications",
            results["UNCOSTED"].reasons,
        )
        self.assertFalse(results["STALE"].eligible)
        self.assertIn("stale quote", results["STALE"].reasons[0])
        self.assertFalse(results["BROKEN"].eligible)
        self.assertIn("history unavailable", results["BROKEN"].reasons[0])

    def test_incremental_candle_fetch_uses_last_timestamp(self) -> None:
        terminal = MockTerminal()
        scanner = InstrumentScanner(terminal, max_spread_to_atr=2.0)
        scanner.scan_once(NOW)
        first_call_count = len(terminal.history_calls)
        scanner.scan_once(NOW + timedelta(seconds=1))
        self.assertIsNone(terminal.history_calls[0])
        self.assertGreater(first_call_count, 0)
        self.assertIsNotNone(terminal.history_calls[first_call_count])


class StrategyTests(unittest.TestCase):
    def test_strategy_can_generate_both_directions_when_short_is_allowed(self) -> None:
        up = candles_for(1)
        down = candles_for(-1)
        buy = signal_for(
            "UP", Timeframe.M5, up, short_allowed=True, now=NOW,
            max_age_seconds=10_000,
        )
        sell = signal_for(
            "DOWN", Timeframe.M5, down, short_allowed=True, now=NOW,
            max_age_seconds=10_000,
        )
        self.assertEqual(buy.side if buy else None, Side.BUY)
        self.assertEqual(sell.side if sell else None, Side.SELL)
        self.assertIsNone(
            signal_for(
                "DOWN", Timeframe.M5, down, short_allowed=False, now=NOW,
                max_age_seconds=10_000,
            )
        )

    def test_backtest_requires_minimum_oos_samples(self) -> None:
        result = walk_forward_backtest(
            "X", Timeframe.M1, candles_for(1), costs_bps=2,
            short_allowed=True, minimum_oos_trades=1000,
        )
        self.assertFalse(result.validated)
        self.assertLessEqual(result.out_of_sample_trades, 1000)


class RankerTests(unittest.TestCase):
    def test_score_is_independent_of_instrument_price_scale(self) -> None:
        # Same edge and same stop in bps; only the price level differs 1000x.
        scans, stats, specs = [], {}, {}
        for symbol, price in (("CHEAP", 1.0), ("PRICEY", 1000.0)):
            signal = Signal(
                symbol, Side.BUY, Timeframe.M5, "v", 0.9,
                price * 0.002, "test", NOW,
            )
            scans.append(ScanResult(
                symbol, True, quote=Quote(price, price, NOW),
                signals=(signal,), costs_bps=1.0,
            ))
            stats[(symbol, Timeframe.M5)] = StrategyStats(
                symbol, Timeframe.M5, "v", 50, 10.0, 0.6, 0.0, True,
            )
            specs[symbol] = MockTerminal().specs["GOOD"]
        ranked = rank_opportunities(scans, stats, specs, RiskConfig())
        self.assertEqual(len(ranked), 2)
        self.assertAlmostEqual(ranked[0].score, ranked[1].score)


class RiskTests(unittest.TestCase):
    def test_sizing_uses_contract_tick_value_and_risk_budget(self) -> None:
        terminal = MockTerminal()
        scanner = InstrumentScanner(terminal, max_spread_to_atr=2.0)
        scan = next(item for item in scanner.scan_once(NOW) if item.symbol == "GOOD")
        signal = signal_for(
            "GOOD", Timeframe.M5, terminal.histories[("GOOD", Timeframe.M5)],
            short_allowed=True, now=NOW, max_age_seconds=10_000,
        )
        self.assertIsNotNone(signal)
        from trading_agent.models import RankedOpportunity

        opportunity = RankedOpportunity(
            "GOOD", Side.BUY, 1.0, 20, 2.0, signal,
            ("out of sample",),
        )
        config = RiskConfig(
            emergency_stop=False, max_spread_to_atr=2.0,
            min_free_margin_fraction=0.1,
            correlation_groups={"GOOD": "fx"},
        )
        proposal = RiskManager(config).propose(
            opportunity, terminal.specs["GOOD"], terminal.account_state(),
            terminal.portfolio(), quote=terminal.quote("GOOD"),
            spread_bps=scan.spread_bps or 0,
            slippage_bps=scan.slippage_bps or 0,
            spread_to_atr=scan.spread_to_atr or 0, now=NOW,
        )
        self.assertLessEqual(proposal.risk_amount, 25.0)
        self.assertGreater(proposal.risk_amount, 24.0)
        self.assertGreaterEqual(proposal.volume, terminal.specs["GOOD"].min_volume)
        self.assertAlmostEqual(proposal.stop_loss, 105.01 - signal.stop_distance)

    def test_counts_pending_orders_and_rejects_duplicate_symbol(self) -> None:
        terminal = MockTerminal()
        signal = signal_for(
            "GOOD", Timeframe.M5, terminal.histories[("GOOD", Timeframe.M5)],
            short_allowed=True, now=NOW, max_age_seconds=10_000,
        )
        from trading_agent.models import RankedOpportunity

        opportunity = RankedOpportunity(
            "GOOD", Side.BUY, 1, 20, 2, signal, (),
        )
        risk = RiskManager(
            RiskConfig(
                emergency_stop=False,
                min_free_margin_fraction=0.1,
                correlation_groups={"GOOD": "fx"},
            )
        )
        portfolio = PortfolioSnapshot(
            pending_orders=(PendingOrder("GOOD", Side.BUY, 0.01, 90, 25),)
        )
        with self.assertRaisesRegex(RiskRejected, "already exists"):
            risk.check(
                opportunity, terminal.specs["GOOD"], terminal.account_state(),
                portfolio, spread_bps=1.0, slippage_bps=1.0,
                spread_to_atr=0.01, now=NOW,
            )

    def test_proposed_risk_cannot_push_account_over_total_limit(self) -> None:
        terminal = MockTerminal()
        signal = signal_for(
            "GOOD", Timeframe.M5, terminal.histories[("GOOD", Timeframe.M5)],
            short_allowed=True, now=NOW, max_age_seconds=10_000,
        )
        from trading_agent.models import RankedOpportunity

        opportunity = RankedOpportunity("GOOD", Side.BUY, 1, 20, 0.5, signal, ())
        config = RiskConfig(
            emergency_stop=False,
            min_free_margin_fraction=0.1,
            correlation_groups={"GOOD": "fx", "OTHER": "fx"},
        )
        portfolio = PortfolioSnapshot(
            positions=(Position("OTHER", Side.BUY, 0.1, 90, None, 80),)
        )
        with self.assertRaisesRegex(RiskRejected, "exceed account-wide"):
            RiskManager(config).propose(
                opportunity,
                terminal.specs["GOOD"],
                terminal.account_state(),
                portfolio,
                quote=terminal.quote("GOOD"),
                spread_bps=1.0,
                slippage_bps=1.0,
                spread_to_atr=0.01,
                now=NOW,
            )

    def test_live_mode_and_emergency_stop_are_fail_closed(self) -> None:
        terminal = MockTerminal()
        from trading_agent.models import RankedOpportunity

        signal = signal_for(
            "GOOD", Timeframe.M5, terminal.histories[("GOOD", Timeframe.M5)],
            short_allowed=True, now=NOW, max_age_seconds=10_000,
        )
        opportunity = RankedOpportunity("GOOD", Side.BUY, 1, 20, 2, signal, ())
        risk = RiskManager(RiskConfig(emergency_stop=True))
        with self.assertRaisesRegex(RiskRejected, "emergency stop"):
            risk.check(
                opportunity, terminal.specs["GOOD"], terminal.account_state(),
                terminal.portfolio(), spread_bps=1.0, slippage_bps=1.0,
                spread_to_atr=0, now=NOW,
            )


class WorkflowTests(unittest.TestCase):
    def test_full_mock_workflow_produces_proposal_but_never_submits(self) -> None:
        terminal = MockTerminal()
        config = RiskConfig(
            emergency_stop=False,
            max_spread_to_atr=0.2,
            max_spread_bps=20,
            min_free_margin_fraction=0.1,
            min_expected_net_bps=1,
            min_out_of_sample_trades=30,
            correlation_groups={"GOOD": "fx", "BETTER": "fx"},
        )
        trader = AutonomousTrader(terminal, risk_config=config)
        self.assertTrue(trader.startup())
        decision = trader.cycle(NOW)
        self.assertEqual(decision.action, "HOLD")
        self.assertIsNotNone(decision.selected)
        self.assertIsNotNone(decision.proposal)
        self.assertIn("execution disabled", decision.reason)
        self.assertEqual(decision.selected.symbol, "BETTER")
        payload = dashboard_payload(
            trader.scanner.scan_once(NOW),
            terminal.account_state(),
            portfolio=terminal.portfolio(),
            ranked=decision.ranked,
            rejected=decision.rejected,
            selected_symbol=decision.selected.symbol,
            proposal=decision.proposal,
            emergency_stop=False,
        )
        self.assertEqual(payload["mode"], "DEMO")
        self.assertEqual(payload["ranked_candidates"][0]["symbol"], "BETTER")
        self.assertFalse(payload["orders_submitted"])
        self.assertTrue(payload["estimated_opportunities_are_not_realized_profit"])

    def test_no_validated_edge_selects_hold(self) -> None:
        terminal = MockTerminal()
        terminal.histories = {
            key: candles_for(0, 150) for key in terminal.histories
        }
        config = RiskConfig(
            emergency_stop=False,
            max_spread_to_atr=0.2,
            min_free_margin_fraction=0.1,
            min_out_of_sample_trades=30,
        )
        trader = AutonomousTrader(terminal, risk_config=config)
        self.assertTrue(trader.startup())
        decision = trader.cycle(NOW)
        self.assertEqual(decision.action, "HOLD")
        self.assertIsNone(decision.selected)
        self.assertIsNone(decision.proposal)
        self.assertIn("no validated", decision.reason)

    def test_default_terminal_cannot_access_account_or_trade(self) -> None:
        trader = AutonomousTrader(UnconfiguredTerminal())
        self.assertFalse(trader.startup())
        decision = trader.cycle(NOW)
        self.assertEqual(decision.action, "HOLD")
        self.assertIsNone(decision.proposal)
        self.assertIn("disabled", decision.reason.lower())

    def test_non_demo_account_does_not_pass_startup(self) -> None:
        class LiveTerminal(MockTerminal):
            def account_state(self):
                account = super().account_state()
                return AccountState(
                    AccountMode.LIVE, account.account_id, account.currency,
                    account.equity, account.balance, account.free_margin,
                    account.daily_pnl, account.peak_equity, account.timestamp,
                )

        trader = AutonomousTrader(LiveTerminal())
        self.assertFalse(trader.startup())
        self.assertIn("DEMO", trader.startup_reason)

    def test_account_switch_after_startup_blocks_scanning(self) -> None:
        class SwitchedTerminal(MockTerminal):
            def __init__(self):
                super().__init__()
                self.switched = False

            def account_state(self):
                account = super().account_state()
                if self.switched:
                    return AccountState(
                        AccountMode.DEMO, "different-demo", account.currency,
                        account.equity, account.balance, account.free_margin,
                        account.daily_pnl, account.peak_equity, account.timestamp,
                    )
                return account

        terminal = SwitchedTerminal()
        trader = AutonomousTrader(terminal)
        self.assertTrue(trader.startup())
        terminal.switched = True
        decision = trader.cycle(NOW)
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("identity", decision.reason)


if __name__ == "__main__":
    unittest.main()
