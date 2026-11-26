"""Milestone 1: connect to MT5 and print account and symbol info. Places no orders."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt5bot.mt5_client import MT5Client, MT5Error

HERE = Path(__file__).resolve().parent


def main() -> int:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("The MetaTrader5 package is missing. It installs only on Windows: pip install -r mt5bot/requirements.txt")
        return 1
    from dotenv import load_dotenv
    import yaml

    load_dotenv(HERE / ".env")
    config = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    try:
        login, password, server = int(os.environ["MT5_LOGIN"]), os.environ["MT5_PASSWORD"], os.environ["MT5_SERVER"]
    except (KeyError, ValueError):
        print("Set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in mt5bot/.env (see .env.example).")
        return 1

    client = MT5Client(mt5)
    try:
        client.connect(login, password, server, config["connect_attempts"], config["connect_backoff_seconds"])
        try:
            for title, info in (("Account", client.account()), ("Symbol", client.symbol(config["symbol"]))):
                print(title)
                for key, value in info.items():
                    print(f"  {key}: {value}")
        finally:
            client.shutdown()
    except MT5Error as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
