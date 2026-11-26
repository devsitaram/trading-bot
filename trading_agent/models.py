from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class AccountMode(StrEnum):
    DEMO = "DEMO"
    LIVE = "LIVE"
    UNKNOWN = "UNKNOWN"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    complete: bool = True


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: datetime
    estimated_slippage_bps: float | None = None

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    category: str
    trade_allowed: bool
    session_open: bool
    min_volume: float
    max_volume: float
    volume_step: float
    tick_size: float
    tick_value: float
    contract_size: float
    margin_per_lot: float
    commission_per_lot: float | None = None
    swap_long_per_lot: float | None = None
    swap_short_per_lot: float | None = None
    account_currency: str = ""
    long_allowed: bool = True
    short_allowed: bool = False


@dataclass(frozen=True)
class AccountState:
    mode: AccountMode
    account_id: str
    currency: str
    equity: float
    balance: float
    free_margin: float
    daily_pnl: float
    peak_equity: float
    timestamp: datetime


@dataclass(frozen=True)
class Position:
    symbol: str
    side: Side
    volume: float
    stop_loss: float | None
    take_profit: float | None
    risk_amount: float


@dataclass(frozen=True)
class PendingOrder:
    symbol: str
    side: Side
    volume: float
    stop_loss: float | None
    risk_amount: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    positions: tuple[Position, ...] = ()
    pending_orders: tuple[PendingOrder, ...] = ()
    recent_deals: tuple[dict[str, object], ...] = ()
    consecutive_losses: int = 0
    consecutive_execution_errors: int = 0


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    timeframe: Timeframe
    strategy_version: str
    quality: float
    stop_distance: float
    reason: str
    timestamp: datetime


@dataclass(frozen=True)
class StrategyStats:
    symbol: str
    timeframe: Timeframe
    strategy_version: str
    out_of_sample_trades: int
    expectancy_bps: float
    win_rate: float
    max_drawdown_bps: float
    validated: bool


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    eligible: bool
    category: str = "unknown"
    session_open: bool | None = None
    quote: Quote | None = None
    spread_bps: float | None = None
    slippage_bps: float | None = None
    commission_bps: float | None = None
    financing_bps: float | None = None
    spread_to_atr: float | None = None
    signals: tuple[Signal, ...] = ()
    reasons: tuple[str, ...] = ()
    costs_bps: float | None = None


@dataclass(frozen=True)
class RankedOpportunity:
    symbol: str
    side: Side
    score: float
    expected_net_bps: float
    risk_reward: float
    signal: Signal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_fraction: float = 0.0025
    max_total_open_risk_fraction: float = 0.01
    max_daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.05
    max_open_positions: int = 3
    max_correlated_positions: int = 2
    max_spread_to_atr: float = 0.15
    max_spread_bps: float = 20.0
    max_slippage_bps: float = 5.0
    min_free_margin_fraction: float = 0.30
    max_consecutive_losses: int = 3
    max_consecutive_execution_errors: int = 2
    cooldown_minutes: int = 60
    min_signal_quality: float = 0.65
    min_expected_net_bps: float = 2.0
    min_out_of_sample_trades: int = 30
    emergency_stop: bool = True
    correlation_groups: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fractions = (
            self.risk_per_trade_fraction,
            self.max_total_open_risk_fraction,
            self.max_daily_loss_fraction,
            self.max_drawdown_fraction,
            self.min_free_margin_fraction,
        )
        if any(not 0 < value <= 1 for value in fractions):
            raise ValueError("risk and margin fractions must be in (0, 1]")
        if self.max_open_positions < 1 or self.max_correlated_positions < 1:
            raise ValueError("position limits must be positive")
        if self.max_spread_to_atr <= 0 or self.max_spread_bps < 0:
            raise ValueError("spread limits must be positive")
        if not 0 <= self.min_signal_quality <= 1:
            raise ValueError("minimum signal quality must be in [0, 1]")
        if self.min_expected_net_bps < 0 or self.min_out_of_sample_trades < 1:
            raise ValueError("edge and out-of-sample thresholds are invalid")
        if self.risk_per_trade_fraction > self.max_total_open_risk_fraction:
            raise ValueError("per-trade risk cannot exceed total open-risk limit")
        if self.max_slippage_bps < 0 or self.cooldown_minutes < 1:
            raise ValueError("slippage and cooldown limits are invalid")


@dataclass(frozen=True)
class OrderProposal:
    symbol: str
    side: Side
    volume: float
    stop_loss: float
    take_profit: float
    risk_amount: float
    rationale: str
