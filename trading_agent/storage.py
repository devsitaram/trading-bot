from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventStore:
    """Low-frequency durable event store; quote processing stays in memory."""

    def __init__(self, path: str | Path = "trading_agent.sqlite3") -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_kind_created
                ON events(kind, created_at);
            """
        )
        self.connection.commit()

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events(created_at, kind, payload) VALUES (?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                kind,
                json.dumps(payload, sort_keys=True, default=str),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
