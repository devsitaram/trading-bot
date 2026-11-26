from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite

from .broker import ReadOnlyTerminal
from .instrument_scanner import InstrumentScanner, TIMEFRAMES
from .models import (
    AccountMode,
    OrderProposal,
    RankedOpportunity,
    RiskConfig,
    StrategyStats,
)
from .opportunity_ranker import rank_opportunities
from .risk_manager import RiskManager, RiskRejected
from .storage import EventStore
from .strategy import signal_for, walk_forward_backtest


@dataclass(frozen=True)
class TradingDecision:
    selected: RankedOpportunity | None
    proposal: OrderProposal | None
    action: str
    reason: str
    rejected: tuple[tuple[str, str], ...]
    ranked: tuple[RankedOpportunity, ...] = ()


class AutonomousTrader:
    """Scanner/ranker/paper-proposal workflow with no order-sending capability."""

    def __init__(
        self,
        terminal: ReadOnlyTerminal,
        *,
        risk_config: RiskConfig | None = None,
        store: EventStore | None = None,
    ) -> None:
        self.terminal = terminal
        self.risk_config = risk_config or RiskConfig()
        self.scanner = InstrumentScanner(
            terminal,
            max_spread_to_atr=self.risk_config.max_spread_to_atr,
            max_spread_bps=self.risk_config.max_spread_bps,
            max_slippage_bps=self.risk_config.max_slippage_bps,
        )
        self.risk = RiskManager(self.risk_config)
        self.store = store
        self.ready = False
        self.startup_reason = "not reconciled"
        self._account_id: str | None = None
        self._stats_cache: dict[
            tuple[str, object], tuple[datetime, StrategyStats]
        ] = {}

    def startup(self) -> bool:
        try:
            account = self.terminal.account_state()
            portfolio = self.terminal.portfolio()
            symbols = self.terminal.discover_symbols()
        except (RuntimeError, OSError, ValueError, TimeoutError, ConnectionError) as exc:
            self.startup_reason = f"terminal health/reconciliation failed: {exc}"
            self.ready = False
            return False
        if account.mode is not AccountMode.DEMO or not account.account_id:
            self.startup_reason = "account identity is not verified DEMO"
            self.ready = False
            return False
        if account.timestamp.tzinfo is None:
            self.startup_reason = "account timestamp is not timezone-aware"
            self.ready = False
            return False
        # Querying the live portfolio here intentionally reconciles positions,
        # pending orders, and recent deals before analysis; never infer from URL.
        del portfolio, symbols
        self._account_id = account.account_id
        self.ready = True
        self.startup_reason = "authenticated DEMO account reconciled; execution remains disabled"
        return True

    def cycle(self, now: datetime | None = None) -> TradingDecision:
        now = now or datetime.now(timezone.utc)
        if not self.ready:
            return TradingDecision(None, None, "HOLD", self.startup_reason, ())
        try:
            account = self.terminal.account_state()
            portfolio = self.terminal.portfolio()
        except (RuntimeError, OSError, ValueError, TimeoutError, ConnectionError) as exc:
            self.ready = False
            return TradingDecision(
                None, None, "HOLD", f"health check failed; no trade: {exc}", ()
            )
        account_age = (
            (now - account.timestamp).total_seconds()
            if account.timestamp.tzinfo
            else float("inf")
        )
        if (
            account.mode is not AccountMode.DEMO
            or account.account_id != self._account_id
            or account_age < 0
            or account_age > 30
        ):
            self.ready = False
            return TradingDecision(
                None,
                None,
                "HOLD",
                "account identity/mode changed or account state is stale",
                (),
            )
        try:
            scans = self.scanner.scan_once(now)
        except (RuntimeError, OSError, ValueError, TimeoutError, ConnectionError) as exc:
            self.ready = False
            return TradingDecision(
                None, None, "HOLD", f"market scan failed; no trade: {exc}", ()
            )

        stats = {}
        for scan in scans:
            if not scan.eligible or scan.costs_bps is None:
                continue
            spec = self.scanner.specs[scan.symbol]
            for timeframe in TIMEFRAMES:
                candles = list(self.scanner.buffers[(scan.symbol, timeframe)])
                if not candles:
                    continue
                cache_key = (scan.symbol, timeframe)
                latest_timestamp = candles[-1].timestamp
                cached = self._stats_cache.get(cache_key)
                if cached is None or cached[0] != latest_timestamp:
                    cached_stats = walk_forward_backtest(
                        scan.symbol,
                        timeframe,
                        candles,
                        costs_bps=scan.costs_bps,
                        short_allowed=spec.short_allowed,
                        minimum_oos_trades=self.risk_config.min_out_of_sample_trades,
                        minimum_expectancy_bps=self.risk_config.min_expected_net_bps,
                    )
                    self._stats_cache[cache_key] = (latest_timestamp, cached_stats)
                else:
                    cached_stats = cached[1]
                stats[cache_key] = cached_stats

        ranked = rank_opportunities(
            scans, stats, self.scanner.specs, self.risk_config
        )
        rejected: list[tuple[str, str]] = [
            (scan.symbol, "; ".join(scan.reasons))
            for scan in scans
            if not scan.eligible and scan.reasons
        ]
        ranked_symbols = {candidate.symbol for candidate in ranked}
        rejected.extend(
            (
                scan.symbol,
                "no candidate passed signal-quality and out-of-sample net-edge thresholds",
            )
            for scan in scans
            if scan.eligible and scan.signals and scan.symbol not in ranked_symbols
        )
        rejected.extend(
            (scan.symbol, "no valid strategy signal on supported timeframes")
            for scan in scans
            if scan.eligible and not scan.signals
        )
        decision = TradingDecision(
            None, None, "HOLD", "no validated positive net-edge opportunity", ()
        )
        for candidate in ranked:
            scan = next(item for item in scans if item.symbol == candidate.symbol)
            try:
                spec = self.terminal.instrument_spec(candidate.symbol)
                if not spec.trade_allowed or not spec.session_open:
                    raise RiskRejected("market session or trading permission changed")
                if not self.scanner.valid_spec(spec):
                    raise RiskRejected("contract specifications changed or are invalid")
                quote = self.terminal.quote(candidate.symbol)
                quote_age = (
                    (now - quote.timestamp).total_seconds()
                    if quote.timestamp.tzinfo
                    else float("inf")
                )
                if (
                    not isfinite(quote.bid)
                    or not isfinite(quote.ask)
                    or quote.bid <= 0
                    or quote.ask < quote.bid
                    or quote_age < 0
                    or quote_age > self.scanner.max_quote_age_seconds
                ):
                    raise RiskRejected("selected instrument quote became stale")
                if (
                    quote.estimated_slippage_bps is None
                    or not isfinite(quote.estimated_slippage_bps)
                    or quote.estimated_slippage_bps < 0
                ):
                    raise RiskRejected("fresh slippage estimate is unavailable")
                candles = list(
                    self.scanner.buffers[
                        (candidate.symbol, candidate.signal.timeframe)
                    ]
                )
                fresh_signal = signal_for(
                    candidate.symbol,
                    candidate.signal.timeframe,
                    candles,
                    short_allowed=spec.short_allowed,
                    now=now,
                    max_age_seconds=self.scanner.max_quote_age_seconds
                    * {
                        "M1": 60,
                        "M5": 300,
                        "M15": 900,
                        "H1": 3600,
                    }[candidate.signal.timeframe.value],
                )
                if fresh_signal is None or fresh_signal.side is not candidate.side:
                    raise RiskRejected("strategy signal is no longer valid")
                mid = (quote.ask + quote.bid) / 2
                if mid <= 0:
                    raise RiskRejected("fresh quote has invalid midpoint")
                spread_bps = quote.spread / mid * 10_000
                commission_bps = (
                    2
                    * spec.commission_per_lot
                    / (spec.contract_size * mid)
                    * 10_000
                )
                financing_bps = (
                    max(abs(spec.swap_long_per_lot), abs(spec.swap_short_per_lot))
                    * self.scanner.estimated_holding_hours
                    / 24
                    / (spec.contract_size * mid)
                    * 10_000
                )
                current_costs_bps = (
                    spread_bps
                    + commission_bps
                    + financing_bps
                    + quote.estimated_slippage_bps
                )
                current_spread_to_atr = quote.spread / (
                    fresh_signal.stop_distance / 2
                )
                gross_stats = stats[
                    (candidate.symbol, candidate.signal.timeframe)
                ]
                current_net_bps = gross_stats.expectancy_bps - current_costs_bps
                if current_net_bps < self.risk_config.min_expected_net_bps:
                    raise RiskRejected("fresh execution costs remove the expected edge")
                risk_reward = (
                    current_net_bps / 10_000 * mid / fresh_signal.stop_distance
                )
                selected = replace(
                    candidate,
                    signal=fresh_signal,
                    expected_net_bps=current_net_bps,
                    score=risk_reward,
                    risk_reward=risk_reward,
                )
                proposal = self.risk.propose(
                    selected,
                    spec,
                    account,
                    portfolio,
                    quote=quote,
                    spread_bps=spread_bps,
                    slippage_bps=quote.estimated_slippage_bps,
                    spread_to_atr=current_spread_to_atr,
                    now=now,
                )
                decision = TradingDecision(
                    selected,
                    proposal,
                    "HOLD",
                    "proposal passed analysis and risk checks; broker execution disabled",
                    (),
                )
                break
            except (RiskRejected, RuntimeError, ValueError) as exc:
                rejected.append((candidate.symbol, str(exc)))
        if ranked and decision.proposal is None:
            decision = TradingDecision(
                None,
                None,
                "HOLD",
                "all ranked opportunities failed fresh health/risk checks",
                tuple(rejected),
            )
        elif not ranked:
            decision = replace(decision, rejected=tuple(rejected))
        decision = replace(decision, ranked=tuple(ranked))

        if self.store:
            self.store.record(
                "scan",
                {
                    "instruments": scans,
                    "ranked_opportunities": ranked,
                },
            )
            self.store.record(
                "decision",
                {
                    "action": decision.action,
                    "reason": decision.reason,
                    "selected_symbol": (
                        decision.selected.symbol if decision.selected else None
                    ),
                    "proposal": decision.proposal,
                    "rejected": decision.rejected,
                    "ranked": decision.ranked,
                },
            )
        return decision
