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
    lat: float = 0.0
    lon: float = 0.0
    fw_version: str = ""
    last_seen_epoch: float = 0.0
    last_poll_ok: Optional[bool] = None  # None = never polled, True = ok, False = timed out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["online"] = self.is_online
        d["poll_ok"] = self.last_poll_ok
        d["pubkey_short"] = self.pubkey[:12] if self.pubkey else ""
        return d

    @property
    def is_online(self) -> bool:
        # Only green when the last poll got a response; red only on explicit failure
        return self.last_poll_ok is True


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                direction TEXT NOT NULL,
                channel_idx INTEGER,
                sender_pubkey TEXT,
                sender_name TEXT,
                text TEXT NOT NULL,
                hops INTEGER DEFAULT -1,
                path TEXT DEFAULT '',
                ack_code TEXT DEFAULT '',
                acks INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_ts
            ON messages (timestamp)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS node_names (
                node_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS advert_nodes (
                pubkey TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL,
                lon REAL,
                last_seen REAL NOT NULL
            )
        """)
        # Migrate existing DB: add new columns if missing
        for col, definition in [
            ("hops", "INTEGER DEFAULT -1"),
            ("path", "TEXT DEFAULT ''"),
            ("ack_code", "TEXT DEFAULT ''"),
            ("acks", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Column already exists
        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_ack_code
                ON messages (ack_code) WHERE ack_code != ''
            """)
        except Exception:
            pass
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

    def reorder(self, pubkeys: list):
        """Reorder the in-memory repeaters dict to match the given pubkey order."""
        with self._lock:
            ordered = {pk: self._repeaters[pk] for pk in pubkeys if pk in self._repeaters}
            for pk, v in self._repeaters.items():
                if pk not in ordered:
                    ordered[pk] = v
            self._repeaters = ordered

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

    def get_route_by_prefix(self, pubkey_prefix: str) -> tuple:
        """Return (hops, route_path) for the first repeater whose pubkey starts with the given prefix.
        Returns (-1, '') if not found."""
        if not pubkey_prefix:
            return (-1, "")
        pre = pubkey_prefix.lower()
        with self._lock:
            for pk, state in self._repeaters.items():
                if pk.lower().startswith(pre) or pre.startswith(pk.lower()):
                    if state.route_path or state.hops >= 0:
                        return (state.hops, state.route_path)
        return (-1, "")

    def update_location(self, pubkey: str, lat: float, lon: float):
        """Update GPS coordinates without touching last_seen."""
        with self._lock:
            if pubkey in self._repeaters:
                self._repeaters[pubkey].lat = lat
                self._repeaters[pubkey].lon = lon

    def mark_poll_failed(self, pubkey: str):
        """Mark the last poll as failed (status request timed out)."""
        with self._lock:
            if pubkey in self._repeaters:
                self._repeaters[pubkey].last_poll_ok = False

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
            r.last_poll_ok = True

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

    def get_activity_logs(self, hours: int = 24, level: str = None, search: str = None, limit: int = 500) -> list:
        """Return recent activity log entries, optionally filtered by level and message text."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            where = "WHERE timestamp > ?"
            params: list = [since]
            if level:
                where += " AND level = ?"
                params.append(level.upper())
            if search:
                where += " AND message LIKE ?"
                params.append(f"%{search}%")
            params.append(limit)
            rows = conn.execute(
                f"SELECT id, timestamp, level, logger_name, message "
                f"FROM activity_log {where} "
                f"ORDER BY timestamp DESC LIMIT ?",
                params,
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

    def store_message(self, direction: str, channel_idx, sender_pubkey: str, sender_name: str, text: str,
                      hops: int = -1, path: str = "", ack_code: str = "") -> bool:
        """Store an incoming or outgoing message, skipping duplicates.
        Returns True if the message was new, False if it was a duplicate."""
        if not self._db_path:
            return True  # no DB — treat as new so callers still act on it
        try:
            conn = sqlite3.connect(self._db_path)
            since = time.time() - 300  # dedup window: 5 minutes
            existing = conn.execute(
                "SELECT id FROM messages WHERE direction=? AND channel_idx IS ? "
                "AND sender_pubkey=? AND text=? AND timestamp > ?",
                (direction, channel_idx, sender_pubkey or "", text, since),
            ).fetchone()
            if existing:
                conn.close()
                return False  # duplicate — skip
            conn.execute(
                "INSERT INTO messages "
                "(timestamp, direction, channel_idx, sender_pubkey, sender_name, text, hops, path, ack_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), direction, channel_idx, sender_pubkey or "", sender_name or "", text,
                 hops, path or "", ack_code or ""),
            )
            conn.commit()
            conn.close()
            return True  # new message
        except Exception as e:
            print(f"[DataStore] Message store error: {e}")
            return True  # on error, assume new so we don't silently drop notifications

    def increment_message_acks(self, ack_code: str) -> int:
        """Increment ack count for the outgoing message matching ack_code.
        Returns the new total ack count, or 0 if not found."""
        if not self._db_path or not ack_code:
            return 0
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "UPDATE messages SET acks = acks + 1 "
                "WHERE ack_code = ? AND direction = 'out'",
                (ack_code,),
            )
            row = conn.execute(
                "SELECT acks FROM messages WHERE ack_code = ? AND direction = 'out'",
                (ack_code,),
            ).fetchone()
            conn.commit()
            conn.close()
            return row[0] if row else 0
        except Exception as e:
            print(f"[DataStore] ACK update error: {e}")
            return 0

    def get_messages(self, channel_idx=None, hours: int = 48, limit: int = 200) -> list:
        """Return recent messages, optionally filtered by channel."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            if channel_idx is not None:
                rows = conn.execute(
                    "SELECT id, timestamp, direction, channel_idx, sender_pubkey, sender_name, "
                    "text, hops, path, acks, ack_code "
                    "FROM messages WHERE timestamp > ? AND channel_idx = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (since, channel_idx, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, timestamp, direction, channel_idx, sender_pubkey, sender_name, "
                    "text, hops, path, acks, ack_code "
                    "FROM messages WHERE timestamp > ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
            conn.close()
            return [
                {
                    "id": row[0],
                    "ts": row[1],
                    "direction": row[2],
                    "channel_idx": row[3],
                    "sender_pubkey": row[4],
                    "sender_name": row[5],
                    "text": row[6],
                    "hops": row[7] if row[7] is not None else -1,
                    "path": row[8] or "",
                    "acks": row[9] or 0,
                    "ack_code": row[10] or "",
                }
                for row in rows
            ]
        except Exception as e:
            print(f"[DataStore] Message read error: {e}")
            return []

    def upsert_advert_node(self, pubkey: str, name: str, lat: float = None, lon: float = None):
        """Upsert a node discovered via advert packet."""
        if not self._db_path or not pubkey or not name:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT INTO advert_nodes (pubkey, name, lat, lon, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(pubkey) DO UPDATE SET
                     name=excluded.name,
                     lat=COALESCE(excluded.lat, advert_nodes.lat),
                     lon=COALESCE(excluded.lon, advert_nodes.lon),
                     last_seen=excluded.last_seen""",
                (pubkey, name, lat, lon, time.time())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Advert node upsert error: {e}")

    def get_advert_nodes(self) -> list:
        """Return all advert-discovered nodes."""
        if not self._db_path:
            return []
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT pubkey, name, lat, lon, last_seen FROM advert_nodes ORDER BY last_seen DESC"
            ).fetchall()
            conn.close()
            return [{"pubkey": r[0], "name": r[1], "lat": r[2], "lon": r[3], "last_seen": r[4]} for r in rows]
        except Exception as e:
            print(f"[DataStore] Advert nodes read error: {e}")
            return []

    def load_node_names(self) -> dict:
        """Load persisted node ID → name cache from DB."""
        if not self._db_path:
            return {}
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute("SELECT node_id, name FROM node_names").fetchall()
            conn.close()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            print(f"[DataStore] Node names load error: {e}")
            return {}

    def save_node_names(self, cache: dict):
        """Persist node ID → name cache to DB (upsert all entries)."""
        if not self._db_path or not cache:
            return
        try:
            now = time.time()
            conn = sqlite3.connect(self._db_path)
            conn.executemany(
                "INSERT OR REPLACE INTO node_names (node_id, name, updated) VALUES (?, ?, ?)",
                [(node_id, name, now) for node_id, name in cache.items()]
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Node names save error: {e}")

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
