"""What MetaTrader 5 on this Mac is doing, read from its own log files, so the chart page can show which connection
step is missing (logged in? JustMarketsBridge running? WebRequest allowed?) instead of a blank panel."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

MT5_DATA = (Path.home() / "Library" / "Application Support" / "net.metaquotes.wine.metatrader5"
            / "drive_c" / "Program Files" / "MetaTrader 5")

LOGIN_OK = re.compile(r"'(\d+)': authorized on (.+?) through")
LOGIN_FAILED = re.compile(r"'(\d+)': authorization on (.+?) failed \((.+?)\)")
NO_CONNECTION = re.compile(r"'(\d+)': no connection to (.+)")
BRIDGE = "JustMarketsBridge"


def _messages(text: str) -> list[tuple[str, str, str]]:
    """(time, source, message) of each log line; for the terminal log only those since MT5 last started."""
    lines = []
    for raw in text.splitlines():
        parts = raw.lstrip("﻿").split("\t")
        if len(parts) >= 5:
            time, source, message = parts[2], parts[3], "\t".join(parts[4:]).strip()
            if source == "Terminal" and " started for " in message:
                lines = []  # a restart: older lines describe a previous session
            lines.append((time, source, message))
    return lines


def parse_logs(terminal_log: str, experts_log: str) -> dict[str, Any]:
    status: dict[str, Any] = {"login": None, "login_error": None, "bridge": None, "bridge_error": None, "webrequest_error": None}
    session = _messages(terminal_log)
    started = session[0][0] if session else ""  # same-day "HH:MM:SS.mmm" times compare as text
    for _time, source, message in session:
        if source == "Network":
            if m := LOGIN_OK.search(message):
                status["login"], status["login_error"] = {"account": int(m[1]), "server": m[2]}, None
            elif m := LOGIN_FAILED.search(message):
                status["login"], status["login_error"] = None, f"Login {m[1]} on {m[2]} failed: {m[3]}"
            elif m := NO_CONNECTION.search(message):
                status["login"], status["login_error"] = None, f"Login {m[1]}: no connection to server {m[2]}"
        elif source == "Experts" and BRIDGE in message:
            if "loaded successfully" in message:
                status["bridge"], status["bridge_error"] = "running", None
            elif "failed" in message:
                status["bridge"] = "failed"
            elif "removed" in message and status["bridge"] != "failed":
                status["bridge"] = "stopped"
    for time, _source, message in _messages(experts_log):
        if time < started:
            continue  # written by the add-on in an earlier MT5 session
        if "Cannot reach" in message:
            status["webrequest_error"] = message.split("JustMarketsBridge", 1)[-1].strip(": ")
        elif "linked" in message and "account" in message:
            status["webrequest_error"] = None
        elif "Alert:" in message and BRIDGE in message:
            status["bridge_error"] = message.split("Alert:", 1)[1].strip()
    return status


def read_status(data_dir: Path = MT5_DATA) -> dict[str, Any]:
    """Status from today's (newest) MT5 terminal and Experts logs; empty status if MT5 has no logs yet."""
    def newest(folder: Path) -> str:
        logs = sorted(folder.glob("[0-9]" * 8 + ".log"))
        if not logs:
            return ""
        try:
            return logs[-1].read_bytes().decode("utf-16-le", errors="replace")
        except OSError:
            return ""
    return parse_logs(newest(data_dir / "logs"), newest(data_dir / "MQL5" / "Logs"))
