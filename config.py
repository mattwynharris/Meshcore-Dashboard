# ============================================================
# MeshCore Repeater Dashboard - Configuration
# ============================================================
# Settings can be edited here OR from the web dashboard's
# Settings page. Web-based changes are saved to settings.json
# and override values in this file.
# ============================================================

import json
import os
from pathlib import Path

_SETTINGS_FILE = Path(__file__).parent / "settings.json"

# --- Defaults (used if settings.json doesn't exist yet) ---

_DEFAULTS = {
    "companion_host": "192.168.1.XXX",
    "companion_port": 5000,
    "repeaters": [
        # {"name": "Repeater 1", "pubkey": "PASTE_PUBKEY_HERE"},
    ],
    "poll_interval_seconds": 120,
    "stagger_delay_seconds": 15,
    "stale_threshold_seconds": 900,
    "low_battery_percent": 20,
    "log_retention_hours": 24,
}

# --- History (not editable from web UI) ---
ENABLE_HISTORY = True
HISTORY_DB = "repeater_history.db"


def _load_settings() -> dict:
    """Load settings from settings.json, falling back to defaults."""
    if _SETTINGS_FILE.exists():
        try:
            with open(_SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            merged = {**_DEFAULTS, **saved}
            return merged
        except (json.JSONDecodeError, IOError) as e:
            print(f"[config] Error reading {_SETTINGS_FILE}: {e}, using defaults")
    return dict(_DEFAULTS)


def save_settings(settings: dict):
    """Save settings to settings.json."""
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def get_settings() -> dict:
    """Get current settings."""
    return _load_settings()


# --- Module-level convenience accessors ---
# These read from settings.json every time so they pick up web UI changes.

def get_companion_host() -> str:
    return _load_settings()["companion_host"]

def get_companion_port() -> int:
    return _load_settings()["companion_port"]

def get_repeaters() -> list:
    return _load_settings()["repeaters"]

def get_poll_interval() -> int:
    return _load_settings()["poll_interval_seconds"]

def get_stagger_delay() -> int:
    return _load_settings()["stagger_delay_seconds"]

def get_stale_threshold() -> int:
    return _load_settings()["stale_threshold_seconds"]

def get_low_battery_percent() -> int:
    return _load_settings().get("low_battery_percent", 20)

def get_log_retention_hours() -> int:
    return _load_settings().get("log_retention_hours", 24)


# Backwards-compatible constants (used by data_store.py at import time)
_s = _load_settings()
COMPANION_HOST = _s["companion_host"]
COMPANION_PORT = _s["companion_port"]
REPEATERS = _s["repeaters"]
POLL_INTERVAL_SECONDS = _s["poll_interval_seconds"]
STAGGER_DELAY_SECONDS = _s["stagger_delay_seconds"]
STALE_THRESHOLD_SECONDS = _s["stale_threshold_seconds"]
