from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from math import isfinite

from .broker import ReadOnlyTerminal
from .models import Candle, InstrumentSpec, ScanResult, Side, Timeframe
from .strategy import signal_for

TIMEFRAMES = (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1)


class InstrumentScanner:
    def __init__(
        self,
        terminal: ReadOnlyTerminal,
        *,
        history_limit: int = 500,
        max_quote_age_seconds: float = 10.0,
        minimum_candles: int = 60,
        max_spread_to_atr: float = 0.15,
        max_spread_bps: float = 20.0,
        max_slippage_bps: float = 5.0,
        estimated_holding_hours: float = 4.0,
        max_buffer_size: int = 1000,
    ) -> None:
        self.terminal = terminal
        self.history_limit = history_limit
        self.max_quote_age_seconds = max_quote_age_seconds
        self.minimum_candles = minimum_candles
        self.max_spread_to_atr = max_spread_to_atr
        self.max_spread_bps = max_spread_bps
        self.max_slippage_bps = max_slippage_bps
        self.estimated_holding_hours = estimated_holding_hours
        self.buffers: dict[tuple[str, Timeframe], deque[Candle]] = defaultdict(
            lambda: deque(maxlen=max_buffer_size)
        )
        self.specs: dict[str, InstrumentSpec] = {}

    @staticmethod
    def valid_spec(spec: InstrumentSpec) -> bool:
        values = (
            spec.min_volume,
            spec.max_volume,
            spec.volume_step,
            spec.tick_size,
            spec.tick_value,
            spec.contract_size,
            spec.margin_per_lot,
        )
        costs = (
            spec.commission_per_lot,
            spec.swap_long_per_lot,
            spec.swap_short_per_lot,
        )
        return (
            all(isfinite(value) and value > 0 for value in values)
            and all(value is not None and isfinite(value) for value in costs)
            and spec.max_volume >= spec.min_volume
            and bool(spec.account_currency)
        )

    def _update_buffer(self, symbol: str, timeframe: Timeframe, now: datetime) -> list[Candle]:
        key = (symbol, timeframe)
        buffer = self.buffers[key]
        latest = buffer[-1].timestamp if buffer else None
        fetched = self.terminal.candles(
            symbol,
            timeframe,
            since=latest,
            limit=self.history_limit if latest is None else 10,
        )
        for candle in fetched:
            prices = (candle.open, candle.high, candle.low, candle.close)
            if (
                candle.timestamp.tzinfo is None
                or not all(isfinite(price) and price > 0 for price in prices)
                or candle.high < max(candle.open, candle.close)
                or candle.low > min(candle.open, candle.close)
                or candle.high < candle.low
            ):
                raise ValueError(f"invalid candle data for {symbol}/{timeframe.value}")
        by_timestamp = {candle.timestamp: candle for candle in buffer}
        by_timestamp.update({candle.timestamp: candle for candle in fetched})
        buffer.clear()
        buffer.extend(
            candle
            for _, candle in sorted(by_timestamp.items())
            if candle.complete and candle.timestamp <= now
        )
        return list(buffer)

    def scan_once(self, now: datetime | None = None) -> list[ScanResult]:
        now = now or datetime.now(timezone.utc)
        results: list[ScanResult] = []
        for symbol in sorted(set(self.terminal.discover_symbols())):
            reasons: list[str] = []
            try:
                spec = self.terminal.instrument_spec(symbol)
                self.specs[symbol] = spec
                quote = self.terminal.quote(symbol)
            except (
                RuntimeError,
                OSError,
                ValueError,
                TimeoutError,
                ConnectionError,
                KeyError,
            ) as exc:
                results.append(ScanResult(symbol, False, reasons=(str(exc),)))
                continue

            if not spec.trade_allowed:
                reasons.append("trading not permitted")
            if not spec.session_open:
                reasons.append("market session closed")
            if not self.valid_spec(spec):
                reasons.append("invalid or incomplete contract specifications")
            if quote.timestamp.tzinfo is None:
                reasons.append("quote timestamp has no timezone")
                quote_age = float("inf")
            else:
                quote_age = (now - quote.timestamp).total_seconds()
            if (
                not isfinite(quote.bid)
                or not isfinite(quote.ask)
                or quote.bid <= 0
                or quote.ask < quote.bid
                or quote_age < 0
                or quote_age > self.max_quote_age_seconds
            ):
                reasons.append("invalid or stale quote")
            if (
                quote.estimated_slippage_bps is None
                or not isfinite(quote.estimated_slippage_bps)
                or quote.estimated_slippage_bps < 0
            ):
                reasons.append("slippage estimate unavailable")
            elif quote.estimated_slippage_bps > self.max_slippage_bps:
                reasons.append("estimated slippage exceeds configured limit")

            quote_valid = (
                isfinite(quote.bid)
                and isfinite(quote.ask)
                and quote.bid > 0
                and quote.ask >= quote.bid
                and 0 <= quote_age <= self.max_quote_age_seconds
            )
            if (
                not quote_valid
                or not spec.trade_allowed
                or not spec.session_open
                or not self.valid_spec(spec)
                or quote.estimated_slippage_bps is None
                or not isfinite(quote.estimated_slippage_bps)
                or quote.estimated_slippage_bps > self.max_slippage_bps
            ):
                mid = (quote.ask + quote.bid) / 2 if quote_valid else 0.0
                results.append(
                    ScanResult(
                        symbol=symbol,
                        eligible=False,
                        category=spec.category,
                        session_open=spec.session_open,
                        quote=quote,
                        spread_bps=quote.spread / mid * 10_000 if mid > 0 else None,
                        slippage_bps=quote.estimated_slippage_bps,
                        reasons=tuple(reasons),
                    )
                )
                continue

            signals = []
            spread_bps = None
            slippage_bps = quote.estimated_slippage_bps
            commission_bps = None
            financing_bps = None
            spread_to_atr = None
            costs_bps = None
            enough_history = False
            history_error = None
            for timeframe in TIMEFRAMES:
                try:
                    candles = self._update_buffer(symbol, timeframe, now)
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    TimeoutError,
                    ConnectionError,
                    KeyError,
                ) as exc:
                    history_error = f"candle history unavailable: {exc}"
                    break
                if len(candles) < self.minimum_candles:
                    continue
                enough_history = True
                true_ranges = [
                    candle.high - candle.low for candle in candles[-14:]
                ]
                atr = sum(true_ranges) / len(true_ranges)
                if atr <= 0:
                    continue
                spread_to_atr = quote.spread / atr
                mid = (quote.ask + quote.bid) / 2
                commission_bps = (
                    2
                    * spec.commission_per_lot
                    / (spec.contract_size * mid)
                    * 10_000
                    if mid > 0
                    else float("inf")
                )
                spread_bps = quote.spread / mid * 10_000
                financing_bps = (
                    max(abs(spec.swap_long_per_lot), abs(spec.swap_short_per_lot))
                    * self.estimated_holding_hours
                    / 24
                    / (spec.contract_size * mid)
                    * 10_000
                    if mid > 0
                    else float("inf")
                )
                costs_bps = (
                    spread_bps
                    + commission_bps
                    + financing_bps
                    + (slippage_bps if slippage_bps is not None else float("inf"))
                )
                if spread_to_atr > self.max_spread_to_atr:
                    continue
                if spread_bps > self.max_spread_bps:
                    continue
                signal = signal_for(
                    symbol,
                    timeframe,
                    candles,
                    short_allowed=spec.short_allowed,
                    now=now,
                    max_age_seconds=self.max_quote_age_seconds
                    * max(1, {Timeframe.M1: 60, Timeframe.M5: 300,
                              Timeframe.M15: 900, Timeframe.H1: 3600}[timeframe]),
                )
                if signal and (
                    signal.side is Side.BUY
                    and spec.long_allowed
                    or signal.side is Side.SELL
                    and spec.short_allowed
                ):
                    signals.append(signal)

            if history_error:
                results.append(
                    ScanResult(
                        symbol=symbol,
                        eligible=False,
                        category=spec.category,
                        session_open=spec.session_open,
                        quote=quote,
                        spread_bps=spread_bps,
                        slippage_bps=slippage_bps,
                        reasons=(history_error,),
                    )
                )
                continue
            if not enough_history:
                reasons.append("insufficient candle history")
            if spread_to_atr is None:
                reasons.append("volatility estimate unavailable")
            if spread_to_atr is not None and spread_to_atr > self.max_spread_to_atr:
                reasons.append("spread is excessive relative to volatility")
            if spread_bps is not None and spread_bps > self.max_spread_bps:
                reasons.append("spread exceeds configured basis-point limit")
            results.append(
                ScanResult(
                    symbol=symbol,
                    eligible=not reasons,
                    category=spec.category,
                    session_open=spec.session_open,
                    quote=quote,
                    spread_bps=spread_bps,
                    slippage_bps=slippage_bps,
                    commission_bps=commission_bps,
                    financing_bps=financing_bps,
                    spread_to_atr=spread_to_atr,
                    signals=tuple(signals),
                    reasons=tuple(reasons),
                    costs_bps=costs_bps,
                )
            )
        return results
