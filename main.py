from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
from pathlib import Path
import plistlib
import re
import subprocess
import threading
import time
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from demo_broker import BrokerError, DemoBroker, order_line
from mt5_status import read_status as read_mt5_status
from paper_ledger import EVENT_KINDS, PaperLedger


PROJECT_ROOT = Path(__file__).resolve().parent
DASHBOARD_FILE = PROJECT_ROOT / "dashboard.html"
CHART_FILE = PROJECT_ROOT / "chart.html"
RULES_FILE = PROJECT_ROOT / "rules.json"
PAPER_DB_FILE = PROJECT_ROOT / "paper_bot.sqlite3"
PAGES = {
    "/": DASHBOARD_FILE,
    "/index.html": DASHBOARD_FILE,
    "/chart": CHART_FILE,
    "/practice": PROJECT_ROOT / "practice.html",
    "/trade": PROJECT_ROOT / "trade.html",
    "/rules.json": RULES_FILE,
}
QUOTE_MAX_AGE_SECONDS = 15
MAX_QUOTE_BODY_BYTES = 4096
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MAX_BROKER_BODY_BYTES = 512 * 1024  # 1,500 candles fit comfortably
MT5_APPS = (Path("/Applications/MetaTrader 5.app"), Path.home() / "Applications" / "MetaTrader 5.app")
MT5_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"
MT5_CHECK_SECONDS = 3  # short, so the page's connection checklist follows each step quickly


