import time
import sqlite3
import logging
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import config as cfg


@dataclass
class RepeaterState:
    name: str = ""
    pubkey: str = ""
    battery_mv: int = 0
    battery_voltage: float = 0.0
    rssi: int = 0
    snr: float = 0.0
    noise_floor: int = 0
    uptime_seconds: int = 0
    packets_recv: int = 0
    packets_sent: int = 0
    hops: int = 0
    route_path: str = ""
    last_seen_epoch: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["online"] = self.is_online
        d["pubkey_short"] = self.pubkey[:12] if self.pubkey else ""
        return d

    @property
    def is_online(self) -> bool:
        if self.last_seen_epoch == 0:
            return False
        return (time.time() - self.last_seen_epoch) < cfg.get_stale_threshold()


class SQLiteLogHandler(logging.Handler):
    """Logging handler that writes log records to the activity_log SQLite table."""

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

    def emit(self, record: logging.LogRecord):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO activity_log (timestamp, level, logger_name, message) "
                "VALUES (?, ?, ?, ?)",
                (record.created, record.levelname, record.name, self.format(record)),
            )
            conn.commit()
            conn.close()
        except Exception:
            self.handleError(record)


class DataStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._repeaters: Dict[str, RepeaterState] = {}
        self._db_path = cfg.HISTORY_DB if cfg.ENABLE_HISTORY else None
        if self._db_path:
            self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                pubkey TEXT NOT NULL,
                name TEXT,
                battery_mv INTEGER,
                battery_voltage REAL,
                rssi INTEGER,
                snr REAL,
                uptime_seconds INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_pubkey_ts
            ON telemetry_log (pubkey, timestamp)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                level TEXT NOT NULL,
                logger_name TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_log_ts
            ON activity_log (timestamp)
        """)
        conn.commit()
        conn.close()

    def init_repeater(self, pubkey: str, name: str):
        """Register a repeater from config. Called at startup."""
        with self._lock:
            if pubkey not in self._repeaters:
                self._repeaters[pubkey] = RepeaterState(name=name, pubkey=pubkey)
            else:
                # Update name if it changed in settings
                self._repeaters[pubkey].name = name

    def remove_repeater(self, pubkey: str):
        """Remove a repeater from the live store (when deleted from settings)."""
        with self._lock:
            self._repeaters.pop(pubkey, None)

    def sync_repeaters(self, configured: list):
        """Sync store with configured repeater list. Add new, remove stale."""
        configured_keys = {r["pubkey"] for r in configured}
        with self._lock:
            # Remove repeaters no longer in config
            for pk in list(self._repeaters.keys()):
                if pk not in configured_keys:
                    del self._repeaters[pk]
        # Add/update configured ones
        for r in configured:
            self.init_repeater(r["pubkey"], r["name"])

    def update_hops(self, pubkey: str, hops: int):
        """Update hop count without touching last_seen."""
        with self._lock:
            if pubkey in self._repeaters:
                self._repeaters[pubkey].hops = hops

    def update_route(self, pubkey: str, hops: int, route_path: str):
        """Update hop count and route path without touching last_seen."""
        with self._lock:
            if pubkey in self._repeaters:
                self._repeaters[pubkey].hops = hops
                self._repeaters[pubkey].route_path = route_path

    def update_repeater(self, pubkey: str, **kwargs):
        """Update a repeater's state with new data from a poll response."""
        with self._lock:
            if pubkey not in self._repeaters:
                self._repeaters[pubkey] = RepeaterState(pubkey=pubkey)

            r = self._repeaters[pubkey]
            for k, v in kwargs.items():
                if hasattr(r, k) and v is not None:
                    setattr(r, k, v)
            r.last_seen_epoch = time.time()

        if self._db_path:
            self._log_to_db(pubkey)

    def _log_to_db(self, pubkey: str):
        with self._lock:
            r = self._repeaters.get(pubkey)
            if not r:
                return
            # Snapshot values under lock
            row = (
                time.time(), r.pubkey, r.name, r.battery_mv,
                r.battery_voltage, r.rssi, r.snr, r.uptime_seconds,
            )

        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO telemetry_log "
                "(timestamp, pubkey, name, battery_mv, battery_voltage, rssi, snr, uptime_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] DB write error: {e}")

    def get_all(self) -> List[dict]:
        """Return all repeater states as a JSON-serializable list."""
        with self._lock:
            return [r.to_dict() for r in self._repeaters.values()]

    def get_history(self, pubkey: str, hours: int = 24) -> List[dict]:
        """Return historical telemetry for a repeater over the last N hours."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT timestamp, battery_mv, battery_voltage, rssi, snr, uptime_seconds "
                "FROM telemetry_log "
                "WHERE pubkey = ? AND timestamp > ? "
                "ORDER BY timestamp",
                (pubkey, since),
            ).fetchall()
            conn.close()
            return [
                {
                    "ts": row[0],
                    "battery_mv": row[1],
                    "battery_v": row[2],
                    "rssi": row[3],
                    "snr": row[4],
                    "uptime": row[5],
                }
                for row in rows
            ]
        except Exception as e:
            print(f"[DataStore] DB read error: {e}")
            return []

    def get_log_handler(self) -> logging.Handler:
        """Return a logging handler that writes to the activity_log table."""
        if not self._db_path:
            return logging.NullHandler()
        handler = SQLiteLogHandler(self._db_path)
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def get_activity_logs(self, hours: int = 24, level: str = None, limit: int = 500) -> list:
        """Return recent activity log entries."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            if level:
                rows = conn.execute(
                    "SELECT id, timestamp, level, logger_name, message "
                    "FROM activity_log "
                    "WHERE timestamp > ? AND level = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (since, level.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, timestamp, level, logger_name, message "
                    "FROM activity_log "
                    "WHERE timestamp > ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
            conn.close()
            return [
                {
                    "id": row[0],
                    "ts": row[1],
                    "level": row[2],
                    "logger": row[3],
                    "message": row[4],
                }
                for row in rows
            ]
        except Exception as e:
            print(f"[DataStore] Activity log read error: {e}")
            return []

    def prune_activity_logs(self, retention_hours: int):
        """Delete activity log entries older than retention_hours."""
        if not self._db_path:
            return
        cutoff = time.time() - (retention_hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM activity_log WHERE timestamp < ?", (cutoff,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Activity log prune error: {e}")
