from __future__ import annotations

from datetime import datetime, timedelta
from math import floor, isfinite

from .models import (
    AccountMode,
    AccountState,
    InstrumentSpec,
    OrderProposal,
    PortfolioSnapshot,
    Quote,
    RankedOpportunity,
    RiskConfig,
    Side,
)


class RiskRejected(ValueError):
    pass


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.cooldown_until: datetime | None = None

    def check(
        self,
        opportunity: RankedOpportunity,
        spec: InstrumentSpec,
        account: AccountState,
        portfolio: PortfolioSnapshot,
        *,
        spread_bps: float,
        slippage_bps: float,
        spread_to_atr: float,
        now: datetime,
    ) -> None:
        cfg = self.config
        if cfg.emergency_stop:
            raise RiskRejected("global emergency stop is active")
        if account.mode is not AccountMode.DEMO:
            raise RiskRejected("account is not verified DEMO")
        if (
            not isfinite(account.equity)
            or not isfinite(account.balance)
            or not isfinite(account.free_margin)
            or not isfinite(account.daily_pnl)
            or account.equity <= 0
            or account.balance < 0
            or account.free_margin < 0
        ):
            raise RiskRejected("invalid account equity or free margin")
        if account.peak_equity <= 0 or not isfinite(account.peak_equity):
            raise RiskRejected("invalid account peak equity")
        if (
            not isfinite(spread_bps)
            or spread_bps < 0
            or not isfinite(spread_to_atr)
            or spread_to_atr < 0
        ):
            raise RiskRejected("spread estimate is unavailable")
        if (
            not isfinite(slippage_bps)
            or slippage_bps < 0
            or slippage_bps > cfg.max_slippage_bps
        ):
            raise RiskRejected("slippage estimate is unavailable or exceeds limit")
        if account.currency != spec.account_currency:
            raise RiskRejected("account and instrument margin currencies differ")
        if self.cooldown_until and now < self.cooldown_until:
            raise RiskRejected("risk cooldown is active")
        if portfolio.consecutive_losses >= cfg.max_consecutive_losses:
            raise RiskRejected("consecutive-loss limit reached")
        if (
            portfolio.consecutive_execution_errors
            >= cfg.max_consecutive_execution_errors
        ):
            raise RiskRejected("execution-error limit reached")
        if account.daily_pnl <= -account.equity * cfg.max_daily_loss_fraction:
            raise RiskRejected("maximum daily loss reached")
        drawdown = max(0.0, account.peak_equity - account.equity)
        if drawdown >= account.peak_equity * cfg.max_drawdown_fraction:
            raise RiskRejected("maximum account drawdown reached")
        if spread_to_atr > cfg.max_spread_to_atr:
            raise RiskRejected("spread exceeds configured limit")
        if spread_bps > cfg.max_spread_bps:
            raise RiskRejected("spread exceeds configured basis-point limit")
        if opportunity.signal.quality < cfg.min_signal_quality:
            raise RiskRejected("signal quality is below configured minimum")
        if opportunity.expected_net_bps < cfg.min_expected_net_bps:
            raise RiskRejected("expected edge is below configured minimum")
        if opportunity.side is Side.SELL and not spec.short_allowed:
            raise RiskRejected("instrument does not permit short positions")
        if opportunity.side is Side.BUY and not spec.long_allowed:
            raise RiskRejected("instrument does not permit long positions")
        if not all(
            isfinite(value) and value > 0
            for value in (
                spec.min_volume,
                spec.max_volume,
                spec.volume_step,
                spec.tick_size,
                spec.tick_value,
                spec.contract_size,
                spec.margin_per_lot,
            )
        ):
            raise RiskRejected("contract specifications are invalid")
        if any(
            item.symbol == opportunity.symbol for item in portfolio.positions
        ) or any(
            item.symbol == opportunity.symbol for item in portfolio.pending_orders
        ):
            raise RiskRejected("position or pending order already exists on symbol")
        exposure_risks = [
            item.risk_amount
            for item in (*portfolio.positions, *portfolio.pending_orders)
        ]
        if any(not isfinite(value) or value <= 0 for value in exposure_risks):
            raise RiskRejected("existing position/order risk cannot be verified")
        total_open_risk = sum(exposure_risks)
        if total_open_risk >= account.equity * cfg.max_total_open_risk_fraction:
            raise RiskRejected("account-wide open-risk limit reached")
        if len(portfolio.positions) + len(portfolio.pending_orders) >= cfg.max_open_positions:
            raise RiskRejected("maximum open positions/orders reached")
        group = cfg.correlation_groups.get(opportunity.symbol)
        if not group:
            raise RiskRejected("correlation group is not configured for symbol")
        if any(
            item.symbol not in cfg.correlation_groups
            for item in (*portfolio.positions, *portfolio.pending_orders)
        ):
            raise RiskRejected("correlation group is missing for existing exposure")
        group_count = sum(
            cfg.correlation_groups.get(item.symbol) == group
            for item in (*portfolio.positions, *portfolio.pending_orders)
        )
        if group_count >= cfg.max_correlated_positions:
            raise RiskRejected("correlated-exposure limit reached")
        if account.free_margin < account.equity * cfg.min_free_margin_fraction:
            raise RiskRejected("free margin is below configured minimum")

    def propose(
        self,
        opportunity: RankedOpportunity,
        spec: InstrumentSpec,
        account: AccountState,
        portfolio: PortfolioSnapshot,
        *,
        quote: Quote,
        spread_bps: float,
        slippage_bps: float,
        spread_to_atr: float,
        now: datetime | None = None,
    ) -> OrderProposal:
        now = now or datetime.now().astimezone()
        self.check(
            opportunity,
            spec,
            account,
            portfolio,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            spread_to_atr=spread_to_atr,
            now=now,
        )
        stop_distance = opportunity.signal.stop_distance
        if stop_distance <= 0 or not isfinite(stop_distance):
            raise RiskRejected("invalid stop distance")
        cash_risk_per_lot = stop_distance / spec.tick_size * spec.tick_value
        if cash_risk_per_lot <= 0 or not isfinite(cash_risk_per_lot):
            raise RiskRejected("cannot calculate risk from contract specifications")
        risk_budget = account.equity * self.config.risk_per_trade_fraction
        raw_volume = risk_budget / cash_risk_per_lot
        volume = floor(raw_volume / spec.volume_step) * spec.volume_step
        if volume < spec.min_volume:
            raise RiskRejected("risk budget is below minimum tradable volume")
        volume = min(volume, spec.max_volume)
        estimated_margin = volume * spec.margin_per_lot
        if estimated_margin > account.free_margin - account.equity * self.config.min_free_margin_fraction:
            raise RiskRejected("insufficient free margin for proposed volume")
        risk_amount = cash_risk_per_lot * volume
        if risk_amount > risk_budget + 1e-8:
            raise RiskRejected("rounded volume exceeds per-trade risk budget")
        existing_risk = sum(
            max(0.0, item.risk_amount) for item in portfolio.positions
        ) + sum(max(0.0, item.risk_amount) for item in portfolio.pending_orders)
        if (
            existing_risk + risk_amount
            > account.equity * self.config.max_total_open_risk_fraction
        ):
            raise RiskRejected("proposed order would exceed account-wide open-risk limit")
        if (
            not isfinite(quote.bid)
            or not isfinite(quote.ask)
            or quote.bid <= 0
            or quote.ask <= 0
            or quote.ask < quote.bid
            or quote.timestamp.tzinfo is None
        ):
            raise RiskRejected("fresh quote is invalid")
        entry = quote.ask if opportunity.side is Side.BUY else quote.bid
        if opportunity.side is Side.BUY:
            stop_loss = entry - stop_distance
            take_profit = entry + stop_distance * opportunity.risk_reward
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - stop_distance * opportunity.risk_reward
        if stop_loss <= 0 or take_profit <= 0:
            raise RiskRejected("calculated protective levels are invalid")
        return OrderProposal(
            symbol=opportunity.symbol,
            side=opportunity.side,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_amount=risk_amount,
            rationale="risk-sized proposal only; no order was submitted",
        )

    def record_loss(self, now: datetime | None = None) -> None:
        self.cooldown_until = (now or datetime.now().astimezone()) + timedelta(
            minutes=self.config.cooldown_minutes
        )

    def record_execution_error(self, now: datetime | None = None) -> None:
        self.cooldown_until = (now or datetime.now().astimezone()) + timedelta(
            minutes=self.config.cooldown_minutes
        )
