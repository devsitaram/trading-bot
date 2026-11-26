from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

EVENT_KINDS = {"open", "close", "level", "pause", "resume"}


class PaperLedger:
    """The paper bot's saved account: trades already made are never recalculated, so a reload cannot change them."""

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, json TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, json TEXT NOT NULL)")

    def read(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT version, json FROM state").fetchone()
            events = self._db.execute("SELECT json FROM events ORDER BY id").fetchall()
        return {
            "version": row[0] if row else 0,
            "state": json.loads(row[1]) if row else None,
            "events": [json.loads(e[0]) for e in events],
        }

    def save(self, version: int, state: dict[str, Any]) -> int | None:
        """Saves only if nothing else saved since `version`; returns the new version, or None if another tab got there first."""
        text = json.dumps(state)
        with self._lock, self._db:
            if version == 0:
                inserted = self._db.execute("INSERT OR IGNORE INTO state (id, version, json) VALUES (1, 1, ?)", (text,))
                return 1 if inserted.rowcount else None
            updated = self._db.execute("UPDATE state SET version = version + 1, json = ? WHERE version = ?", (text, version))
            return version + 1 if updated.rowcount else None

    def add_event(self, event: dict[str, Any]) -> None:
        """A manual action; replayed until its candle closes and is saved into the state."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO events (json) VALUES (?)", (json.dumps(event),))
