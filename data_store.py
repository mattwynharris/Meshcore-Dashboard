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
    temperature_c: float = 0.0
    last_seen_epoch: float = 0.0
    last_poll_ok: Optional[bool] = None  # None = never polled, True = ok, False = timed out
    clock_offset_s: Optional[float] = None  # seconds difference: repeater clock − system clock

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
                uptime_seconds INTEGER,
                temperature_c REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_pubkey_ts
            ON telemetry_log (pubkey, timestamp)
        """)
        # Migration: add new columns if missing
        for _col, _def in [("temperature_c", "REAL"), ("noise_floor", "INTEGER")]:
            try:
                conn.execute(f"ALTER TABLE telemetry_log ADD COLUMN {_col} {_def}")
            except Exception:
                pass  # Column already exists

        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                pubkey TEXT NOT NULL,
                online INTEGER NOT NULL,
                latency_ms INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_poll_log_pubkey_ts
            ON poll_log (pubkey, timestamp)
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
        # Add first_seen to advert_nodes (set once on INSERT, never overwritten)
        try:
            conn.execute("ALTER TABLE advert_nodes ADD COLUMN first_seen REAL")
        except Exception:
            pass  # column already exists
        # One-time GPS scale migration: fix advert_nodes that were stored with /1e7 instead of /1e6
        # Detectable: NZ lat should be ~-34 to -47, lon ~166-178. Wrong values are ~-3.4 to -4.7, 16.6-17.8
        try:
            conn.execute("""
                UPDATE advert_nodes
                SET lat = lat * 10, lon = lon * 10
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND abs(lat) < 10 AND abs(lon) < 20
                  AND abs(lat * 10) BETWEEN 10 AND 90
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
            pk = pubkey.lower()
            # Case-insensitive lookup
            key = next((k for k in self._repeaters if k.lower() == pk), None)
            if key:
                self._repeaters[key].hops = hops
                self._repeaters[key].route_path = route_path

    def clear_all_routes(self):
        """Clear stored hops and route_path on every repeater."""
        with self._lock:
            for state in self._repeaters.values():
                state.hops = -1
                state.route_path = ""

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
        self.log_poll(pubkey, online=False)

    def log_poll(self, pubkey: str, online: bool, latency_ms: int = None):
        """Record a poll result (success/failure + latency) to poll_log."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO poll_log (timestamp, pubkey, online, latency_ms) VALUES (?, ?, ?, ?)",
                (time.time(), pubkey, 1 if online else 0, latency_ms),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] poll_log write error: {e}")

    def get_poll_history(self, pubkey: str, hours: int = 24) -> list:
        """Return poll history for a repeater: [{ts, online, latency_ms}]."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT timestamp, online, latency_ms FROM poll_log "
                "WHERE pubkey = ? AND timestamp > ? ORDER BY timestamp",
                (pubkey, since),
            ).fetchall()
            conn.close()
            return [{"ts": r[0], "online": bool(r[1]), "latency_ms": r[2]} for r in rows]
        except Exception as e:
            print(f"[DataStore] poll_log read error: {e}")
            return []

    def log_cli(self, name: str, cmd: str, response: str = None):
        """Write a CLI-level entry to the activity_log table."""
        msg = f"CMD: {cmd}"
        if response:
            msg += f" → {response[:200]}"
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO activity_log (timestamp, level, logger_name, message) VALUES (?, ?, ?, ?)",
                    (time.time(), "CLI", name, msg),
                )

    def log_alert(self, name: str, message: str):
        """Write an ALERT-level entry directly to the activity_log table."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO activity_log (timestamp, level, logger_name, message) VALUES (?, ?, ?, ?)",
                (time.time(), "ALERT", name, message),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] alert log error: {e}")

    def update_repeater(self, pubkey: str, latency_ms: int = None, **kwargs):
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
        self.log_poll(pubkey, online=True, latency_ms=latency_ms)

    def _log_to_db(self, pubkey: str):
        with self._lock:
            r = self._repeaters.get(pubkey)
            if not r:
                return
            # Snapshot values under lock
            row = (
                time.time(), r.pubkey, r.name, r.battery_mv,
                r.battery_voltage, r.rssi, r.snr, r.uptime_seconds,
                r.temperature_c or None, r.noise_floor or None,
            )

        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO telemetry_log "
                "(timestamp, pubkey, name, battery_mv, battery_voltage, rssi, snr, uptime_seconds, temperature_c, noise_floor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] DB write error: {e}")

    def log_companion_battery(self, battery_mv: int):
        """Log a companion battery reading to the telemetry_log table."""
        if not self._db_path or not battery_mv:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO telemetry_log (timestamp, pubkey, name, battery_mv) VALUES (?, ?, ?, ?)",
                (time.time(), "__companion__", "Companion", battery_mv),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] companion battery log error: {e}")

    def get_companion_history(self, hours: int = 24) -> List[dict]:
        """Return companion battery history over the last N hours."""
        if not self._db_path:
            return []
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT timestamp, battery_mv FROM telemetry_log "
                "WHERE pubkey = '__companion__' AND timestamp > ? ORDER BY timestamp",
                (since,),
            ).fetchall()
            conn.close()
            return [{"ts": row[0], "battery_mv": row[1]} for row in rows]
        except Exception as e:
            print(f"[DataStore] companion history error: {e}")
            return []

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
                "SELECT timestamp, battery_mv, battery_voltage, rssi, snr, uptime_seconds, temperature_c, noise_floor "
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
                    "temperature_c": row[6],
                    "noise_floor": row[7],
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

    def count_messages(self, hours: int = 24) -> int:
        """Return the number of messages received in the last N hours."""
        if not self._db_path:
            return 0
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute("SELECT COUNT(*) FROM messages WHERE timestamp > ?", (since,)).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    def upsert_advert_node(self, pubkey: str, name: str, lat: float = None, lon: float = None):
        """Upsert a node discovered via advert packet."""
        if not self._db_path or not pubkey or not name:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            now = time.time()
            conn.execute(
                """INSERT INTO advert_nodes (pubkey, name, lat, lon, last_seen, first_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pubkey) DO UPDATE SET
                     name=excluded.name,
                     lat=COALESCE(excluded.lat, advert_nodes.lat),
                     lon=COALESCE(excluded.lon, advert_nodes.lon),
                     last_seen=excluded.last_seen""",
                (pubkey, name, lat, lon, now, now)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Advert node upsert error: {e}")

    def clear_all_advert_nodes(self):
        """Delete every advert node from the DB."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM advert_nodes")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Advert nodes clear error: {e}")

    def delete_advert_node(self, pubkey: str):
        """Remove an advert node from the DB by pubkey prefix match."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM advert_nodes WHERE pubkey LIKE ?", (pubkey.lower() + '%',))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DataStore] Advert node delete error: {e}")

    def get_advert_nodes(self) -> list:
        """Return all advert-discovered nodes."""
        if not self._db_path:
            return []
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT pubkey, name, lat, lon, last_seen, first_seen FROM advert_nodes ORDER BY last_seen DESC"
            ).fetchall()
            conn.close()
            return [{"pubkey": r[0], "name": r[1], "lat": r[2], "lon": r[3], "last_seen": r[4], "first_seen": r[5]} for r in rows]
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
