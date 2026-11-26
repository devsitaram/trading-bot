"""Link to a JustMarkets account (demo or real) through the MT5 add-on mt5/JustMarketsBridge.mq5.

The add-on reports the account, its positions, live prices and candles; the chart page queues orders; the add-on
executes them in MT5. Demo accounts are traded as soon as they are linked. A REAL (money) account needs two separate
switches: AllowRealAccount in the add-on's inputs in MT5, and the chart page set to Real account (its orders then
carry allow_real). Contest accounts are never traded.
"""

from __future__ import annotations

from itertools import count
from math import isfinite
import threading
import time
from typing import Any

LINK_MAX_AGE_SECONDS = 5
TIMEFRAMES = ("1m", "5m", "15m", "1h")
MAX_BARS = 1500
MAX_POSITIONS = 50
MAX_VOLUME = 1.0  # lots; the 1% risk rule sizes far below this on a small account
MAX_PENDING_ORDERS = 10
MODES = ("DEMO", "REAL", "CONTEST")
SIDES = ("BUY", "SELL")


class BrokerError(ValueError):
    pass


def _number(value: object, name: str, *, minimum: float = 0.0, exclusive: bool = True, maximum: float = float("inf")) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise BrokerError(f"{name} must be a number")
    if value < minimum or (exclusive and value == minimum) or value > maximum:
        raise BrokerError(f"{name} is out of range")
    return float(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BrokerError(f"{name} must be a positive integer")
    return value


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BrokerError(f"{name} must be a whole number of 0 or more")
    return value


def _text(value: object, name: str, *, max_length: int = 64) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= max_length:
        raise BrokerError(f"{name} must be text")
    return value


def _position(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("side") not in SIDES:
        raise BrokerError("positions must be objects with side BUY or SELL")
    return {
        "ticket": _integer(raw.get("ticket"), "ticket"),
        "side": raw["side"],
        "volume": _number(raw.get("volume"), "volume", maximum=1000),
        "price": _number(raw.get("price"), "price"),
        "sl": _number(raw.get("sl"), "sl", exclusive=False),
        "tp": _number(raw.get("tp"), "tp", exclusive=False),
        "profit": _number(raw.get("profit"), "profit", minimum=-1e9),
        "time": _integer(raw.get("time"), "time"),
    }


class DemoBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account: dict[str, Any] | None = None
        self._account_at = 0.0
        self._account_wall = 0.0
        self._bars: dict[str, list[list[float]]] = {}
        self._orders: list[dict[str, Any]] = []
        self._ids = count(1)
        # Only one open chart tab may trade the account; others would fight over the same position.
        self._controller: str | None = None
        self._controller_seen = 0.0

    # --- from the MT5 add-on -------------------------------------------------------------------------------------

    def report_account(self, payload: dict[str, Any]) -> None:
        if payload.get("mode") not in MODES:
            raise BrokerError("mode must be DEMO, REAL or CONTEST")
        positions = payload.get("positions")
        if not isinstance(positions, list) or len(positions) > MAX_POSITIONS:
            raise BrokerError("positions must be a list")
        bid = _number(payload.get("bid"), "bid")
        ask = _number(payload.get("ask"), "ask")
        if ask < bid:
            raise BrokerError("ask must not be below bid")
        account = {
            "login": _integer(payload.get("login"), "login"),
            "server": _text(payload.get("server"), "server"),
            "company": _text(payload.get("company"), "company", max_length=128),
            "mode": payload["mode"],
            "currency": _text(payload.get("currency"), "currency", max_length=8),
            "balance": _number(payload.get("balance"), "balance", exclusive=False),
            "equity": _number(payload.get("equity"), "equity", minimum=-1e9),
            "symbol": _text(payload.get("symbol"), "symbol", max_length=32),
            "bid": bid,
            "ask": ask,
            "positions": [_position(p) for p in positions],
            # From MT5's deal history: today's P/L and bot trades since local midnight, bot wins over 30 days.
            "today_pl": _number(payload.get("today_pl"), "today_pl", minimum=-1e9),
            "today_trades": _count(payload.get("today_trades"), "today_trades"),
            "wins": _count(payload.get("wins"), "wins"),
            "closed": _count(payload.get("closed"), "closed"),
        }
        with self._lock:
            self._account = account
            self._account_at = time.monotonic()
            self._account_wall = time.time()  # lets the page tell whether a report came after an order's result

    def set_bars(self, payload: dict[str, Any]) -> None:
        timeframe, bars = payload.get("tf"), payload.get("bars")
        if timeframe not in TIMEFRAMES or not isinstance(bars, list) or not 0 < len(bars) <= MAX_BARS:
            raise BrokerError("bars need tf (1m, 5m, 15m, 1h) and 1–1500 bars")
        clean = []
        for bar in bars:
            if not isinstance(bar, list) or len(bar) != 5:
                raise BrokerError("each bar is [time, open, high, low, close]")
            t = _integer(bar[0], "bar time")
            o, h, l, c = (_number(v, "bar price") for v in bar[1:])
            if not l <= min(o, c) <= max(o, c) <= h:
                raise BrokerError("bar high/low do not contain open/close")
            clean.append([t, o, h, l, c])
        with self._lock:
            self._bars[timeframe] = clean

    def take_orders(self) -> list[dict[str, Any]]:
        """Pending orders for the add-on to execute; each is handed out once."""
        with self._lock:
            pending = [o for o in self._orders if o["status"] == "pending"]
            for order in pending:
                order["status"] = "sent"
            return [dict(o) for o in pending]

    def order_result(self, payload: dict[str, Any]) -> None:
        order_id = _integer(payload.get("id"), "id")
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise BrokerError("ok must be true or false")
        message = payload.get("message", "")
        if not isinstance(message, str):
            raise BrokerError("message must be text")
        with self._lock:
            order = next((o for o in self._orders if o["id"] == order_id), None)
            if order is None:
                raise BrokerError("unknown order id")
            order["status"] = "done" if ok else "failed"
            order["message"] = message[:200]
            order["done_at"] = time.time()
            ticket = payload.get("ticket")
            if isinstance(ticket, int) and not isinstance(ticket, bool) and ticket > 0:
                order["ticket"] = ticket

    # --- from the chart page -------------------------------------------------------------------------------------

    def queue_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if payload.get("client") is None or payload.get("client") != self._controller or not self._controller_alive_locked():
                raise BrokerError("only the chart tab that controls the account may send orders")
            if not self._linked_locked():
                raise BrokerError("MT5 is not linked (no fresh account report)")
            mode = self._account["mode"]
            if mode == "CONTEST" or (mode == "REAL" and payload.get("allow_real") is not True):
                raise BrokerError(f"MT5 is logged in to a {mode} account; real-money orders need the chart page set to Real account")
            if sum(o["status"] in ("pending", "sent") for o in self._orders) >= MAX_PENDING_ORDERS:
                raise BrokerError("too many orders waiting for MT5")
        action = payload.get("action")
        order: dict[str, Any] = {"action": action, "side": "", "volume": 0.0, "sl": 0.0, "tp": 0.0, "ticket": 0,
                                 "sl_distance": 0.0, "tp_distance": 0.0}
        if action == "open":
            if payload.get("side") not in SIDES:
                raise BrokerError("side must be BUY or SELL")
            volume = _number(payload.get("volume"), "volume", maximum=MAX_VOLUME)
            if abs(volume * 100 - round(volume * 100)) > 1e-6:
                raise BrokerError("volume must be in 0.01-lot steps")
            order.update(side=payload["side"], volume=round(volume, 2),
                         sl_distance=_number(payload.get("sl_distance"), "sl_distance", maximum=500),
                         tp_distance=_number(payload.get("tp_distance"), "tp_distance", maximum=1000))
        elif action == "modify":
            order.update(ticket=_integer(payload.get("ticket"), "ticket"),
                         sl=_number(payload.get("sl"), "sl"), tp=_number(payload.get("tp"), "tp"))
        elif action == "close":
            order.update(ticket=_integer(payload.get("ticket"), "ticket"))
        else:
            raise BrokerError("action must be open, modify or close")
        with self._lock:
            order.update(id=next(self._ids), status="pending", message="", queued_at=time.time())
            self._orders.append(order)
            del self._orders[:-100]  # keep the recent history only
            return dict(order)

    def status(self, client: str | None = None) -> dict[str, Any]:
        """A polling chart tab (client) takes control if no other tab has polled within LINK_MAX_AGE_SECONDS."""
        with self._lock:
            if client is not None and (client == self._controller or not self._controller_alive_locked()):
                self._controller = client
                self._controller_seen = time.monotonic()
            return {
                "controller": client is not None and client == self._controller,
                "linked": self._linked_locked(),
                "account": dict(self._account) if self._account else None,
                "account_at": self._account_wall,
                "bars": sorted(self._bars),
                "orders": [dict(o) for o in self._orders[-20:]],
            }

    def bars(self, timeframe: str) -> list[list[float]] | None:
        with self._lock:
            return self._bars.get(timeframe)

    def _controller_alive_locked(self) -> bool:
        return self._controller is not None and time.monotonic() - self._controller_seen <= LINK_MAX_AGE_SECONDS

    def _linked_locked(self) -> bool:
        return self._account is not None and time.monotonic() - self._account_at <= LINK_MAX_AGE_SECONDS


def order_line(order: dict[str, Any]) -> str:
    """One order as a plain-text line for the MQL5 add-on, which has no JSON parser."""
    return "|".join(str(order[k]) for k in ("id", "action", "side", "volume", "sl", "tp", "ticket", "sl_distance", "tp_distance"))
