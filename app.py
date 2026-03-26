import asyncio
import json
import logging
import os
import signal
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config as cfg
from data_store import DataStore
from meshcore_poller import MeshcorePoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = DataStore()
poller = MeshcorePoller(store)

# Attach SQLite log handler to capture poller activity
_log_handler = store.get_log_handler()
logging.getLogger().addHandler(_log_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller.start())
    logger.info("MeshCore poller started")

    async def prune_logs_periodically():
        while True:
            await asyncio.sleep(3600)
            retention = cfg.get_log_retention_hours()
            store.prune_activity_logs(retention)

    prune_task = asyncio.create_task(prune_logs_periodically())

    yield

    prune_task.cancel()
    await poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await prune_task
    except asyncio.CancelledError:
        pass
    logger.info("MeshCore poller stopped")


app = FastAPI(title="MeshCore Repeater Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "dashboard.html"
    return html_path.read_text()


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    return templates.TemplateResponse("messages.html", {"request": request})


@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    return templates.TemplateResponse("map.html", {"request": request})


@app.get("/packets", response_class=HTMLResponse)
async def packets_page(request: Request):
    return templates.TemplateResponse("packets.html", {"request": request})


# --- Repeater Data API ---

@app.get("/api/repeaters")
async def get_repeaters():
    return store.get_all()


@app.get("/api/node-names")
async def get_node_names():
    """Return the 2-char node ID → name cache for path hop resolution."""
    return poller._node_id_name_cache if poller else {}


@app.get("/api/contact-routes")
async def get_contact_routes():
    """Return cached contact routes (pubkey_prefix → {hops, path}) for all contacts."""
    if not poller:
        return {}
    return {k: {"hops": v[0], "path": v[1]} for k, v in poller._contact_routes.items()}


@app.get("/api/map")
async def get_map_data():
    """Return repeater data optimised for the map page, including home node location."""
    repeaters = store.get_all()
    home = {"lat": 0.0, "lon": 0.0, "name": "Gateway"}
    if poller and poller.mc and hasattr(poller.mc, "self_info") and poller.mc.self_info:
        si = poller.mc.self_info
        home["lat"] = si.get("adv_lat", 0.0) or 0.0
        home["lon"] = si.get("adv_lon", 0.0) or 0.0
    # Fall back to manually saved home location if device has no GPS
    if not home["lat"] and not home["lon"]:
        s = cfg.get_settings()
        home["lat"] = s.get("home_lat", 0.0) or 0.0
        home["lon"] = s.get("home_lon", 0.0) or 0.0

    # Include all mesh contacts for neighbour discovery on the map
    mesh_contacts = []
    if poller:
        try:
            mesh_contacts = poller.get_mesh_contacts()
        except Exception:
            pass
    configured_pubkeys = {r["pubkey"] for r in repeaters}
    for c in mesh_contacts:
        c["configured"] = c["pubkey"] in configured_pubkeys

    # Advert-discovered nodes heard via RF (may include foreign repeaters)
    advert_nodes = store.get_advert_nodes()
    # Tag each as configured if pubkey matches a dashboard repeater
    for n in advert_nodes:
        n["configured"] = any(
            n["pubkey"] == pk or n["pubkey"].startswith(pk) or pk.startswith(n["pubkey"])
            for pk in configured_pubkeys
        )

    return {"home": home, "repeaters": repeaters, "contacts": mesh_contacts, "advert_nodes": advert_nodes}


@app.post("/api/home")
async def set_home_location(request: Request):
    """Save a manually placed home/gateway location."""
    body = await request.json()
    try:
        lat = float(body.get("lat", 0.0))
        lon = float(body.get("lon", 0.0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "lat and lon must be numbers"}
    s = cfg.get_settings()
    s["home_lat"] = lat
    s["home_lon"] = lon
    cfg.save_settings(s)
    logger.info(f"Home location set to {lat:.6f}, {lon:.6f}")
    return {"ok": True}


@app.get("/api/history/{pubkey}")
async def get_history(pubkey: str, hours: int = 24):
    return store.get_history(pubkey, hours)


@app.get("/api/logs")
async def get_logs(hours: int = 24, level: str = None, search: str = None, limit: int = 500):
    """Return recent activity logs, optionally filtered by level and/or message text."""
    return store.get_activity_logs(hours=hours, level=level, search=search, limit=limit)


@app.get("/api/stream")
async def event_stream():
    async def generate():
        while True:
            data = json.dumps(store.get_all())
            yield f"event: update\ndata: {data}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Connection API ---

@app.get("/api/channels")
async def get_device_channels():
    """Return channels fetched from the companion device, falling back to settings."""
    device_chs = poller.device_channels
    if device_chs:
        return device_chs
    return cfg.get_channels()


@app.get("/api/connection")
async def get_connection():
    """Return current connection status."""
    result = {
        "connected": poller.is_connected,
        "host": cfg.get_companion_host(),
        "port": cfg.get_companion_port(),
    }
    # Companion device battery — prefer telemetry (analog ch 1), fall back to self_info
    bat = poller._companion_battery_mv or 0
    if not bat and poller.mc and hasattr(poller.mc, "self_info") and poller.mc.self_info:
        si = poller.mc.self_info
        bat = si.get("bat", 0) or si.get("bat_mv", 0) or si.get("battery", 0) or si.get("battery_mv", 0) or 0
    if bat > 0:
        result["battery_mv"] = bat
    result["polling_enabled"] = poller._polling_enabled
    result["last_connected"] = poller._last_connected_ts
    return result


@app.post("/api/polling/toggle")
async def toggle_polling():
    """Toggle duty-cycle repeater polling on or off."""
    enabled = poller.toggle_polling()
    return {"ok": True, "polling_enabled": enabled}


@app.post("/api/disconnect")
async def disconnect_companion():
    """Disconnect from companion device and stay disconnected."""
    poller.manual_disconnect()
    logger.info("Manual disconnect requested")
    return {"ok": True}


@app.post("/api/connect")
async def connect_companion():
    """Reconnect to companion device."""
    poller.request_reconnect()
    logger.info("Manual connect requested")
    return {"ok": True}


# --- Messages API ---

@app.get("/api/packets")
async def get_packets(limit: int = 100):
    """Return recent mesh packet events (messages, ACKs, path updates)."""
    return poller.get_recent_events(limit=limit)


@app.get("/api/messages")
async def get_messages(channel_idx: int = None, hours: int = 48, limit: int = 200):
    """Return recent messages, optionally filtered by channel index."""
    messages = store.get_messages(channel_idx=channel_idx, hours=hours, limit=limit)
    # Enrich messages that have hops but no stored path with the current known route
    for msg in messages:
        if msg.get("hops", -1) > 0 and not msg.get("path"):
            sender = msg.get("sender_pubkey", "")
            if sender:
                _, stored_path = store.get_route_by_prefix(sender)
                if stored_path:
                    msg["path"] = stored_path
    return messages


@app.post("/api/messages/send")
async def send_message(request: Request):
    """Send a message to a channel or contact."""
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "Message text is required"}

    if "channel_idx" in body:
        return await poller.send_channel_message(int(body["channel_idx"]), text)
    elif "pubkey" in body:
        return await poller.send_contact_message(body["pubkey"], text)
    else:
        return {"ok": False, "error": "Must specify channel_idx or pubkey"}


# --- Settings API ---

@app.get("/api/settings")
async def get_settings():
    """Return current settings for the settings page."""
    return cfg.get_settings()


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings from the web UI and trigger poller reconnect."""
    body = await request.json()

    # Validate required fields
    if "companion_host" not in body or not body["companion_host"]:
        return {"ok": False, "error": "Companion host IP is required"}

    if "companion_port" not in body:
        body["companion_port"] = 5000

    # Ensure port is an integer
    try:
        body["companion_port"] = int(body["companion_port"])
    except (ValueError, TypeError):
        return {"ok": False, "error": "Port must be a number"}

    # Validate repeaters list
    repeaters = body.get("repeaters", [])
    for r in repeaters:
        if not r.get("name") or not r.get("pubkey"):
            return {"ok": False, "error": "Each repeater needs a name and public key"}

    # Ensure timing values are integers with sensible minimums
    body.setdefault("poll_interval_seconds", 120)
    body.setdefault("stagger_delay_seconds", 15)
    body.setdefault("stale_threshold_seconds", 900)
    try:
        body["poll_interval_seconds"] = max(30, int(body["poll_interval_seconds"]))
        body["stagger_delay_seconds"] = max(5, int(body["stagger_delay_seconds"]))
        body["stale_threshold_seconds"] = max(60, int(body["stale_threshold_seconds"]))
    except (ValueError, TypeError):
        return {"ok": False, "error": "Timing values must be numbers"}

    # Log retention
    body.setdefault("log_retention_hours", 24)
    try:
        body["log_retention_hours"] = max(1, int(body["log_retention_hours"]))
    except (ValueError, TypeError):
        body["log_retention_hours"] = 24

    # Preserve fields managed by other endpoints (e.g. ntfy toggle) that aren't in this payload
    existing = cfg.get_settings()
    for key in ("ntfy_enabled",):
        if key not in body:
            body[key] = existing.get(key, True)

    # Save to settings.json
    cfg.save_settings(body)
    logger.info(f"Settings saved: {body['companion_host']}:{body['companion_port']}, "
                f"{len(repeaters)} repeaters")

    # Sync the data store with the new repeater list
    store.sync_repeaters(repeaters)

    # Tell the poller to reconnect with new settings
    poller.request_reconnect()

    return {"ok": True}


# --- Reorder & Ping APIs ---

@app.post("/api/reorder")
async def reorder_repeaters(request: Request):
    """Reorder repeaters in settings and in the live data store."""
    body = await request.json()
    pubkeys = body.get("pubkeys", [])
    if not pubkeys:
        return {"ok": False, "error": "No pubkeys provided"}

    settings = cfg.get_settings()
    existing = {r["pubkey"]: r for r in settings.get("repeaters", [])}
    settings["repeaters"] = [existing[pk] for pk in pubkeys if pk in existing]
    # Preserve any not in the list (shouldn't happen, but be safe)
    for pk, r in existing.items():
        if pk not in pubkeys:
            settings["repeaters"].append(r)
    cfg.save_settings(settings)
    store.reorder(pubkeys)
    return {"ok": True}


@app.post("/api/ping/{pubkey}")
async def ping_repeater(pubkey: str):
    """Ping a repeater and return round-trip latency."""
    return await poller.ping_repeater(pubkey)


@app.post("/api/advert/{pubkey}")
async def send_advert(pubkey: str):
    """Login to a repeater and trigger it to broadcast a flood advertisement."""
    return await poller.send_advert(pubkey)


@app.post("/api/ntfy/test")
async def test_ntfy(request: Request):
    """Send a test push notification using the provided topic and server."""
    body = await request.json()
    topic = str(body.get("topic", "")).strip()
    server = str(body.get("server", "https://ntfy.sh")).strip().rstrip("/")
    click_url = str(body.get("click_url", "")).strip()
    if not topic:
        return {"ok": False, "error": "No topic provided"}
    if poller:
        await poller._send_ntfy_to(
            server, topic,
            "MeshCore Test", "Push notifications are working!", click_url
        )
    return {"ok": True}


@app.post("/api/ntfy/toggle")
async def toggle_ntfy():
    """Toggle push notifications on/off without changing topic/server settings."""
    s = cfg.get_settings()
    s["ntfy_enabled"] = not s.get("ntfy_enabled", True)
    cfg.save_settings(s)
    logger.info(f"Push notifications {'enabled' if s['ntfy_enabled'] else 'disabled'}")
    return {"ok": True, "enabled": s["ntfy_enabled"]}


# --- Update API ---

# Allowed file paths inside the zip (only update dashboard source files)
_ALLOWED_UPDATE_PATHS = {
    "app.py", "config.py", "data_store.py", "meshcore_poller.py", "requirements.txt",
    "docker-compose.yml",
}
_ALLOWED_UPDATE_PREFIXES = ("templates/", "static/")
# These are top-level source directories — never strip them during normalisation
_KNOWN_TOP_DIRS = {"templates", "static"}


def _is_allowed_path(name: str) -> bool:
    if name in _ALLOWED_UPDATE_PATHS:
        return True
    return any(name.startswith(p) for p in _ALLOWED_UPDATE_PREFIXES)


@app.post("/api/update")
async def apply_update(file: UploadFile = File(...)):
    """Accept a zip file, validate its contents, and extract to /app/."""
    if not file.filename.endswith(".zip"):
        return {"ok": False, "error": "File must be a .zip archive"}

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        return {"ok": False, "error": "Upload too large (max 20 MB)"}

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            names = zf.namelist()
            # Strip leading top-level directory if zip was created with one
            # e.g. "meshcore-dashboard/app.py" -> "app.py"
            # But do NOT strip known source dirs like "templates/" or "static/"
            def _normalise(name: str) -> str:
                parts = name.split("/", 1)
                if (len(parts) == 2 and "." not in parts[0]
                        and parts[0] not in _KNOWN_TOP_DIRS and parts[1]):
                    return parts[1]
                return name

            normalised = [_normalise(n) for n in names]
            bad = [n for n in normalised if n and not n.endswith("/") and not _is_allowed_path(n)]
            if bad:
                return {"ok": False, "error": f"Zip contains unexpected paths: {bad[:5]}"}

            for zip_name, norm_name in zip(names, normalised):
                if not norm_name or norm_name.endswith("/"):
                    continue
                dest = BASE_DIR / norm_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(zip_name))

        logger.info(f"Update applied: {len([n for n in normalised if n and not n.endswith('/')])} files")
        return {"ok": True, "files": [n for n in normalised if n and not n.endswith("/")]}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "Invalid zip file"}
    finally:
        os.unlink(tmp_path)


@app.post("/api/restart")
async def restart_app():
    """Send SIGTERM to self — Docker will restart the container."""
    logger.info("Restart requested via /api/restart")

    async def _delayed_kill():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_kill())
    return {"ok": True}
