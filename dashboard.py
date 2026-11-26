from __future__ import annotations

from dataclasses import asdict
from typing import Any

from trading_agent.models import (
    AccountMode,
    AccountState,
    OrderProposal,
    PortfolioSnapshot,
    RankedOpportunity,
    ScanResult,
)


def dashboard_payload(
    scans: list[ScanResult],
    account: AccountState | None,
    *,
    portfolio: PortfolioSnapshot | None = None,
    ranked: tuple[RankedOpportunity, ...] = (),
    rejected: tuple[tuple[str, str], ...] = (),
    selected_symbol: str | None,
    proposal: OrderProposal | None = None,
    emergency_stop: bool,
) -> dict[str, Any]:
    portfolio = portfolio or PortfolioSnapshot()
    return {
        "mode": account.mode.value if account else AccountMode.UNKNOWN.value,
        "account_verified": bool(account and account.mode is AccountMode.DEMO),
        "emergency_stop": emergency_stop,
        "daily_pnl": account.daily_pnl if account else None,
        "daily_pnl_source": "broker account state; not an estimated opportunity",
        "equity_drawdown": (
            max(0.0, account.peak_equity - account.equity)
            if account
            else None
        ),
        "estimated_opportunities_are_not_realized_profit": True,
        "selected_symbol": selected_symbol,
        "selected_rationale": next(
            (
                list(item.reasons)
                for item in ranked
                if item.symbol == selected_symbol
            ),
            [],
        ),
        "proposed_order": asdict(proposal) if proposal else None,
        "instruments": [
            {
                "symbol": scan.symbol,
                "category": scan.category,
                "eligible": scan.eligible,
                "session_status": (
                    "unknown"
                    if scan.session_open is None
                    else "open"
                    if scan.session_open
                    else "closed"
                ),
                "bid": scan.quote.bid if scan.quote else None,
                "ask": scan.quote.ask if scan.quote else None,
                "spread": scan.quote.spread if scan.quote else None,
                "spread_bps": scan.spread_bps,
                "slippage_bps_estimate": scan.slippage_bps,
                "spread_to_atr": scan.spread_to_atr,
                "commission_bps_estimate": scan.commission_bps,
                "financing_bps_estimate": scan.financing_bps,
                "costs_bps_estimate": scan.costs_bps,
                "signals": [
                    {
                        "direction": signal.side.value,
                        "timeframe": signal.timeframe.value,
                        "strategy_version": signal.strategy_version,
                        "quality": signal.quality,
                        "stop_distance": signal.stop_distance,
                        "reason": signal.reason,
                        "timestamp": signal.timestamp.isoformat(),
                    }
                    for signal in scan.signals
                ],
                "rejection_reasons": list(scan.reasons),
            }
            for scan in scans
        ],
        "open_positions": [asdict(position) for position in portfolio.positions],
        "pending_orders": [asdict(order) for order in portfolio.pending_orders],
        "account_wide_open_risk": sum(
            max(0.0, position.risk_amount) for position in portfolio.positions
        )
        + sum(
            max(0.0, order.risk_amount) for order in portfolio.pending_orders
        ),
        "ranked_candidates": [
            {
                "symbol": item.symbol,
                "direction": item.side.value,
                "score": item.score,
                "expected_net_bps_estimate": item.expected_net_bps,
                "risk_reward_estimate": item.risk_reward,
                "strategy_version": item.signal.strategy_version,
                "reasons": list(item.reasons),
            }
            for item in ranked
        ],
        "rejected_opportunities": [
            {"symbol": symbol, "reason": reason} for symbol, reason in rejected
        ],
        "orders_submitted": False,
    }
