from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import (
    AccountState,
    Candle,
    InstrumentSpec,
    PortfolioSnapshot,
    Quote,
    Timeframe,
)


class ReadOnlyTerminal(Protocol):
    """Read-only contract for an authenticated broker adapter.

    A concrete adapter must establish its own authenticated connection. A web
    terminal URL is not an API endpoint and must never be used as credentials.
    """

    def account_state(self) -> AccountState: ...

    def discover_symbols(self) -> list[str]: ...

    def instrument_spec(self, symbol: str) -> InstrumentSpec: ...

    def quote(self, symbol: str) -> Quote: ...

    def candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[Candle]: ...

    def portfolio(self) -> PortfolioSnapshot: ...


class UnconfiguredTerminal:
    """Safe default adapter: no account access and no order execution."""

    def _unavailable(self, operation: str) -> None:
        raise RuntimeError(
            "No authenticated broker adapter is configured. "
            f"Cannot perform {operation}; trading is disabled."
        )

    def account_state(self) -> AccountState:
        self._unavailable("read account state")

    def discover_symbols(self) -> list[str]:
        self._unavailable("discover symbols")

    def instrument_spec(self, symbol: str) -> InstrumentSpec:
        self._unavailable(f"read contract specifications for {symbol}")

    def quote(self, symbol: str) -> Quote:
        self._unavailable(f"read quote for {symbol}")

    def candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[Candle]:
        self._unavailable(
            f"read {timeframe.value} candles for {symbol} since {since} (limit {limit})"
        )

    def portfolio(self) -> PortfolioSnapshot:
        self._unavailable("reconcile portfolio")
