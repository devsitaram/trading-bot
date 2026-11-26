from __future__ import annotations

from .models import (
    InstrumentSpec,
    RankedOpportunity,
    RiskConfig,
    ScanResult,
    Side,
    StrategyStats,
)


def rank_opportunities(
    scans: list[ScanResult],
    stats: dict[tuple[str, object], StrategyStats],
    specs: dict[str, InstrumentSpec],
    config: RiskConfig,
) -> list[RankedOpportunity]:
    opportunities: list[RankedOpportunity] = []
    for scan in scans:
        if not scan.eligible or scan.costs_bps is None:
            continue
        spec = specs[scan.symbol]
        for signal in scan.signals:
            if signal.quality < config.min_signal_quality:
                continue
            if signal.side is Side.SELL and not spec.short_allowed:
                continue
            result = stats.get((scan.symbol, signal.timeframe))
            if (
                result is None
                or not result.validated
                or result.out_of_sample_trades < config.min_out_of_sample_trades
            ):
                continue
            if result.expectancy_bps <= config.min_expected_net_bps:
                continue
            expected_net = result.expectancy_bps - scan.costs_bps
            if expected_net < config.min_expected_net_bps:
                continue
            mid = (scan.quote.ask + scan.quote.bid) / 2
            expected_reward = expected_net / 10_000 * mid
            risk_reward = expected_reward / signal.stop_distance
            if risk_reward <= 0:
                continue
            opportunities.append(
                RankedOpportunity(
                    symbol=scan.symbol,
                    side=signal.side,
                    score=risk_reward,
                    expected_net_bps=expected_net,
                    risk_reward=risk_reward,
                    signal=signal,
                    reasons=(
                        "chronological out-of-sample strategy passed thresholds",
                        f"estimated net edge {expected_net:.2f} bps; live costs {scan.costs_bps:.2f} bps",
                        "ranked by net edge relative to stop distance",
                    ),
                )
            )
    return sorted(opportunities, key=lambda item: item.score, reverse=True)