def find_metatrader(apps: tuple[Path, ...] = MT5_APPS) -> dict[str, Any]:
    """Is MetaTrader 5 installed (and running) on this Mac? MT5 updates itself when opened, so the installed version
    is reported as found rather than compared with an online list."""
    for app in apps:
        if not app.is_dir():
            continue
        try:
            version = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException):
            version = None
        try:
            running = subprocess.run(["pgrep", "-f", "terminal64.exe"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            running = False
        # Login, add-on and WebRequest status from MT5's own logs (only meaningful while it runs).
        logs = read_mt5_status() if running else {}
        return {"installed": True, "app": str(app), "version": version, "running": running, "download": MT5_DOWNLOAD_URL, **logs}
    return {"installed": False, "app": None, "version": None, "running": False, "download": MT5_DOWNLOAD_URL}
PAGE_ORIGINS = ("http://127.0.0.1:8765", "http://localhost:8765")
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_SECONDS = 3600
NEWS_RETRY_SECONDS = 300  # the feed answers 429 Too Many Requests when polled often


class NewsCalendar:
    """High-impact events from the free Forex Factory weekly calendar, fetched at most once per hour."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, str]] | None = None
        self._fetched_at = 0.0
        self._failed_at = float("-inf")

    def high_impact(self) -> list[dict[str, str]]:
        with self._lock:
            now = time.monotonic()
            stale = self._events is None or now - self._fetched_at > NEWS_CACHE_SECONDS
            if stale and now - self._failed_at > NEWS_RETRY_SECONDS:
                try:
                    request = Request(NEWS_URL, headers={"User-Agent": "TradingAgent/1.0"})
                    with urlopen(request, timeout=10) as response:
                        calendar = json.load(response)
                    self._events = [
                        {"time": event["date"], "title": event["title"], "currency": event["country"]}
                        for event in calendar
                        if event.get("impact") == "High"
                    ]
                    self._fetched_at = now
                except (OSError, ValueError, KeyError, TypeError):
                    self._failed_at = now  # keep serving the last good calendar, if any
            if self._events is None:
                raise OSError("News calendar not loaded yet")
            return self._events


class QuoteFeed:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, dict[str, float | int | str]] = {}
        self._received_at: dict[str, float] = {}

    def publish(
        self,
        *,
        symbol: str,
        bid: float,
        ask: float,
        timestamp_ms: int,
    ) -> bool:
        current_time_ms = int(time.time() * 1000)
        allowed_clock_skew_ms = QUOTE_MAX_AGE_SECONDS * 1000
        if (
            timestamp_ms < current_time_ms - allowed_clock_skew_ms
            or timestamp_ms > current_time_ms + allowed_clock_skew_ms
        ):
            return False

        with self._lock:
            previous = self._quotes.get(symbol)
            if (
                previous
                and previous["timestamp_ms"] == timestamp_ms
                and previous["bid"] == bid
                and previous["ask"] == ask
            ):
                return True
            self._quotes[symbol] = {
                "symbol": symbol,
                "category": "MT5",
                "eligible": False,
                "session_status": "unknown",
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "signal_direction": "HOLD",
                "timestamp_ms": timestamp_ms,
            }
            self._received_at[symbol] = time.monotonic()
        return True

    def snapshot(self) -> list[dict[str, float | int | str]]:
        now = time.monotonic()
        with self._lock:
            return [
                quote.copy()
                for symbol, quote in self._quotes.items()
                if now - self._received_at[symbol] <= QUOTE_MAX_AGE_SECONDS
            ]


def dashboard_status(feed: QuoteFeed | None = None) -> dict[str, Any]:
    instruments = feed.snapshot() if feed else []
    connected = bool(instruments)
    return {
        "application": "Trading Agent",
        "application_status": "running",
        "broker": "JustMarkets",
        "broker_connected": connected,
        "account_mode": "UNKNOWN",
        "account_verified": False,
        "market_data": "available" if connected else "unavailable",
        "execution_enabled": False,
        "orders_submitted": False,
        "instruments": instruments,
        "positions": [],
        "message": (
            "Receiving read-only quotes from the local MT5 terminal. "
            "Account data is not connected and orders are disabled."
            if connected
            else "No MT5 quote bridge is connected. Start the read-only MT5 bridge to "
            "display prices. This dashboard does not read browser sessions or credentials."
        ),
    }


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    @property
    def dashboard_server(self) -> LocalDashboardServer:
        return cast(LocalDashboardServer, self.server)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            payload = json.dumps(
                dashboard_status(self.dashboard_server.quote_feed)
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/api/news":
            try:
                events = self.dashboard_server.news.high_impact()
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(502, "News calendar unavailable")
                return
            payload = json.dumps(events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        url = urlsplit(self.path)
        if url.path == "/api/paper":
            self._send_json(self.dashboard_server.paper.read())
            return
        if url.path == "/api/broker":
            client = parse_qs(url.query).get("client", [None])[0]
            if client is not None and not re.fullmatch(r"[a-z0-9]{8,40}", client):
                self.send_error(400, "Invalid client id")
                return
            self._send_json({**self.dashboard_server.broker.status(client), "mt5": self.dashboard_server.metatrader()})
            return
        if url.path == "/api/broker/bars":
            bars = self.dashboard_server.broker.bars(parse_qs(url.query).get("tf", [""])[0])
            if bars is None:
                self.send_error(404, "No MT5 candles for this timeframe yet")
                return
            self._send_json(bars)
            return
        if url.path == "/api/broker/orders":  # the MT5 add-on collects orders as plain-text lines
            payload = "\n".join(order_line(o) for o in self.dashboard_server.broker.take_orders()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path in PAGES:
            try:
                payload = PAGES[self.path].read_bytes()
            except OSError:
                self.send_error(500, "Page could not be read")
                return
            content_type = (
                "application/json" if PAGES[self.path].suffix == ".json" else "text/html"
            )
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(404, "Not found")

    def _send_json(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _broker_post(self) -> None:
        # Another website open in the browser must not be able to report accounts or send orders to this server.
        origin = self.headers.get("Origin")
        if self.client_address[0] != "127.0.0.1" or (origin is not None and origin not in PAGE_ORIGINS):
            self.send_error(403, "Only this Mac's chart page and MT5 add-on may call the broker link")
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415, "Content-Type must be application/json")
            return
        try:
            body_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            body_length = 0
        if not 0 < body_length <= MAX_BROKER_BODY_BYTES:
            self.send_error(413, "Payload size is invalid")
            return
        try:
            payload = json.loads(self.rfile.read(body_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "Payload must be valid UTF-8 JSON")
            return
        if not isinstance(payload, dict):
            self.send_error(400, "Payload must be a JSON object")
            return

        if self.path == "/api/paper/state":
            version, state = payload.get("version"), payload.get("state")
            if isinstance(version, bool) or not isinstance(version, int) or not isinstance(state, dict):
                self.send_error(400, "version and state are required")
                return
            saved = self.dashboard_server.paper.save(version, state)
            if saved is None:
                self._send_json({"error": "Another tab saved first"}, 409)
                return
            self._send_json({"version": saved})
            return
        if self.path == "/api/paper/events":
            if payload.get("kind") not in EVENT_KINDS or not _is_finite_number(payload.get("time")):
                self.send_error(400, "kind and time are required")
                return
            self.dashboard_server.paper.add_event(payload)
            self._send_json({}, 201)
            return

        broker = self.dashboard_server.broker
        try:
            if self.path == "/api/broker/account":
                broker.report_account(payload)
            elif self.path == "/api/broker/bars":
                broker.set_bars(payload)
            elif self.path == "/api/broker/result":
                broker.order_result(payload)
            elif self.path == "/api/broker/orders":
                self._send_json(broker.queue_order(payload), 201)
                return
            else:
                self.send_error(404, "Not found")
                return
        except BrokerError as error:
            self._send_json({"error": str(error)}, 400)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path.startswith(("/api/broker/", "/api/paper/")):
            self._broker_post()
            return
        if self.path != "/api/quotes":
            self.send_error(404, "Not found")
            return

        if self.client_address[0] != "127.0.0.1":
            self.send_error(403, "Quote updates are accepted only from this Mac")
            return

        if self.headers.get_content_type() != "application/json":
            self.send_error(415, "Content-Type must be application/json")
            return

        content_length = self.headers.get("Content-Length")
        try:
            body_length = int(content_length) if content_length is not None else 0
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if body_length < 1 or body_length > MAX_QUOTE_BODY_BYTES:
            self.send_error(413, "Quote payload size is invalid")
            return

        try:
            payload = json.loads(self.rfile.read(body_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "Quote payload must be valid UTF-8 JSON")
            return

        if not isinstance(payload, dict):
            self.send_error(400, "Quote payload must be a JSON object")
            return

        symbol = payload.get("symbol")
        bid = payload.get("bid")
        ask = payload.get("ask")
        timestamp_ms = payload.get("timestamp_ms")
        if (
            not isinstance(symbol, str)
            or not SYMBOL_PATTERN.fullmatch(symbol)
            or not _is_finite_number(bid)
            or bid <= 0
            or not _is_finite_number(ask)
            or ask < bid
            or isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms <= 0
        ):
            self.send_error(400, "Quote fields are invalid")
            return

        try:
            spread = ask - bid
        except OverflowError:
            self.send_error(400, "Quote fields are invalid")
            return
        if not _is_finite_number(spread):
            self.send_error(400, "Quote fields are invalid")
            return

        accepted = self.dashboard_server.quote_feed.publish(
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp_ms=timestamp_ms,
        )
        self.send_response(204 if accepted else 202)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if self.path.startswith("/api/broker") and str(args[1] if len(args) > 1 else "").startswith("2"):
            return  # the MT5 add-on and the page poll several times a second; only log problems
        print(f"[dashboard] {format % args}")


class LocalDashboardServer(ThreadingHTTPServer):
    def __init__(self, port: int = 8765, paper_db: Path | str = PAPER_DB_FILE) -> None:
        self.paper = PaperLedger(paper_db)
        self.quote_feed = QuoteFeed()
        self.news = NewsCalendar()
        self.broker = DemoBroker()
        self._mt5 = find_metatrader()
        self._mt5_at = time.monotonic()
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.daemon_threads = True

    def metatrader(self) -> dict[str, Any]:
        """The MetaTrader 5 check, refreshed at most every MT5_CHECK_SECONDS (the page polls twice a second)."""
        if time.monotonic() - self._mt5_at > MT5_CHECK_SECONDS:
            self._mt5, self._mt5_at = find_metatrader(), time.monotonic()
        return self._mt5


def main() -> None:
    server = LocalDashboardServer()
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"Trading Agent dashboard: {address}")
    print(f"Chart and bot: {address}/chart")
    mt5 = server.metatrader()
    if not mt5["installed"]:
        print(f"MetaTrader 5 is NOT installed on this Mac. Install it from {mt5['download']}, log in to your")
        print("JustMarkets account (demo or real) and attach mt5/JustMarketsBridge.mq5 to an XAUUSD chart (see README).")
    else:
        print(f"MetaTrader 5 {mt5['version'] or ''} found at {mt5['app']}"
              + (" (running)." if mt5["running"] else " (not running: open it and log in to your JustMarkets account)."))
        print("It updates itself when opened. The account links once JustMarketsBridge runs on an XAUUSD chart.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
