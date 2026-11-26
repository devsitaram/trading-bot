"""Used by link_justmarkets.command: turns the dashboard's /api/broker answer (read from stdin) into one line,
"DONE|<message for the user>" once the link succeeded or clearly failed, or "WAIT" while MT5 is still connecting."""

import json
import sys

ADD_SERVERS = ("MT5 does not know this server yet: File → Open an Account → search JustMarkets → "
               "click Just Global Markets → Cancel, then run link_justmarkets.command again.")


def result(status: dict) -> str:
    mt5, account = status.get("mt5", {}), status.get("account")
    if status.get("linked"):
        return (f"DONE|✅ Connected to {account['server']} #{account['login']} ({account['mode']}), "
                f"balance {account['balance']:.2f} {account['currency']}. The bot is linked.")
    if mt5.get("login_error"):
        on_justmarkets = "justmarkets" in mt5["login_error"].lower()
        hint = "Wrong account number or MT5 trading password?" if "Invalid account" in mt5["login_error"] and on_justmarkets else ADD_SERVERS
        return f"DONE|❌ {mt5['login_error']}.\n\n{hint}"
    if mt5.get("webrequest_error"):
        return ("DONE|⚠️ Logged in, but MT5 blocks the connection to the bot. In MT5: Tools → Options → Expert Advisors → "
                "tick Allow WebRequest for listed URL → add http://127.0.0.1:8765.")
    if mt5.get("bridge") in ("failed", "stopped"):
        return f"DONE|⚠️ JustMarketsBridge stopped: {mt5.get('bridge_error') or mt5['bridge']}"
    return "WAIT"


if __name__ == "__main__":
    print(result(json.load(sys.stdin)))
