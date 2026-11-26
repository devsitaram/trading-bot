from __future__ import annotations

import time
from typing import Any, Callable


class MT5Error(Exception):
    pass


class MT5Client:
    """Talks to the MT5 terminal. `mt5` is the MetaTrader5 module, which installs only on Windows."""

    def __init__(self, mt5: Any, sleep: Callable[[float], None] = time.sleep) -> None:
        self._mt5 = mt5
        self._sleep = sleep

    def connect(self, login: int, password: str, server: str, attempts: int, backoff_seconds: float) -> None:
        for attempt in range(attempts):
            if self._mt5.initialize(login=login, password=password, server=server):
                return
            error = self._mt5.last_error()
            self._mt5.shutdown()
            if attempt < attempts - 1:
                self._sleep(backoff_seconds * 2**attempt)
        raise MT5Error(f"Could not connect to MT5 on {server} after {attempts} attempts: {error}")

    def account(self) -> dict[str, Any]:
        info = self._mt5.account_info()
        if info is None:
            raise MT5Error(f"Could not read the account: {self._mt5.last_error()}")
        return {
            "login": info.login,
            "server": info.server,
            "demo": info.trade_mode == self._mt5.ACCOUNT_TRADE_MODE_DEMO,
            "currency": info.currency,
            "balance": info.balance,
            "equity": info.equity,
            "leverage": info.leverage,
            "algo_trading_allowed": info.trade_expert,
        }

    def symbol(self, name: str) -> dict[str, Any]:
        info = self._mt5.symbol_info(name)
        if info is None or not self._mt5.symbol_select(name, True):
            raise MT5Error(f'Symbol "{name}" was not found. Check its exact name in Market Watch and set it in config.yaml.')
        return {
            "name": info.name,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
            "volume_max": info.volume_max,
            "stops_level_points": info.trade_stops_level,
            "contract_size": info.trade_contract_size,
            "point": info.point,
            "spread_points": info.spread,
            "filling_mode": info.filling_mode,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
        }

    def shutdown(self) -> None:
        self._mt5.shutdown()
