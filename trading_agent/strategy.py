from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from math import isfinite

from .models import Candle, Side, Signal, StrategyStats, Timeframe

STRATEGY_VERSION = "trend-breakout-1.0"


def _ema(values: Sequence[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"EMA requires at least {period} observations")
    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        raise ValueError(f"ATR requires at least {period + 1} candles")
    ranges = []
    for previous, current in zip(candles[-period - 1 : -1], candles[-period:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(ranges) / period


def signal_for(
    symbol: str,
    timeframe: Timeframe,
    candles: Sequence[Candle],
    *,
    short_allowed: bool,
    now: datetime,
    max_age_seconds: float,
) -> Signal | None:
    """Trend-confirmed breakout; emits no signal on incomplete/stale history."""
    closed = [candle for candle in candles if candle.complete]
    if len(closed) < 30:
        return None
    latest = closed[-1]
    if (now - latest.timestamp).total_seconds() > max_age_seconds:
        return None
    closes = [candle.close for candle in closed]
    atr = _atr(closed)
    if atr <= 0 or not isfinite(atr):
        return None

    fast, slow = _ema(closes[-30:], 8), _ema(closes[-30:], 21)
    prior = closed[-21:-1]
    breakout_high = max(candle.high for candle in prior)
    breakout_low = min(candle.low for candle in prior)

    side = Side.HOLD
    if fast > slow and latest.close > breakout_high:
        side = Side.BUY
    elif short_allowed and fast < slow and latest.close < breakout_low:
        side = Side.SELL
    if side is Side.HOLD:
        return None

    quality = min(1.0, abs(fast - slow) / atr)
    return Signal(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        strategy_version=STRATEGY_VERSION,
        quality=quality,
        stop_distance=2.0 * atr,
        reason=f"{timeframe.value} trend-confirmed range breakout",
        timestamp=now,
    )


def walk_forward_backtest(
    symbol: str,
    timeframe: Timeframe,
    candles: Sequence[Candle],
    *,
    costs_bps: float,
    short_allowed: bool,
    minimum_oos_trades: int = 30,
    minimum_expectancy_bps: float = 2.0,
) -> StrategyStats:
    """Chronological 70/30 split; one-bar next-open-to-close returns include costs."""
    closed = [candle for candle in candles if candle.complete]
    split = int(len(closed) * 0.70)
    outcomes: list[float] = []
    for index in range(max(30, split), len(closed) - 1):
        window = closed[: index + 1]
        signal = signal_for(
            symbol,
            timeframe,
            window,
            short_allowed=short_allowed,
            now=window[-1].timestamp,
            max_age_seconds=float("inf"),
        )
        if signal is None:
            continue
        entry = closed[index + 1].open
        exit_price = closed[index + 1].close
        if entry <= 0:
            continue
        direction = 1.0 if signal.side is Side.BUY else -1.0
        outcomes.append(direction * (exit_price - entry) / entry * 10_000)

    expectancy = sum(outcomes) / len(outcomes) if outcomes else 0.0
    wins = sum(outcome > 0 for outcome in outcomes)
    equity, peak, max_drawdown = 0.0, 0.0, 0.0
    for outcome in outcomes:
        equity += outcome
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    validated = (
        len(outcomes) >= minimum_oos_trades
        and expectancy - costs_bps >= minimum_expectancy_bps
    )
    return StrategyStats(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=STRATEGY_VERSION,
        out_of_sample_trades=len(outcomes),
        expectancy_bps=expectancy,
        win_rate=wins / len(outcomes) if outcomes else 0.0,
        max_drawdown_bps=max_drawdown,
        validated=validated,
    )
